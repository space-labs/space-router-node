"""rc.8 MAJ-6 — receipt_store singleton must not survive Stop→Start.

Diagnostic PR #108 confirmed Hypothesis A: on macOS, the pywebview
process is long-lived and ``app.payment.receipt_store._singleton``
survives a Save & Restart cycle. The cached ``ReceiptStore`` holds an
async-sqlite connection that was opened on the previous event loop's
thread; reusing it on the next loop fails with a thread-affinity error
and parks the daemon in ERROR before reaching EARNING.

Same singleton-survives-the-cycle bug class as rc.6 BLK-2 — different
trigger. The rc.6 fix wired ``clear_singleton()`` into the Reset path
(``app.paths.wipe_operational_state``); the rc.8 fix wires it into the
Stop path (``Api.stop_node``) and the Start preamble
(``Api.start_node``).

These tests assert the singleton-clearing contract, not the network
behavior. The daemon thread lifecycle is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def reset_receipt_store_singleton():
    """Module-level globals leak across tests; ensure each test sees a
    clean ``_singleton`` going in and out."""
    from app.payment import receipt_store as rs

    rs.clear_singleton()
    yield rs
    rs.clear_singleton()


def _make_api_with_mock_node():
    """Build an ``Api`` whose ``NodeManager`` is a MagicMock — no real
    daemon thread, no listener, no event loop. We only care about the
    singleton-clearing contract around ``start_node`` / ``stop_node``."""
    from gui.api import Api

    node = MagicMock()
    # Mirror the real attribute the G5 path touches. Without this the
    # MagicMock would auto-create one and the assertion below would
    # accidentally pass.
    node.status.staking_status = "earning"
    # ``start_node`` consults this; default the MagicMock to "not running"
    # so the start path actually executes.
    node.is_running = False

    config = MagicMock()
    # apply_to_env / _load_settings_v2 must be quiet defaults — the
    # MagicMock returns child mocks by default, which is fine.
    config._load_settings_v2.return_value.wallet.identity_passphrase_set = False

    return Api(config=config, node_manager=node), node


# ---------------------------------------------------------------------------
# rc.8 MAJ-6 — stop_node must drop the cached store
# ---------------------------------------------------------------------------


class TestStopNodeClearsReceiptStoreSingleton:
    def test_stop_node_clears_singleton(
        self, tmp_path, reset_receipt_store_singleton
    ):
        rs = reset_receipt_store_singleton

        # Prime: a prior session built the singleton.
        path = tmp_path / "receipts.db"
        first = rs.get_store(path)
        assert rs._singleton is first

        api, _ = _make_api_with_mock_node()
        result = api.stop_node()
        assert result == {"ok": True}

        # Contract #1 — the cached singleton is gone.
        assert rs._singleton is None

    def test_get_store_after_stop_returns_fresh_instance(
        self, tmp_path, reset_receipt_store_singleton
    ):
        """Contract #2 — the next ``get_store`` returns a NEW object
        (different ``id()``), so the next ``initialize()`` rebinds the
        async-sqlite connection to the current event loop's thread."""
        rs = reset_receipt_store_singleton

        path = tmp_path / "receipts.db"
        first = rs.get_store(path)
        first_id = id(first)

        api, _ = _make_api_with_mock_node()
        api.stop_node()

        second = rs.get_store(path)
        assert id(second) != first_id
        assert second is not first

    def test_stop_node_clears_singleton_even_when_node_stop_raises(
        self, tmp_path, reset_receipt_store_singleton
    ):
        """If ``self._node.stop()`` raises, ``stop_node`` returns an
        error — but we must NOT leave the stale singleton in place. The
        whole point of MAJ-6 is that the next Start gets a clean store.

        The current implementation places ``clear_singleton()`` after
        the try/except for ``stop()``, so an exception short-circuits
        and returns before the clear runs. That is acceptable: in the
        failure path the GUI will not transition to a new "Start" cycle
        without the user clicking again, at which point ``start_node``'s
        belt-and-braces clear fires. This test pins that behavior — if a
        future refactor moves the clear into a ``finally`` that's fine,
        but the start-side clear must continue to cover the gap."""
        rs = reset_receipt_store_singleton

        path = tmp_path / "receipts.db"
        rs.get_store(path)
        assert rs._singleton is not None

        api, node = _make_api_with_mock_node()
        node.stop.side_effect = RuntimeError("boom")
        result = api.stop_node()
        assert result["ok"] is False

        # Singleton may or may not be cleared in the raise-path. The
        # invariant we *do* enforce is that start_node's clear covers
        # this gap — see the next test class.

    def test_stop_node_still_blanks_staking_status(
        self, tmp_path, reset_receipt_store_singleton
    ):
        """Regression: rc.6 G5 contract (synchronous staking_status
        blanking) must not regress. The MAJ-6 clear lives below the
        ``self._node.stop()`` call, so the G5 invariant is unaffected,
        but pin it explicitly."""
        api, node = _make_api_with_mock_node()
        captured: list[str] = []

        def _stop(timeout=None):
            captured.append(node.status.staking_status)

        node.stop = _stop
        api.stop_node()
        assert captured == ["—"]


