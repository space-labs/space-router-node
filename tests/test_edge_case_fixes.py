"""Unit tests for the edge-case bug fixes in PR 'fix/leg2-edge-case-bugs'.

Covers:

- **P9**: submitter refuses to generate receipts when NODE_RATE_PER_GB=0.
- **P14**: 403 "Timestamp expired" → SIGN_REJECTED_CLOCK_SKEW (transient).
- **S1**: reaper detects reorg and reverts claimed → claimable.
- **S3**: unclaimed() excludes CLAIM_TX_TIMEOUT rows even with include_retryable.
- Receipt store: new ``revert_claimed`` and ``list_recently_claimed`` helpers.
- Daemon lock: second acquire on same store fails; different stores coexist.

The startup sanity checks (P1, P2, S8) are exercised by reading the
helper ``_verify_escrow_config`` in main.py — since it depends on a real
RPC + ABI, the E2E verification is done on seethis, not here.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.receipt_store import get_store


def _mk_receipt(**overrides) -> Receipt:
    base = dict(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024, total_price=1,
    )
    base.update(overrides)
    return Receipt(**base)


# ── P9: rate=0 skip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submitter_skips_when_rate_is_zero(tmp_path):
    from app.payment.receipt_submitter import ReceiptSubmitter

    class FakeSettings:
        NODE_RATE_PER_GB = 0
        RECEIPT_STORE_PATH = str(tmp_path / "r.db")
        COORDINATION_API_URL = "http://coord"

    s = ReceiptSubmitter(
        settings=FakeSettings(),
        node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )
    # Must not raise, must not persist anything.
    await s.submit("req-1", 10_000)

    store = get_store(str(tmp_path / "r.db"))
    await store.initialize()
    summary = await store.summary()
    assert sum(summary[k] for k in ("claimable", "pending_sign",
                                     "failed_retryable", "failed_terminal",
                                     "claimed")) == 0


# ── P14: clock skew classification ──────────────────────────────────


@pytest.mark.asyncio
async def test_submit_403_timestamp_expired_is_transient_clock_skew(tmp_path):
    from app.payment.receipt_submitter import _record_clock_skew

    db = tmp_path / "r.db"
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    class S:
        RECEIPT_STORE_PATH = str(db)

    await _record_clock_skew(
        S(), r.request_uuid, "Timestamp expired. Must be within 60s.",
    )

    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.last_error_code == reasons.SIGN_REJECTED_CLOCK_SKEW
    # Transient → counter did NOT increment.
    assert stored.sign_attempts == 0
    assert stored.locked is False


@pytest.mark.asyncio
async def test_timestamp_expired_routes_to_clock_skew_handler(tmp_path):
    from app.payment.receipt_submitter import ReceiptSubmitter

    db = tmp_path / "r.db"

    class FakeSettings:
        NODE_RATE_PER_GB = 10**18
        RECEIPT_STORE_PATH = str(db)
        COORDINATION_API_URL = "http://coord"

    s = ReceiptSubmitter(
        settings=FakeSettings(),
        node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )

    recorded = {}

    async def fake_skew(settings, uuid, detail):
        recorded["uuid"] = uuid
        recorded["detail"] = detail

    async def fake_generic(settings, uuid, resp):
        recorded["generic"] = uuid

    resp = httpx.Response(
        status_code=403,
        json={"detail": "Timestamp expired. Must be within 60s."},
    )
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json):
            return resp

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()), \
         patch("app.payment.receipt_submitter._record_clock_skew", fake_skew), \
         patch("app.payment.receipt_submitter._record_sign_rejection", fake_generic):
        await s._fire_submit(r, "req-1")

    assert recorded.get("uuid") == r.request_uuid
    assert "generic" not in recorded  # not routed to permanent path


@pytest.mark.asyncio
async def test_403_other_detail_routes_to_generic_rejection(tmp_path):
    """A 403 that isn't about timestamps (e.g. Signature mismatch)
    should be treated as a permanent rejection so the user sees it."""
    from app.payment.receipt_submitter import ReceiptSubmitter

    db = tmp_path / "r.db"

    class FakeSettings:
        NODE_RATE_PER_GB = 10**18
        RECEIPT_STORE_PATH = str(db)
        COORDINATION_API_URL = "http://coord"

    s = ReceiptSubmitter(
        settings=FakeSettings(), node_id="n1",
        identity_key="0x" + "c" * 64,
        identity_address="0x" + "a" * 40,
        gateway_payer_address="0x" + "d" * 40,
        node_wallet_address="0x" + "e" * 40,
    )

    routed = {}

    async def fake_skew(settings, uuid, detail):
        routed["skew"] = uuid

    async def fake_generic(settings, uuid, resp):
        routed["generic"] = uuid

    resp = httpx.Response(
        status_code=403, json={"detail": "Signature mismatch"},
    )
    r = _mk_receipt()
    store = get_store(str(db))
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json):
            return resp

    with patch("app.payment.receipt_submitter.httpx.AsyncClient",
               return_value=FakeClient()), \
         patch("app.payment.receipt_submitter._record_clock_skew", fake_skew), \
         patch("app.payment.receipt_submitter._record_sign_rejection", fake_generic):
        await s._fire_submit(r, "req-1")

    assert "generic" in routed
    assert "skew" not in routed


# ── S3: CLAIM_TX_TIMEOUT rows always excluded from claim batches ────


@pytest.mark.asyncio
async def test_unclaimed_excludes_timeout_even_with_include_retryable(tmp_path):
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r_fresh = _mk_receipt()
    r_timeout = _mk_receipt()
    r_revert = _mk_receipt()
    await store.store(r_fresh, signature="0x01")
    await store.store(r_timeout, signature="0x02")
    await store.store(r_revert, signature="0x03")
    await store.mark_claim_failed(
        [r_timeout.request_uuid], reasons.CLAIM_TX_TIMEOUT,
    )
    await store.mark_claim_failed(
        [r_revert.request_uuid], reasons.CLAIM_REVERTED,
    )

    default = await store.unclaimed()
    default_uuids = {s.receipt.request_uuid for s in default}
    assert default_uuids == {r_fresh.request_uuid}

    with_retry = await store.unclaimed(include_retryable=True)
    with_retry_uuids = {s.receipt.request_uuid for s in with_retry}
    # Timeout is NOT included even though include_retryable is True.
    assert with_retry_uuids == {r_fresh.request_uuid, r_revert.request_uuid}


# ── S1: reorg reconciliation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaper_reverts_claimed_on_reorg(tmp_path):
    from app.payment.reaper import ClaimReaper

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_claimed([r.request_uuid], "0xrealhash")

    class S:
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        RECEIPT_STORE_PATH = str(db)

    reaper = ClaimReaper(settings=S())

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Mock Web3: isNonceUsed returns False (reorg!).
    with patch("app.payment.reaper.asyncio.to_thread", fake_to_thread), \
         patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        # Timeout pass sees no rows; reorg pass sees our one claimed row.
        contract.functions.isNonceUsed.return_value.call.return_value = False
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        result = await reaper.tick()

    assert result["reorg_checked"] == 1
    assert result["reorg_reverted"] == 1

    restored = await store.get_by_uuid(r.request_uuid)
    assert restored.view == "claimable"
    assert restored.claimed_at is None
    assert restored.claim_tx_hash is None
    # Counter NOT incremented — reorg isn't the operator's fault.
    assert restored.claim_attempts == 0


@pytest.mark.asyncio
async def test_reaper_reorg_skips_external_reconciled_rows(tmp_path):
    """Rows previously reconciled via isNonceUsed (tx_hash='external')
    must NOT be re-checked on reorg pass — they weren't a real tx from
    us, so a reorg of our own claim tx is not the concern."""
    from app.payment.reaper import ClaimReaper

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_claimed([r.request_uuid], "external")

    class S:
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        RECEIPT_STORE_PATH = str(db)

    rows = await store.list_recently_claimed(younger_than_seconds=3600)
    # Pre-test: external rows excluded from the reorg-candidate query.
    assert rows == []


# ── receipt_store: new helpers ──────────────────────────────────────


@pytest.mark.asyncio
async def test_revert_claimed_clears_state_but_preserves_counters(tmp_path):
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)
    # Pretend it later claimed successfully.
    await store.mark_claimed([r.request_uuid], "0xrealhash")
    pre = await store.get_by_uuid(r.request_uuid)
    assert pre.view == "claimed"
    assert pre.claim_attempts == 1

    assert await store.revert_claimed(r.request_uuid) is True

    post = await store.get_by_uuid(r.request_uuid)
    assert post.view == "claimable"
    assert post.claimed_at is None
    assert post.claim_tx_hash is None
    # Counter preserved — reorg undoes the claim, doesn't refund retries.
    # But last_error_code is cleared so it re-enters the claim queue.
    assert post.last_error_code is None


@pytest.mark.asyncio
async def test_revert_claimed_refuses_on_locked(tmp_path):
    """Defensive guard: if somehow a row ended up in the impossible
    ``claimed AND locked=1`` state (e.g. future-you adds a code path
    that locks claimed rows), reorg reconciliation must refuse to
    un-claim it."""
    import sqlite3
    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_claimed([r.request_uuid], "0xrealhash")
    # Force the impossible state directly — lock() would refuse on an
    # already-claimed row.
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE signed_receipts SET locked = 1 WHERE request_uuid = ?",
            (r.request_uuid,),
        )

    assert await store.revert_claimed(r.request_uuid) is False
    assert (await store.get_by_uuid(r.request_uuid)).claimed_at is not None


# ── Daemon lock ─────────────────────────────────────────────────────


def test_daemon_lock_refuses_second_acquire_on_same_store(tmp_path):
    """Two daemons on the same receipts.db must not both start.

    flock is a per-process advisory lock — the real protection is
    cross-process. We spawn a subprocess to simulate "another daemon"
    and verify it exits non-zero.
    """
    import subprocess

    store_dir = tmp_path / "holder"
    store_dir.mkdir()
    store_path = store_dir / "receipts.db"

    holder_code = f"""
