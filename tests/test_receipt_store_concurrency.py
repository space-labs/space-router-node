"""Regression test for the SQLite lock contention bug uncovered by the
300-req × 8-worker stress test on Apr 20, 2026.

Before the fix, ``initialize()`` took a write lock on every call (via an
unconditional ``PRAGMA user_version = N``). Under concurrent submits, the
9th concurrent caller timed out waiting for the writer lock and the
receipt was dropped with ``sqlite3.OperationalError: database is locked``.

This test drives N concurrent ``initialize() + store_unsigned()`` pairs
and asserts that every receipt lands in the DB.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

import pytest

from app.payment.eip712 import Receipt
from app.payment.receipt_store import ReceiptStore


def _mk_receipt() -> Receipt:
    return Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024,
        total_price=1,
    )


@pytest.mark.asyncio
async def test_concurrent_initialize_and_store_no_drops():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "receipts.db"
        store = ReceiptStore(db_path)

        N = 32

        async def one(i: int) -> str:
            # Same call pattern as receipt_submitter.submit():
            # initialize() is called on every submit.
            await store.initialize()
            r = _mk_receipt()
            await store.store_unsigned(r, request_id=f"req-{i}")
            return r.request_uuid

        uuids = await asyncio.gather(*(one(i) for i in range(N)))

        count_unsigned = await store.count_unsigned()
        assert count_unsigned == N, (
            f"expected {N} unsigned rows, got {count_unsigned} — "
            f"SQLite lock contention likely dropped writes"
        )
        assert len(set(uuids)) == N


@pytest.mark.asyncio
async def test_initialize_is_idempotent_after_first_call():
    """After the first call, initialize() must be a no-op write-wise
    so it can be called on the hot path without taking a lock."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "receipts.db"
        store = ReceiptStore(db_path)

        await store.initialize()
        assert store._initialized is True

        # Force a second call — should short-circuit, not touch the DB.
        # If this raised or hung, the concurrency bug would be back.
        await store.initialize()
        await store.initialize()
