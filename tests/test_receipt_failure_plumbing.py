"""PR 2: submitter rejection → store, poller rejection endpoint, settlement
failure plumbing, and reaper reconciliation logic.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.reaper import ClaimReaper
from app.payment.receipt_store import ReceiptStore, get_store
from app.payment.receipt_submitter import (
    ReceiptPoller, ReceiptSubmitter, _record_sign_rejection,
)


# --- Submitter rejection recording -----------------------------------------


@pytest.mark.asyncio
async def test_submit_records_explicit_rejection_with_known_code(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    monkeypatch.setenv("SR_RECEIPT_STORE_PATH", str(db))

    class Settings:
        RECEIPT_STORE_PATH = str(db)

    r = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    resp = httpx.Response(
        status_code=409,
        json={"reason": "SIGN_REJECTED_BYTE_MISMATCH", "detail": "off by 5%"},
    )
    await _record_sign_rejection(Settings(), r.request_uuid, resp)

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code == reasons.SIGN_REJECTED_BYTE_MISMATCH
    assert stored.last_error_detail == "off by 5%"
    assert stored.sign_attempts == 1
    assert stored.view == "failed_retryable"


@pytest.mark.asyncio
async def test_submit_records_unknown_rejection_falls_back_to_unknown_code(tmp_path):
    db = tmp_path / "r.db"

    class Settings:
        RECEIPT_STORE_PATH = str(db)

    r = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    resp = httpx.Response(status_code=400, text="server unhappy")
    await _record_sign_rejection(Settings(), r.request_uuid, resp)

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code == reasons.SIGN_REJECTED_UNKNOWN_REQUEST
    assert "server unhappy" in (stored.last_error_detail or "")


# --- Poller rejection endpoint ---------------------------------------------


@pytest.mark.asyncio
async def test_poller_tick_rejections_marks_failed(tmp_path):
    db = tmp_path / "r.db"
    r = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    class Settings:
        COORDINATION_API_URL = "http://coord"
        RECEIPT_STORE_PATH = str(db)

    poller = ReceiptPoller(
        settings=Settings(), node_id="n1",
        identity_key="0x" + "c" * 64, node_wallet_address="0x" + "d" * 40,
    )

    rejection_rows = [{
        "request_uuid": r.request_uuid,
        "reason": "SIGN_REJECTED_PRICE_CAP",
        "detail": "rate too high",
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }]

    class FakeResponse:
        def __init__(self, rows):
            self.status_code = 200
            self._rows = rows
        def json(self):
            return self._rows

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            assert "/rejected-receipts" in url
            return FakeResponse(rejection_rows)

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        await poller._tick_rejections(store)

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code == reasons.SIGN_REJECTED_PRICE_CAP
    assert stored.view == "failed_retryable"


@pytest.mark.asyncio
async def test_poller_tick_rejections_404_is_graceful(tmp_path):
    """Coord API without the new endpoint shouldn't break the provider."""
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    class Settings:
        COORDINATION_API_URL = "http://coord"
        RECEIPT_STORE_PATH = str(db)

    poller = ReceiptPoller(
        settings=Settings(), node_id="n1",
        identity_key="0x" + "c" * 64, node_wallet_address="0x" + "d" * 40,
    )

    class FakeResponse:
        status_code = 404
        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            return FakeResponse()

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()):
        # Should not raise.
        await poller._tick_rejections(store)


# --- Reaper reconciliation --------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_marks_landed_tx_as_claimed(tmp_path):
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r_landed = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    r_dropped = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    await store.store(r_landed, signature="0xsig1")
    await store.store(r_dropped, signature="0xsig2")
    # Both receipts hit CLAIM_TX_TIMEOUT; ages need to be past the grace window.
    await store.mark_claim_failed(
        [r_landed.request_uuid, r_dropped.request_uuid],
        reasons.CLAIM_TX_TIMEOUT,
    )
    import sqlite3
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE signed_receipts SET last_attempt_at = ?",
            (int(time.time()) - 10_000,),
        )

    class Settings:
        ESCROW_CHAIN_RPC = "http://fake-rpc"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        RECEIPT_STORE_PATH = str(db)

    reaper = ClaimReaper(settings=Settings())

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("app.payment.reaper.asyncio.to_thread", fake_to_thread), \
         patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        # first row → used=True (landed), second → False (dropped)
        contract.functions.isNonceUsed.return_value.call.side_effect = [True, False]
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        result = await reaper.tick()

    assert result["checked"] == 2
    assert result["reconciled"] == 1
    assert result["cleared"] == 1

    landed_row = await store.get_by_uuid(r_landed.request_uuid)
    dropped_row = await store.get_by_uuid(r_dropped.request_uuid)
    assert landed_row.view == "claimed"
    assert landed_row.claim_tx_hash == "external"
    # Dropped row: error cleared, back to claimable. CLAIM_TX_TIMEOUT is
    # transient so the attempts counter never incremented.
    assert dropped_row.last_error_code is None
    assert dropped_row.claim_attempts == 0
    assert dropped_row.view == "claimable"


@pytest.mark.asyncio
async def test_reaper_disabled_without_escrow_config(tmp_path):
    class Settings:
        ESCROW_CHAIN_RPC = ""
        ESCROW_CONTRACT_ADDRESS = ""
        RECEIPT_STORE_PATH = str(tmp_path / "r.db")

    reaper = ClaimReaper(settings=Settings())
    assert reaper.enabled is False
    # start() is a no-op when not enabled.
    await reaper.start()
    assert reaper._task is None


