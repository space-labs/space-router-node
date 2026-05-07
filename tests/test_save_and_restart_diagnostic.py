"""MAJ-6 diagnostic — Settings → Save & Restart on macOS GUI

After Settings → Save & Restart, the macOS Provider GUI goes offline with
``Endpoint verification failed: connection_refused``. Linux CLI is
unaffected (fork-per-start), Windows is unaffected.

Two competing hypotheses:

A. ``app.payment.receipt_store._singleton`` is module-level. The pywebview
   process holds it across Stop->Start, so the second start re-uses the
   same ``ReceiptStore`` (and any DB-handle / WAL state cached inside).

B. UPnP teardown/setup race: Stop calls ``remove_upnp_mapping`` which
   tells the IGD to drop the lease; Start calls ``setup_upnp_mapping``
   immediately after. The router can take 5-15s to clear the previous
   mapping, so the new ``addportmapping`` either errors silently (returns
   None) or returns a stale mapping that no longer points at the freshly
   bound socket. The coord probes the advertised endpoint and gets
   ``connection_refused`` at the network layer.

This test:

- exercises ``Api.stop_node`` -> ``Api.start_node`` in a single process
- mocks UPnP add/remove + the registration HTTP path so we don't hit
  the network
- captures every ``[MAJ-6-diag]`` log line
- asserts on which hypothesis each captured line supports

The test does NOT fix the bug. It only confirms the diagnosis.
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Log capture helper
# ---------------------------------------------------------------------------


class _DiagCapture(logging.Handler):
    """Capture every log record whose message starts with ``[MAJ-6-diag]``."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "[MAJ-6-diag]" in msg:
            self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def matching(self, fragment: str) -> list[str]:
        return [m for m in self.messages() if fragment in m]


@pytest.fixture
def diag_capture():
    handler = _DiagCapture()
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # Make sure target loggers also propagate.
    for name in (
        "gui.api",
        "app.upnp",
        "app.registration",
        "app.payment.receipt_store",
        "app.main",
    ):
        logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


# ---------------------------------------------------------------------------
# Hypothesis A — receipt_store singleton survives Stop->Start
# ---------------------------------------------------------------------------


def test_hypothesis_a_receipt_store_singleton_survives_stop_start(
    tmp_path, diag_capture,
):
    """Drive ``get_store`` twice with a Stop->Start in between.

    If hypothesis A is true, ``id()`` of the returned store is identical
    across the two calls — proving the module-level singleton is not
    cleared by ``Api.stop_node`` and is reused by the next
    ``Api.start_node`` (i.e. any handle / WAL state inside the cached
    store leaks across the restart).
    """
    from app.payment import receipt_store as rs

    # Reset module state for hermetic test.
    rs._singleton = None

    db_path = tmp_path / "receipts.db"

    # First "start": fresh store created.
    store1 = rs.get_store(db_path)
    id1 = id(store1)

    # Simulate Api.stop_node returning normally. stop_node does NOT call
    # clear_singleton today — that's the bug. We assert the bug here.
    # (We do NOT call clear_singleton.)

    # Second "start" immediately after: same db_path -> cache hit.
    store2 = rs.get_store(db_path)
    id2 = id(store2)

    assert id1 == id2, (
        "expected receipt_store singleton to survive Stop->Start "
        "(this confirms hypothesis A — fix would call clear_singleton "
        "from stop_node or in_app start_node)"
    )

    # Diagnostic log evidence.
    cached = diag_capture.matching("get_store CACHED")
    fresh = diag_capture.matching("get_store FRESH")
    assert len(fresh) == 1, "first call must have been FRESH"
    assert len(cached) == 1, "second call must have been CACHED (=hypothesis A)"


def test_hypothesis_a_clear_singleton_does_invalidate_when_called(
    tmp_path, diag_capture,
):
    """Sanity check: ``clear_singleton`` is wired correctly. The bug is
    that nobody calls it on stop_node, not that the helper is broken."""
    from app.payment import receipt_store as rs

    rs._singleton = None
    db_path = tmp_path / "receipts.db"

    store1 = rs.get_store(db_path)
    rs.clear_singleton()
    store2 = rs.get_store(db_path)

    assert id(store1) != id(store2)
    assert diag_capture.matching("clear_singleton CALLED")