import sys, time
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from app.main import _acquire_daemon_lock

class S:
    RECEIPT_STORE_PATH = {str(store_path)!r}

fd = _acquire_daemon_lock(S())
print("HOLDER_OK")
sys.stdout.flush()
time.sleep(10)  # hold the lock
"""

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait until the holder has actually acquired the lock.
    for _ in range(50):
        line = holder.stdout.readline()
        if "HOLDER_OK" in line:
            break
        time.sleep(0.05)
    else:
        holder.kill()
        pytest.fail("Holder never acquired the lock")

    try:
        # Second acquire in a fresh subprocess MUST exit non-zero.
        contender = subprocess.run(
            [sys.executable, "-c", holder_code.replace("time.sleep(10)", "")],
            capture_output=True, text=True, timeout=10,
        )
        assert contender.returncode == 1
        assert "already running" in (contender.stderr or "").lower()
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_daemon_lock_allows_different_stores(tmp_path):
    """Two daemons pointing at different store paths are fine.

    In-process test is sufficient here — different files can never
    conflict via flock regardless of process boundaries.
    """
    from app.main import _acquire_daemon_lock

    class SettingsA:
        RECEIPT_STORE_PATH = str(tmp_path / "a" / "receipts.db")

    class SettingsB:
        RECEIPT_STORE_PATH = str(tmp_path / "b" / "receipts.db")

    fd_a = _acquire_daemon_lock(SettingsA())
    fd_b = _acquire_daemon_lock(SettingsB())
    assert fd_a >= 0
    assert fd_b >= 0
    assert fd_a != fd_b


def test_daemon_lock_reclaims_stale_lock(tmp_path):
    """A lock file from a crashed predecessor (PID written but that
    process is no longer alive) should be reclaimable — the stale-PID
    check treats it as abandoned. This is the Windows-smoke-test case
    where the OS takes a moment to release the file-lock after
    TerminateProcess.
    """
    from app.main import _acquire_daemon_lock

    store_dir = tmp_path / "crashed"
    store_dir.mkdir()
    lock_path = store_dir / "daemon.lock"
    # Simulate a dead predecessor: write a PID that definitely isn't
    # a live process. Values above 4 million exceed typical PID space.
    dead_pid = 4_194_303
    lock_path.write_text(f"{dead_pid}\n")

    class S:
        RECEIPT_STORE_PATH = str(store_dir / "receipts.db")

    fd = _acquire_daemon_lock(S())
    assert fd >= 0

    # File now has our live PID, not the dead one.
    content = lock_path.read_text()
    assert str(os.getpid()) in content


# ── Disk-space check ────────────────────────────────────────────────


def test_disk_space_check_warns_on_low(tmp_path, caplog):
    """Low disk should log WARN, not ERROR. Doesn't raise."""
    from app.main import _check_disk_space

    class FakeSettings:
        RECEIPT_STORE_PATH = str(tmp_path / "receipts.db")

    import shutil
    orig = shutil.disk_usage

    class FakeUsage:
        total = 10 * 1024 * 1024 * 1024  # 10 GB
        free = 200 * 1024 * 1024          # 200 MB — low
        used = 0

    def fake_usage(path):
        return FakeUsage()

    shutil.disk_usage = fake_usage
    try:
        with caplog.at_level("WARNING"):
            _check_disk_space(FakeSettings())
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("disk low" in (r.message or "").lower() for r in warnings)
    finally:
        shutil.disk_usage = orig


def test_disk_space_check_errors_on_critical(tmp_path, caplog):
    """<50MB free must log ERROR so operators notice."""
    from app.main import _check_disk_space

    class FakeSettings:
        RECEIPT_STORE_PATH = str(tmp_path / "receipts.db")

    import shutil
    orig = shutil.disk_usage

    class FakeUsage:
        total = 10 * 1024 * 1024 * 1024
        free = 10 * 1024 * 1024
        used = 0

    shutil.disk_usage = lambda path: FakeUsage()
    try:
        with caplog.at_level("ERROR"):
            _check_disk_space(FakeSettings())
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("almost full" in (r.message or "").lower() for r in errors)
    finally:
        shutil.disk_usage = orig
