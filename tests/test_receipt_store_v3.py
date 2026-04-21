"""v3 schema: failure tracking, lock/unlock, views, summary, migrations."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.receipt_store import ReceiptStore


def _mk_receipt(**overrides) -> Receipt:
    fields = dict(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024,
        total_price=1,
    )
    fields.update(overrides)
    return Receipt(**fields)


@pytest.fixture
def store(tmp_path):
    return ReceiptStore(tmp_path / "receipts.db")


@pytest.mark.asyncio
async def test_fresh_db_is_v3(store, tmp_path):
    await store.initialize()
    with sqlite3.connect(tmp_path / "receipts.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {row[1] for row in conn.execute("PRAGMA table_info(signed_receipts)")}
    assert {"sign_attempts", "claim_attempts", "last_error_code",
            "last_error_detail", "last_attempt_at", "locked"} <= cols


@pytest.mark.asyncio
async def test_partial_v2_to_v3_migration_is_self_healing(tmp_path):
    """If a previous migration run added the columns but failed to bump
    user_version (e.g. concurrent writer scenario seen on seethis during
    PR 1 E2E), a subsequent initialize() must not crash with "duplicate
    column name" — it must detect the existing columns and just fix the
    version pragma."""
    db_path = tmp_path / "receipts.db"

    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE signed_receipts (
                request_uuid      TEXT PRIMARY KEY,
                tunnel_request_id TEXT,
                client_address    TEXT NOT NULL,
                node_address      TEXT NOT NULL,
                data_amount       INTEGER NOT NULL,
                total_price       INTEGER NOT NULL,
                signature         TEXT,
                created_at        INTEGER NOT NULL,
                claimed_at        INTEGER,
                claim_tx_hash     TEXT,
                sign_attempts     INTEGER NOT NULL DEFAULT 0,
                claim_attempts    INTEGER NOT NULL DEFAULT 0,
                last_error_code   TEXT,
                last_error_detail TEXT,
                last_attempt_at   INTEGER,
                locked            INTEGER NOT NULL DEFAULT 0
            );
        """)
        # Deliberately leave user_version at 2 to simulate partial state.
        conn.execute("PRAGMA user_version = 2")

    store = ReceiptStore(db_path)
    await store.initialize()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_v2_to_v3_migration_preserves_rows(tmp_path):
    """Simulate a v2 DB with real data, then run initialize and check columns."""
    db_path = tmp_path / "receipts.db"

    # Hand-write a v2 schema DB with one row.
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE signed_receipts (
                request_uuid      TEXT PRIMARY KEY,
                tunnel_request_id TEXT,
                client_address    TEXT NOT NULL,
                node_address      TEXT NOT NULL,
                data_amount       INTEGER NOT NULL,
                total_price       INTEGER NOT NULL,
                signature         TEXT,
                created_at        INTEGER NOT NULL,
                claimed_at        INTEGER,
                claim_tx_hash     TEXT
            );
            CREATE INDEX idx_signed_receipts_unclaimed
                ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;
            CREATE INDEX idx_signed_receipts_unsigned
                ON signed_receipts (created_at) WHERE signature IS NULL;
        """)
        conn.execute(
            "INSERT INTO signed_receipts "
            "(request_uuid, client_address, node_address, data_amount, "
            "total_price, signature, created_at) VALUES "
            "('u1', '0xaaaa', '0xbbbb', 100, 1, '0xsigned', 1111)",
        )
        conn.execute("PRAGMA user_version = 2")

    store = ReceiptStore(db_path)
    await store.initialize()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        row = conn.execute(
            "SELECT request_uuid, signature, sign_attempts, claim_attempts, "
            "last_error_code, locked FROM signed_receipts"
        ).fetchone()
    assert row == ("u1", "0xsigned", 0, 0, None, 0)


@pytest.mark.asyncio
async def test_mark_sign_failed_increments_and_locks(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    # First attempt → retryable.
    assert await store.mark_sign_failed(
        r.request_uuid, reasons.SIGN_REJECTED_BYTE_MISMATCH, "off by 5%"
    )
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.sign_attempts == 1
    assert stored.locked is False
    assert stored.view == "failed_retryable"
    assert stored.last_error_code == reasons.SIGN_REJECTED_BYTE_MISMATCH
    assert stored.last_error_detail == "off by 5%"

    # Second attempt at cap=2 → locked.
    assert await store.mark_sign_failed(
        r.request_uuid, reasons.SIGN_REJECTED_BYTE_MISMATCH, "off by 6%"
    )
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.sign_attempts == 2
    assert stored.locked is True
    assert stored.view == "failed_terminal"

    # Third attempt on a locked row → no-op (WHERE locked=0 guard).
    assert await store.mark_sign_failed(
        r.request_uuid, reasons.SIGN_REJECTED_BYTE_MISMATCH, "off by 7%"
    ) is False
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.sign_attempts == 2  # unchanged


@pytest.mark.asyncio
async def test_mark_sign_failed_transient_does_not_count(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store_unsigned(r, request_id="req-1")

    # SIGN_TIMEOUT is transient — 10 hits should not lock.
    for _ in range(10):
        await store.mark_sign_failed(r.request_uuid, reasons.SIGN_TIMEOUT)
    stored = await store.get_by_uuid(r.request_uuid)
    assert stored.sign_attempts == 0
    assert stored.locked is False
    assert stored.last_error_code == reasons.SIGN_TIMEOUT


@pytest.mark.asyncio
async def test_mark_claim_failed_batch_locks_after_cap(store):
    r1 = _mk_receipt()
    r2 = _mk_receipt()
    await store.initialize()
    await store.store(r1, signature="0xsig1")
    await store.store(r2, signature="0xsig2")

    updated = await store.mark_claim_failed(
        [r1.request_uuid, r2.request_uuid], reasons.CLAIM_REVERTED, "tx 0xabc",
    )
    assert updated == 2

    s1 = await store.get_by_uuid(r1.request_uuid)
    assert s1.claim_attempts == 1 and s1.locked is False
    assert s1.view == "failed_retryable"

    # Second claim failure locks both.
    await store.mark_claim_failed(
        [r1.request_uuid, r2.request_uuid], reasons.CLAIM_REVERTED, "tx 0xdef",
    )
    s1 = await store.get_by_uuid(r1.request_uuid)
    s2 = await store.get_by_uuid(r2.request_uuid)
    assert s1.locked is True and s2.locked is True


@pytest.mark.asyncio
async def test_unclaimed_default_excludes_retryable_and_locked(store):
    # 3 rows: one fresh claimable, one retryable, one locked.
    r_fresh = _mk_receipt()
    r_retry = _mk_receipt()
    r_lock = _mk_receipt()
    await store.initialize()
    await store.store(r_fresh, signature="0xaaa")
    await store.store(r_retry, signature="0xbbb")
    await store.store(r_lock, signature="0xccc")
    await store.mark_claim_failed([r_retry.request_uuid], reasons.CLAIM_REVERTED)
    await store.lock(r_lock.request_uuid)

    default = await store.unclaimed()
    default_uuids = {s.receipt.request_uuid for s in default}
    assert default_uuids == {r_fresh.request_uuid}

    with_retry = await store.unclaimed(include_retryable=True)
    with_retry_uuids = {s.receipt.request_uuid for s in with_retry}
    assert with_retry_uuids == {r_fresh.request_uuid, r_retry.request_uuid}


@pytest.mark.asyncio
async def test_count_unclaimed_matches_unclaimed_default(store):
    r_fresh = _mk_receipt(total_price=50)
    r_retry = _mk_receipt(total_price=20)
    await store.initialize()
    await store.store(r_fresh, signature="0xaaa")
    await store.store(r_retry, signature="0xbbb")
    await store.mark_claim_failed([r_retry.request_uuid], reasons.CLAIM_REVERTED)

    count, total = await store.count_unclaimed()
    assert count == 1 and total == 50  # retryable excluded


@pytest.mark.asyncio
async def test_list_by_view_buckets(store):
    r_pending = _mk_receipt()
    r_claim = _mk_receipt()
    r_retry = _mk_receipt()
    r_lock = _mk_receipt()
    r_done = _mk_receipt()
    await store.initialize()
    await store.store_unsigned(r_pending, request_id="rp")
    await store.store(r_claim, signature="0xaaa")
    await store.store(r_retry, signature="0xbbb")
    await store.store(r_lock, signature="0xccc")
    await store.store(r_done, signature="0xddd")
    await store.mark_claim_failed([r_retry.request_uuid], reasons.CLAIM_REVERTED)
    await store.lock(r_lock.request_uuid)
    await store.mark_claimed([r_done.request_uuid], "0xtx")

    def uuids(rs):
        return {s.receipt.request_uuid for s in rs}

    assert uuids(await store.list_by_view("pending_sign")) == {r_pending.request_uuid}
    assert uuids(await store.list_by_view("claimable")) == {r_claim.request_uuid}
    assert uuids(await store.list_by_view("failed_retryable")) == {r_retry.request_uuid}
    assert uuids(await store.list_by_view("failed_terminal")) == {r_lock.request_uuid}
    assert uuids(await store.list_by_view("claimed")) == {r_done.request_uuid}
    assert len(await store.list_by_view("all")) == 5


@pytest.mark.asyncio
async def test_summary_counts(store):
    r_a = _mk_receipt(total_price=100)
    r_b = _mk_receipt(total_price=50)
    r_c = _mk_receipt(total_price=30)
    await store.initialize()
    await store.store(r_a, signature="0xa")
    await store.store(r_b, signature="0xb")
    await store.store_unsigned(r_c, request_id="rc")

    summ = await store.summary()
    assert summ["claimable"] == 2
    assert summ["claimable_total_price"] == 150
    assert summ["pending_sign"] == 1
    assert summ["failed_retryable"] == 0
    assert summ["failed_terminal"] == 0
    assert summ["claimed"] == 0


@pytest.mark.asyncio
async def test_unlock_for_retry_resets_counters(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store(r, signature="0xsig")
    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)
    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)

    assert (await store.get_by_uuid(r.request_uuid)).locked is True
    assert await store.unlock_for_retry(r.request_uuid) is True

    restored = await store.get_by_uuid(r.request_uuid)
    assert restored.locked is False
    assert restored.claim_attempts == 0
    assert restored.last_error_code is None
    assert restored.view == "claimable"


@pytest.mark.asyncio
async def test_unlock_refuses_if_already_claimed(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store(r, signature="0xs")
    await store.mark_claimed([r.request_uuid], "0xtx")
    assert await store.unlock_for_retry(r.request_uuid) is False


@pytest.mark.asyncio
async def test_clear_error_preserves_counters(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store(r, signature="0xs")
    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)

    assert await store.clear_error(r.request_uuid) is True
    restored = await store.get_by_uuid(r.request_uuid)
    assert restored.claim_attempts == 1  # counter preserved
    assert restored.last_error_code is None
    assert restored.view == "claimable"


@pytest.mark.asyncio
async def test_list_timed_out_claims(store, monkeypatch):
    import time as time_mod

    r_old = _mk_receipt()
    r_new = _mk_receipt()
    r_other = _mk_receipt()
    await store.initialize()
    await store.store(r_old, signature="0xold")
    await store.store(r_new, signature="0xnew")
    await store.store(r_other, signature="0xoth")

    # Rewrite last_attempt_at directly to simulate age.
    await store.mark_claim_failed([r_old.request_uuid], reasons.CLAIM_TX_TIMEOUT)
    await store.mark_claim_failed([r_new.request_uuid], reasons.CLAIM_TX_TIMEOUT)
    await store.mark_claim_failed([r_other.request_uuid], reasons.CLAIM_REVERTED)

    with sqlite3.connect(store._path) as conn:
        now = int(time_mod.time())
        conn.execute(
            "UPDATE signed_receipts SET last_attempt_at = ? WHERE request_uuid = ?",
            (now - 3600, r_old.request_uuid),
        )
        conn.execute(
            "UPDATE signed_receipts SET last_attempt_at = ? WHERE request_uuid = ?",
            (now - 30, r_new.request_uuid),
        )

    aged = await store.list_timed_out_claims(older_than_seconds=300)
    aged_uuids = {s.receipt.request_uuid for s in aged}
    assert aged_uuids == {r_old.request_uuid}


@pytest.mark.asyncio
async def test_stored_receipt_view_derivation(store):
    r = _mk_receipt()
    await store.initialize()
    await store.store_unsigned(r, request_id="r1")

    s = await store.get_by_uuid(r.request_uuid)
    assert s.view == "pending_sign"

    await store.mark_signed(r.request_uuid, "0xsig")
    s = await store.get_by_uuid(r.request_uuid)
    assert s.view == "claimable"

    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)
    s = await store.get_by_uuid(r.request_uuid)
    assert s.view == "failed_retryable"

    await store.lock(r.request_uuid)
    s = await store.get_by_uuid(r.request_uuid)
    assert s.view == "failed_terminal"

    # Even locked rows flip to claimed if marked claimed out-of-band.
    await store.mark_claimed([r.request_uuid], "0xtx")
    s = await store.get_by_uuid(r.request_uuid)
    assert s.view == "claimed"