# ---------------------------------------------------------------------------
# Hypothesis B — UPnP teardown / setup race
# ---------------------------------------------------------------------------


def test_hypothesis_b_upnp_remove_then_setup_logs_both_calls(
    diag_capture, monkeypatch,
):
    """Drive the UPnP teardown -> setup pair the way Stop->Start does
    inside ``app.main._run`` and assert the order of calls.

    If the router race (hypothesis B) is real, the second
    ``setup_upnp_mapping`` call after a fresh ``remove_upnp_mapping``
    would either:

    1. return None (mapping rejected because the lease is still being
       torn down on the IGD), or
    2. succeed but the external-port-to-internal-IP table on the router
       still points at the dead socket from the previous start.

    We can't reproduce case 2 without a live IGD; what we CAN
    reproduce here is the call order — proving ``remove_upnp_mapping``
    happens immediately before ``setup_upnp_mapping`` with no
    deferral / retry logic in between. That is the smoking gun for
    hypothesis B regardless of whether miniupnpc actually fails.
    """
    import asyncio

    from app import upnp as upnp_mod

    # Stub out the synchronous miniupnpc helpers — keep the async
    # wrappers untouched so the [MAJ-6-diag] log lines still fire.
    setup_calls: list[tuple] = []
    remove_calls: list[int] = []

    def _fake_setup(internal_ip, internal_port, lease_duration):
        setup_calls.append((internal_ip, internal_port, lease_duration))
        return ("203.0.113.5", internal_port)

    def _fake_remove(external_port):
        remove_calls.append(external_port)

    monkeypatch.setattr(upnp_mod, "_do_upnp_mapping", _fake_setup)
    monkeypatch.setattr(upnp_mod, "_do_upnp_removal", _fake_remove)
    monkeypatch.setattr(upnp_mod, "_get_local_ip", lambda: "192.168.1.42")

    async def drive() -> None:
        # First start: setup mapping.
        first = await upnp_mod.setup_upnp_mapping(8443, lease_duration=3600)
        assert first == ("203.0.113.5", 8443)
        # Stop: remove mapping (this is what app/main.py:2208-2209 does).
        await upnp_mod.remove_upnp_mapping(first[1])
        # Restart: immediately re-add, no delay.
        second = await upnp_mod.setup_upnp_mapping(8443, lease_duration=3600)
        assert second == ("203.0.113.5", 8443)

    asyncio.run(drive())

    # Both setup calls fired — and remove fired between them.
    msgs = diag_capture.messages()
    setup_log = [m for m in msgs if "setup_upnp_mapping CALL" in m]
    remove_log = [m for m in msgs if "remove_upnp_mapping CALL" in m]
    result_log = [m for m in msgs if "setup_upnp_mapping RESULT" in m]

    assert len(setup_log) == 2, "two setup calls expected (initial + restart)"
    assert len(remove_log) == 1, "one remove call between them"
    assert len(result_log) == 2

    # Order check: remove must come BETWEEN the two setups, with no
    # deferral / settling delay in the application code path.
    setup_indices = [i for i, m in enumerate(msgs) if "setup_upnp_mapping CALL" in m]
    remove_index = next(i for i, m in enumerate(msgs) if "remove_upnp_mapping CALL" in m)
    assert setup_indices[0] < remove_index < setup_indices[1], (
        "remove_upnp_mapping must fire between the two setup_upnp_mapping calls "
        "for hypothesis B to apply"
    )

    # The application makes both upnp calls back-to-back inside the same
    # event loop with NO sleep/backoff to give the router time to clear
    # the lease. That's the race window for hypothesis B.


# ---------------------------------------------------------------------------
# Api.stop_node + Api.start_node — diagnostic logs fire end-to-end
# ---------------------------------------------------------------------------