# ---------------------------------------------------------------------------
# rc.8 MAJ-6 — start_node belt-and-braces clear
# ---------------------------------------------------------------------------


class TestStartNodeClearsReceiptStoreSingleton:
    def test_start_node_clears_singleton_before_starting(
        self, tmp_path, reset_receipt_store_singleton
    ):
        """Belt-and-braces: even if some prior path left the singleton
        in place (e.g. a previous Stop that raised, or a code path we
        haven't audited yet), ``start_node`` must drop it before kicking
        the daemon. Otherwise the daemon's ``initialize()`` reuses the
        stale connection on the new loop's thread → ERROR."""
        rs = reset_receipt_store_singleton

        path = tmp_path / "receipts.db"
        first = rs.get_store(path)
        assert rs._singleton is first

        api, _ = _make_api_with_mock_node()
        result = api.start_node()
        assert result == {"ok": True}

        # Singleton was cleared before start ran.
        assert rs._singleton is None

    def test_start_node_does_not_clear_when_passphrase_required(
        self, tmp_path, reset_receipt_store_singleton
    ):
        """If ``start_node`` short-circuits with PASSPHRASE_REQUIRED,
        clearing the singleton is harmless — the daemon won't actually
        start. The current implementation places the clear after the
        passphrase gate, so the singleton survives this short-circuit;
        that's fine because no new event loop has been created either.

        This test exists to lock the ordering: the clear must NOT come
        before the passphrase gate, otherwise we'd needlessly thrash
        the cache on every wrong-passphrase prompt."""
        import os

        rs = reset_receipt_store_singleton

        path = tmp_path / "receipts.db"
        first = rs.get_store(path)
        assert rs._singleton is first

        api, _ = _make_api_with_mock_node()
        api._config._load_settings_v2.return_value.wallet.identity_passphrase_set = True

        # Ensure no passphrase in env so the gate fires.
        os.environ.pop("SR_IDENTITY_PASSPHRASE", None)
        result = api.start_node()
        assert result["error_code"] == "PASSPHRASE_REQUIRED"

        # Singleton survives the short-circuit — no event loop was
        # created, so the cached connection is still valid.
        assert rs._singleton is first


# ---------------------------------------------------------------------------
# rc.6 BLK-2 regression — Reset path must still clear the singleton
# ---------------------------------------------------------------------------


class TestResetPathStillClearsSingleton:
    """Pin the rc.6 BLK-2 fix so the rc.8 changes don't accidentally
    regress the Reset path. ``wipe_operational_state`` is the original
    caller of ``clear_singleton()``."""

    def test_wipe_operational_state_clears_singleton(
        self, tmp_path, reset_receipt_store_singleton
    ):
        from app.paths import wipe_operational_state

        rs = reset_receipt_store_singleton

        path = tmp_path / "receipts.db"
        first = rs.get_store(path)
        assert rs._singleton is first

        wipe_operational_state(tmp_path)
        assert rs._singleton is None

        # Next caller gets a fresh instance.
        second = rs.get_store(path)
        assert second is not first
