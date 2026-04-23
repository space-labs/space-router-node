"""PR 5: GUI API surface for the Payments screen.

Tests the Python side exposed to pywebview: receipts_summary,
receipts_list, receipts_detail, receipts_claim_all / retry /
status, and the task registry.
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.receipt_store import get_store


def _mk(price=1):
    return Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024, total_price=price,
    )


@pytest.fixture
def api_with_store(tmp_path):
    from gui.api import Api
    db = tmp_path / "r.db"

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 50
        IDENTITY_KEY_PATH = str(tmp_path / "id.key")
        IDENTITY_PASSPHRASE = ""

    patcher = patch("app.main.load_settings", return_value=FakeSettings())
    patcher.start()

    api = Api(config=MagicMock(), node_manager=MagicMock())
    yield api, db, FakeSettings()
    patcher.stop()


def _seed_sync(db_path):
    """Populate a store with one of each view synchronously."""
    import asyncio

    async def _run():
        store = get_store(str(db_path))
        await store.initialize()
        claim = _mk(price=500)
        retry = _mk(price=200)
        locked = _mk(price=100)
        pending = _mk(price=50)
        done = _mk(price=10)
        await store.store(claim, signature="0xs1")
        await store.store(retry, signature="0xs2")
        await store.mark_claim_failed(
            [retry.request_uuid], reasons.CLAIM_REVERTED, "tx 0x1",
        )
        await store.store(locked, signature="0xs3")
        await store.lock(locked.request_uuid)
        await store.store_unsigned(pending, request_id="rp")
        await store.store(done, signature="0xs4")
        await store.mark_claimed([done.request_uuid], "0xtx")
        return {
            "claim": claim, "retry": retry, "locked": locked,
            "pending": pending, "done": done,
        }

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def test_receipts_summary_shape(api_with_store):
    api, db, _ = api_with_store
    _seed_sync(db)

    resp = api.receipts_summary()
    assert resp["ok"] is True
    assert resp["escrow_configured"] is True
    s = resp["summary"]
    assert s["claimable"] == 1
    assert s["failed_retryable"] == 1
    assert s["failed_terminal"] == 1
    assert s["pending_sign"] == 1
    assert s["claimed"] == 1
    assert s["claimable_total_price"] == 500


def test_receipts_list_default_returns_all_views(api_with_store):
    api, db, _ = api_with_store
    _seed_sync(db)

    resp = api.receipts_list(view="all", limit=100, offset=0)
    assert resp["ok"] is True
    assert len(resp["receipts"]) == 5
    views = {r["view"] for r in resp["receipts"]}
    assert views == {
        "claimable", "failed_retryable", "failed_terminal",
        "pending_sign", "claimed",
    }


def test_receipts_list_filters_by_view(api_with_store):
    api, db, _ = api_with_store
    _seed_sync(db)

    resp = api.receipts_list(view="failed_retryable")
    assert resp["ok"] is True
    assert len(resp["receipts"]) == 1
    assert resp["receipts"][0]["view"] == "failed_retryable"


def test_receipts_list_exposes_claim_history_fields(api_with_store):
    """The Earnings screen's Claim history card depends on the API
    returning ``view="claimed"``, ``claimed_at`` and ``claim_tx_hash``
    for claimed rows — lock the contract down with a test."""
    api, db, _ = api_with_store
    _seed_sync(db)

    resp = api.receipts_list(view="all")
    claimed = [r for r in resp["receipts"] if r["view"] == "claimed"]
    assert len(claimed) == 1
    row = claimed[0]
    assert row["claimed_at"] is not None
    assert row["claim_tx_hash"] == "0xtx"


def test_receipts_list_distinguishes_external_reconciled(api_with_store):
    """``tx_hash="external"`` is the synthetic marker the reaper writes
    when the gateway auto-settled on our behalf — the GUI uses that
    string to distinguish "reconciled" rows from rows claimed by a tx
    this node submitted. Pin the convention in a test."""
    import asyncio

    api, db, _ = api_with_store
    reconciled_receipt = _mk(price=77)

    async def _prep():
        store = get_store(str(db))
        await store.initialize()
        await store.store(reconciled_receipt, signature="0xs")
        await store.mark_claimed(
            [reconciled_receipt.request_uuid], tx_hash="external",
        )

    asyncio.new_event_loop().run_until_complete(_prep())

    resp = api.receipts_list(view="all")
    match = [
        r for r in resp["receipts"]
        if r["request_uuid"] == reconciled_receipt.request_uuid
    ]
    assert len(match) == 1
    assert match[0]["view"] == "claimed"
    assert match[0]["claim_tx_hash"] == "external"


def test_receipts_detail_returns_known_uuid(api_with_store):
    api, db, _ = api_with_store
    seeded = _seed_sync(db)

    resp = api.receipts_detail(seeded["claim"].request_uuid)
    assert resp["ok"] is True
    assert resp["receipt"]["request_uuid"] == seeded["claim"].request_uuid
    assert resp["receipt"]["view"] == "claimable"


def test_receipts_detail_not_found(api_with_store):
    api, _, _ = api_with_store
    resp = api.receipts_detail("00000000-0000-0000-0000-000000000000")
    assert resp["ok"] is False
    assert resp["error"] == "not_found"


def test_receipts_retry_on_locked_is_noop(api_with_store):
    api, db, _ = api_with_store
    seeded = _seed_sync(db)

    resp = api.receipts_retry(seeded["locked"].request_uuid)
    assert resp["ok"] is True
    assert resp["noop"] is True
    assert resp["reason"] == "locked"


def test_receipts_retry_on_claimed_is_noop(api_with_store):
    api, db, _ = api_with_store
    seeded = _seed_sync(db)

    resp = api.receipts_retry(seeded["done"].request_uuid)
    assert resp["ok"] is True
    assert resp["noop"] is True
    assert resp["reason"] == "already_claimed"


def test_receipts_retry_not_found(api_with_store):
    api, _, _ = api_with_store
    resp = api.receipts_retry("00000000-0000-0000-0000-000000000000")
    assert resp["ok"] is False
    assert resp["error"] == "not_found"


def test_claim_task_registry_lifecycle():
    from gui.api import _ClaimTaskRegistry

    reg = _ClaimTaskRegistry()
    done = threading.Event()

    def runner():
        done.wait(timeout=1.0)
        return {"ok": True, "submitted": 5}

    task_id = reg.start(runner)
    # Queued → running in the background thread.
    for _ in range(20):
        st = reg.status(task_id)
        if st["state"] == "running":
            break
        time.sleep(0.01)
    assert st["state"] in ("running", "queued")

    done.set()
    for _ in range(50):
        st = reg.status(task_id)
        if st["state"] == "done":
            break
        time.sleep(0.01)
    assert st["state"] == "done"
    assert st["result"]["submitted"] == 5


def test_claim_task_registry_propagates_error():
    from gui.api import _ClaimTaskRegistry

    reg = _ClaimTaskRegistry()

    def runner():
        raise RuntimeError("boom")

    task_id = reg.start(runner)
    for _ in range(50):
        st = reg.status(task_id)
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert st["state"] == "error"
    assert "boom" in st["error"]


def test_claim_task_registry_gc_drops_finished():
    from gui.api import _ClaimTaskRegistry

    reg = _ClaimTaskRegistry()

    def runner():
        return {"done": True}

    task_id = reg.start(runner)
    for _ in range(50):
        if reg.status(task_id)["state"] == "done":
            break
        time.sleep(0.01)

    # Backdate the start time so gc picks it up.
    with reg._lock:
        reg._tasks[task_id]["started_at"] = 0
    reg.gc(max_age_seconds=60)
    assert reg.status(task_id) is None


def test_receipts_claim_all_serialised_by_flock(api_with_store):
    """The flock in _claim_runner must prevent concurrent claim work.

    We simulate two claim_all calls racing; the second one must land
    in a ``{noop:true, reason:claim_in_progress}`` state rather than
    running a real settlement.
    """
    from gui.api import _claim_runner

    api, db, settings = api_with_store
    _seed_sync(db)

    # Stub the actual claim_all so neither call hits the chain.
    import app.payment.settlement as settlement_mod
    from unittest.mock import patch

    call_log = []
    first_running = threading.Event()
    release = threading.Event()

    async def fake_claim_all(s, k, include_retryable=False, only_uuids=None):
        call_log.append(threading.get_ident())
        first_running.set()
        # Hold the lock until the second caller tries.
        release.wait(timeout=2.0)
        return []

    async def fake_reconcile(s, st):
        return 0

    with patch.object(settlement_mod, "claim_all", fake_claim_all), \
         patch("app.main.load_settings", return_value=settings), \
         patch("app.identity.load_or_create_identity",
               return_value=("0x" + "f" * 64, "0x" + "a" * 40)):
        result_a: dict = {}
        result_b: dict = {}

        def run_a():
            result_a.update(_claim_runner(None, False))

        def run_b():
            result_b.update(_claim_runner(None, False))

        ta = threading.Thread(target=run_a)
        ta.start()
        first_running.wait(timeout=2.0)
        # Second call should see flock held and return immediately.
        tb = threading.Thread(target=run_b)
        tb.start()
        tb.join(timeout=3.0)

        assert result_b.get("noop") is True
        assert result_b.get("reason") == "claim_in_progress"

        release.set()
        ta.join(timeout=3.0)
        # A ran to completion.
        assert result_a.get("ok") is True