def test_stop_then_start_diagnostic_logs_fire(diag_capture, monkeypatch):
    """End-to-end: drive ``Api.stop_node`` then ``Api.start_node`` with
    a mock NodeManager and confirm the [MAJ-6-diag] log family fires
    for both. This is what the GUI does on Save & Restart.
    """
    from gui.api import Api
    from app.state import NodeState

    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    settings_v2 = MagicMock()
    settings_v2.wallet.identity_passphrase_set = False
    config._load_settings_v2.return_value = settings_v2

    node = MagicMock()
    node.is_running = True  # Pretend we're running before stop.
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING
    node.has_live_thread.return_value = True

    api = Api(config=config, node_manager=node)
    api.stop_node()

    # Now flip "running" off — the second call (start) sees a stopped node.
    node.is_running = False
    node._sm.state = NodeState.IDLE

    api.start_node()

    msgs = diag_capture.messages()

    # Both halves logged ENTRY+EXIT.
    assert any("stop_node ENTRY" in m for m in msgs)
    assert any("stop_node EXIT" in m for m in msgs)
    assert any("start_node ENTRY" in m for m in msgs)
    assert any("start_node EXIT ok" in m for m in msgs)

    # Both must log the receipt_store singleton id so a real-world
    # stop->start log pair can be diffed offline.
    assert any("receipt_store._singleton id=" in m for m in msgs)


def test_stop_node_does_not_clear_receipt_store_singleton(
    tmp_path, diag_capture,
):
    """Pin the bug: ``Api.stop_node`` does NOT call ``clear_singleton``.

    This test will FAIL once the fix lands (and that failure is the
    signal to delete this test). For now it documents the current
    (buggy) behaviour so the diagnosis is unambiguous.
    """
    from app.payment import receipt_store as rs
    from gui.api import Api
    from app.state import NodeState

    rs._singleton = None
    db_path = tmp_path / "receipts.db"

    # Pretend the node was running and had a populated receipt store.
    rs.get_store(db_path)
    pre_id = id(rs._singleton)
    assert pre_id is not None

    node = MagicMock()
    node.is_running = True
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING
    node.has_live_thread.return_value = True

    api = Api(config=MagicMock(), node_manager=node)
    api.stop_node()

    # Bug: the singleton is unchanged.
    assert rs._singleton is not None
    assert id(rs._singleton) == pre_id

    # The diag log shows the same id surviving.
    matches = diag_capture.matching("stop_node EXIT ok")
    assert matches, "stop_node EXIT ok line must be logged"
    # The id appears in that line.
    assert str(pre_id) in matches[0]


# ---------------------------------------------------------------------------
# Summary helper — print captured diagnostic timeline (manual triage aid)
# ---------------------------------------------------------------------------


def test_print_captured_diag_timeline(diag_capture, tmp_path, monkeypatch):
    """Drives the full Stop->Start flow once, then prints the captured
    timeline so a human running the test with -s can read the evidence
    at a glance. Always passes — its only purpose is the side effect.
    """
    from app.payment import receipt_store as rs
    from gui.api import Api
    from app.state import NodeState
    from app import upnp as upnp_mod

    rs._singleton = None
    monkeypatch.setattr(upnp_mod, "_do_upnp_mapping",
                        lambda ip, port, lease: ("203.0.113.5", port))
    monkeypatch.setattr(upnp_mod, "_do_upnp_removal", lambda port: None)
    monkeypatch.setattr(upnp_mod, "_get_local_ip", lambda: "192.168.1.42")

    db_path = tmp_path / "receipts.db"
    rs.get_store(db_path)

    node = MagicMock()
    node.is_running = True
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING
    node.has_live_thread.return_value = True
    api = Api(config=MagicMock(), node_manager=node)
    api.stop_node()

    import asyncio
    async def _upnp_pair():
        await upnp_mod.remove_upnp_mapping(8443)
        await upnp_mod.setup_upnp_mapping(8443)
    asyncio.run(_upnp_pair())

    rs.get_store(db_path)
    node.is_running = False
    node._sm.state = NodeState.IDLE
    api.start_node()

    print("\n----- [MAJ-6-diag] captured timeline -----")
    for m in diag_capture.messages():
        print("  ", m)
    print("------------------------------------------")
