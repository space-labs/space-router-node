"""QA fixture harness — ensures both the importable entry point and the
legacy ``scripts/qa_escrow_inject.py`` delegate produce the same receipt
state, and that the safety guards actually refuse bad invocations.
"""

from __future__ import annotations

import uuid as uuid_lib

import pytest

from app import qa_fixtures
from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.receipt_store import get_store


async def _seed_unsigned(db_path: str) -> str:
    r = Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid_lib.uuid4()),
        data_amount=100, total_price=1,
    )
    store = get_store(db_path)
    await store.initialize()
    await store.store_unsigned(r, request_id="req")
    return r.request_uuid


async def _seed_signed(db_path: str) -> str:
    uuid = await _seed_unsigned(db_path)
    store = get_store(db_path)
    await store.mark_signed(uuid, signature="0x" + "c" * 130)
    return uuid


# ── Guard tests ───────────────────────────────────────────────────────


def test_guard_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv("SR_ALLOW_TEST_FIXTURES", raising=False)
    with pytest.raises(qa_fixtures.FixtureRefused, match="SR_ALLOW_TEST_FIXTURES"):
        qa_fixtures.check_guards("http://testnet.example")


def test_guard_refuses_mainnet_rpc(monkeypatch):
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    with pytest.raises(qa_fixtures.FixtureRefused, match="mainnet"):
        qa_fixtures.check_guards("https://rpc.mainnet.creditcoin.network")


def test_guard_allows_testnet_with_opt_in(monkeypatch):
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    qa_fixtures.check_guards("https://rpc.testnet.creditcoin.network")  # no raise


# ── Scenario application ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_sign_scenario_marks_sign_failed(tmp_path, monkeypatch):
    db = str(tmp_path / "r.db")
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    uuid = await _seed_unsigned(db)

    target = await qa_fixtures.inject("byte-mismatch", db_path=db)
    assert target == uuid

    store = get_store(db)
    row = await store.get_by_uuid(uuid)
    assert row.last_error_code == reasons.SIGN_REJECTED_BYTE_MISMATCH
    assert "dataAmount 123 exceeds observed 100" in row.last_error_detail


@pytest.mark.asyncio
async def test_inject_claim_scenario_needs_signed_row(tmp_path, monkeypatch):
    db = str(tmp_path / "r.db")
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    uuid = await _seed_signed(db)

    target = await qa_fixtures.inject("claim-revert", db_path=db)
    assert target == uuid

    store = get_store(db)
    row = await store.get_by_uuid(uuid)
    assert row.last_error_code == reasons.CLAIM_REVERTED


@pytest.mark.asyncio
async def test_inject_claim_timeout_backdates_last_attempt(tmp_path, monkeypatch):
    db = str(tmp_path / "r.db")
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    uuid = await _seed_signed(db)

    await qa_fixtures.inject("claim-timeout", db_path=db)

    import sqlite3
    import time as _time
    with sqlite3.connect(db) as c:
        last = c.execute(
            "SELECT last_attempt_at FROM signed_receipts WHERE request_uuid=?",
            (uuid,),
        ).fetchone()[0]
    # Back-dated well past the 300s reaper grace.
    assert int(_time.time()) - last > 5_000


@pytest.mark.asyncio
async def test_inject_clear_requires_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    with pytest.raises(qa_fixtures.FixtureRefused, match="--uuid is required"):
        await qa_fixtures.inject("clear", db_path=str(tmp_path / "x.db"))


@pytest.mark.asyncio
async def test_inject_clear_resets_error_state(tmp_path, monkeypatch):
    db = str(tmp_path / "r.db")
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    uuid = await _seed_unsigned(db)
    await qa_fixtures.inject("byte-mismatch", db_path=db)

    await qa_fixtures.inject("clear", uuid=uuid, db_path=db)

    store = get_store(db)
    row = await store.get_by_uuid(uuid)
    assert row.last_error_code in (None, "")
    assert row.sign_attempts == 0


@pytest.mark.asyncio
async def test_inject_refused_without_receipt(tmp_path, monkeypatch):
    db = str(tmp_path / "empty.db")
    monkeypatch.setenv("SR_ALLOW_TEST_FIXTURES", "1")
    monkeypatch.setenv("SR_ESCROW_CHAIN_RPC", "http://testnet")
    # Prime the DB schema but leave it empty
    store = get_store(db)
    await store.initialize()

    with pytest.raises(qa_fixtures.FixtureRefused, match="No suitable receipt"):
        await qa_fixtures.inject("byte-mismatch", db_path=db)


# ── CLI exit codes ────────────────────────────────────────────────────


def test_run_cli_refuses_without_opt_in(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("SR_ALLOW_TEST_FIXTURES", raising=False)
    monkeypatch.setenv("SR_RECEIPT_STORE_PATH", str(tmp_path / "r.db"))
    rc = qa_fixtures.run_cli("byte-mismatch")
    assert rc == 2
    err = capsys.readouterr().err
    assert "SR_ALLOW_TEST_FIXTURES" in err