# --- Settlement failure plumbing -------------------------------------------


@pytest.mark.asyncio
async def test_claim_all_records_revert_and_continues_to_next_batch(tmp_path):
    """A reverted batch must mark its rows ``failed_retryable`` and let
    the loop advance — unlike pre-v1.5 which broke on first error and
    left unrelated receipts unsettled."""
    from app.payment import settlement as settlement_mod

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    # Two receipts; batch size 1 so each is its own batch.
    rs = []
    for _ in range(2):
        r = Receipt(
            client_address="0x" + "a" * 40,
            node_address="0x" + "b" * 64,
            request_uuid=str(uuid.uuid4()),
            data_amount=100, total_price=1,
        )
        rs.append(r)
        await store.store(r, signature="0x" + "11" * 65)

    class Settings:
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 1
        RECEIPT_STORE_PATH = str(db)

    # Stub out the real submit path: first batch reverts, second succeeds.
    call_log = []

    async def fake_submit_batch(settings, key, batch, store_):
        call_log.append([sr.receipt.request_uuid for sr in batch])
        if len(call_log) == 1:
            await store_.mark_claim_failed(
                [sr.receipt.request_uuid for sr in batch],
                reasons.CLAIM_REVERTED, "tx 0xdead",
            )
            return settlement_mod.ClaimResult(
                submitted=1, tx_hash="0xdead", gas_used=100,
                error="tx reverted", reason_code=reasons.CLAIM_REVERTED,
            )
        marked = await store_.mark_claimed(
            [sr.receipt.request_uuid for sr in batch], "0xaa",
        )
        return settlement_mod.ClaimResult(
            submitted=marked, tx_hash="0xaa", gas_used=100,
        )

    async def fake_reconcile(settings, store_):
        return 0

    with patch.object(settlement_mod, "_submit_batch", fake_submit_batch), \
         patch.object(settlement_mod, "_reconcile_already_claimed", fake_reconcile):
        results = await settlement_mod.claim_all(Settings(), "0x" + "f" * 64)

    assert len(call_log) == 2  # loop continued past the revert
    assert len(results) == 2
    assert results[0].reason_code == reasons.CLAIM_REVERTED
    assert results[1].reason_code is None

    # First row now failed_retryable, second row claimed.
    s0 = await store.get_by_uuid(rs[0].request_uuid)
    s1 = await store.get_by_uuid(rs[1].request_uuid)
    assert s0.view == "failed_retryable"
    assert s0.claim_attempts == 1
    assert s1.view == "claimed"


@pytest.mark.asyncio
async def test_claim_all_stops_on_rpc_unreachable(tmp_path):
    """RPC unreachable is the only failure class that aborts the run —
    otherwise we'd spin re-trying against a down endpoint."""
    from app.payment import settlement as settlement_mod

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    for _ in range(3):
        r = Receipt(
            client_address="0x" + "a" * 40,
            node_address="0x" + "b" * 64,
            request_uuid=str(uuid.uuid4()),
            data_amount=100, total_price=1,
        )
        await store.store(r, signature="0x" + "11" * 65)

    class Settings:
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 1
        RECEIPT_STORE_PATH = str(db)

    call_count = 0

    async def fake_submit_batch(settings, key, batch, store_):
        nonlocal call_count
        call_count += 1
        return settlement_mod.ClaimResult(
            submitted=len(batch), tx_hash=None, gas_used=None,
            error="RPC unreachable", reason_code=reasons.CLAIM_RPC_UNREACHABLE,
        )

    async def fake_reconcile(settings, store_):
        return 0

    with patch.object(settlement_mod, "_submit_batch", fake_submit_batch), \
         patch.object(settlement_mod, "_reconcile_already_claimed", fake_reconcile):
        results = await settlement_mod.claim_all(Settings(), "0x" + "f" * 64)

    assert call_count == 1  # stopped after first RPC error
    assert len(results) == 1


@pytest.mark.asyncio
async def test_claim_all_with_only_uuids_restricts_scope(tmp_path):
    """Single-receipt retry path (GUI / --claim --uuid) must not pick up
    unrelated rows even when they're claimable."""
    from app.payment import settlement as settlement_mod

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    targeted = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    other = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=100, total_price=1,
    )
    await store.store(targeted, signature="0xs1")
    await store.store(other, signature="0xs2")

    class Settings:
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 50
        RECEIPT_STORE_PATH = str(db)

    observed = []

    async def fake_submit_batch(settings, key, batch, store_):
        observed.append([sr.receipt.request_uuid for sr in batch])
        marked = await store_.mark_claimed(
            [sr.receipt.request_uuid for sr in batch], "0xaa",
        )
        return settlement_mod.ClaimResult(
            submitted=marked, tx_hash="0xaa", gas_used=100,
        )

    async def fake_reconcile(settings, store_):
        return 0

    with patch.object(settlement_mod, "_submit_batch", fake_submit_batch), \
         patch.object(settlement_mod, "_reconcile_already_claimed", fake_reconcile):
        await settlement_mod.claim_all(
            Settings(), "0x" + "f" * 64, only_uuids=[targeted.request_uuid],
        )

    assert observed == [[targeted.request_uuid]]


@pytest.mark.asyncio
async def test_reaper_tick_noop_when_no_timeouts(tmp_path):
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    class Settings:
        ESCROW_CHAIN_RPC = "http://fake-rpc"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        RECEIPT_STORE_PATH = str(db)

    reaper = ClaimReaper(settings=Settings())
    result = await reaper.tick()
    assert result == {"checked": 0, "reconciled": 0, "cleared": 0}
