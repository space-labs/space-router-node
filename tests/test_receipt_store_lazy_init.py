"""Regression: GUI/CLI read paths must not raise on a fresh DB.

The test.95 receipt bug: escrow.enabled stayed false after Fresh
Restart, so the receipt submitter never instantiated, the SQLite
schema was never bootstrapped, and the GUI's polling Earnings card
hammered ``store.summary()`` straight against an empty DB —
``OperationalError: no such table: signed_receipts`` every probe tick.

Lazy-initialize on read methods so a fresh-or-empty DB returns sane
zero-data instead of raising.
"""

from __future__ import annotations

import pytest

from app.payment.receipt_store import ReceiptStore


@pytest.fixture
def fresh_store(tmp_path):
    return ReceiptStore(tmp_path / "receipts.db")


@pytest.mark.asyncio
async def test_summary_on_uninitialized_store_returns_zeros(fresh_store):
    """summary() on a never-touched DB used to raise; now it bootstraps
    the schema and returns the empty rollup."""
    summary = await fresh_store.summary()
    assert summary == {
        "claimed": 0,
        "failed_terminal": 0,
        "claimable": 0,
        "failed_retryable": 0,
        "pending_sign": 0,
        "claimable_total_price": 0,
    }


@pytest.mark.asyncio
async def test_list_by_view_on_uninitialized_store_returns_empty(fresh_store):
    rows = await fresh_store.list_by_view(view="all")
    assert rows == []


@pytest.mark.asyncio
async def test_get_by_uuid_on_uninitialized_store_returns_none(fresh_store):
    row = await fresh_store.get_by_uuid("00000000-0000-0000-0000-000000000001")
    assert row is None


@pytest.mark.asyncio
async def test_list_recently_claimed_on_uninitialized_store_returns_empty(fresh_store):
    rows = await fresh_store.list_recently_claimed(younger_than_seconds=3600)
    assert rows == []


@pytest.mark.asyncio
async def test_initialize_is_idempotent_via_lazy_callers(fresh_store, monkeypatch):
    """Repeated read calls trigger the short-circuit on subsequent
    `initialize()` invocations — confirms we're not paying schema cost
    on every GUI probe tick."""
    real_initialize = fresh_store.initialize
    call_count = {"n": 0}

    async def counted_initialize():
        call_count["n"] += 1
        await real_initialize()

    monkeypatch.setattr(fresh_store, "initialize", counted_initialize)

    await fresh_store.summary()
    await fresh_store.summary()
    await fresh_store.list_by_view(view="all")

    # initialize is called from every read but its body short-circuits
    # via self._initialized — so we count invocations, not work done.
    assert call_count["n"] == 3
    assert fresh_store._initialized is True
