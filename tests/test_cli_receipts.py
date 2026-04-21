"""PR 3: CLI --receipts / --claim extensions — JSON schema, rich output,
failed-only filtering, single-uuid claim guard."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.payment import reasons
from app.payment.eip712 import Receipt
from app.payment.receipt_store import get_store


def _mk_receipt() -> Receipt:
    return Receipt(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024 * 1024,
        total_price=1_000_000_000_000_000,
    )


@pytest.fixture
def seeded_store(tmp_path, monkeypatch):
    """Create a store with one of each view bucket."""
    db = tmp_path / "r.db"
    monkeypatch.setenv("SR_RECEIPT_STORE_PATH", str(db))

    async def _seed():
        store = get_store(str(db))
        await store.initialize()

        claimable = _mk_receipt()
        pending = _mk_receipt()
        retry = _mk_receipt()
        locked = _mk_receipt()
        claimed = _mk_receipt()

        await store.store(claimable, signature="0xsig1")
        await store.store_unsigned(pending, request_id="rp")
        await store.store(retry, signature="0xsig3")
        await store.mark_claim_failed(
            [retry.request_uuid], reasons.CLAIM_REVERTED, "tx 0xdead",
        )
        await store.store(locked, signature="0xsig4")
        await store.lock(locked.request_uuid)
        await store.store(claimed, signature="0xsig5")
        await store.mark_claimed([claimed.request_uuid], "0xtx")

        return {
            "claimable": claimable,
            "pending": pending,
            "retry": retry,
            "locked": locked,
            "claimed": claimed,
        }

    return db, asyncio.run(_seed())


@pytest.mark.asyncio
async def test_cmd_receipts_json_contains_all_views(seeded_store, capsys, monkeypatch):
    from app.main import _cmd_receipts
    db, seeded = seeded_store

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = ""
        ESCROW_CONTRACT_ADDRESS = ""

    with patch("app.main.load_settings", return_value=FakeSettings()):
        await _cmd_receipts(as_json=True)

    out = capsys.readouterr().out
    payload = json.loads(out)

    assert payload["summary"]["claimable"] == 1
    assert payload["summary"]["failed_retryable"] == 1
    assert payload["summary"]["failed_terminal"] == 1
    assert payload["summary"]["pending_sign"] == 1
    assert payload["summary"]["claimed"] == 1

    # Claimed rows aren't in the default listing (UX focus is on
    # actionable state), but the summary counts them.
    views_in_list = {r["view"] for r in payload["receipts"]}
    assert views_in_list == {
        "claimable", "pending_sign", "failed_retryable", "failed_terminal",
    }

    # Schema stability: every receipt has these keys.
    required = {
        "request_uuid", "client_address", "node_address", "data_amount",
        "total_price", "view", "signature_present", "created_at",
        "sign_attempts", "claim_attempts", "max_sign_attempts",
        "max_claim_attempts", "last_error_code", "last_error_message",
        "locked",
    }
    for r in payload["receipts"]:
        assert required <= set(r.keys())


@pytest.mark.asyncio
async def test_cmd_receipts_failed_only_filters(seeded_store, capsys):
    from app.main import _cmd_receipts
    db, seeded = seeded_store

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = ""
        ESCROW_CONTRACT_ADDRESS = ""

    with patch("app.main.load_settings", return_value=FakeSettings()):
        await _cmd_receipts(failed_only=True, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    views = {r["view"] for r in payload["receipts"]}
    assert views == {"failed_retryable", "failed_terminal"}


@pytest.mark.asyncio
async def test_cmd_receipts_default_preserves_pre_v1_5_happy_path(tmp_path, capsys, monkeypatch):
    """With no failures anywhere, default output must not spam new
    columns — validates the "don't break the UX" promise."""
    from app.main import _cmd_receipts
    db = tmp_path / "r.db"

    store = get_store(str(db))
    await store.initialize()
    await store.store(_mk_receipt(), signature="0xsig")

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = ""
        ESCROW_CONTRACT_ADDRESS = ""

    with patch("app.main.load_settings", return_value=FakeSettings()):
        await _cmd_receipts()

    out = capsys.readouterr().out
    # No "Needs attention" line when there are no failures.
    assert "Needs attention" not in out
    assert "Claimable: 1 receipt" in out


@pytest.mark.asyncio
async def test_cmd_claim_refuses_locked_uuid(seeded_store, capsys):
    from app.main import _cmd_claim
    db, seeded = seeded_store

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0xe"
        CLAIM_BATCH_SIZE = 50
        ESCROW_CHAIN_ID = 102031

    with patch("app.main.load_settings", return_value=FakeSettings()):
        with pytest.raises(SystemExit) as exc:
            await _cmd_claim(only_uuid=seeded["locked"].request_uuid)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "locked" in err.lower()


@pytest.mark.asyncio
async def test_cmd_claim_rejects_unknown_uuid(seeded_store, capsys):
    from app.main import _cmd_claim
    db, _ = seeded_store

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0xe"
        CLAIM_BATCH_SIZE = 50
        ESCROW_CHAIN_ID = 102031

    with patch("app.main.load_settings", return_value=FakeSettings()):
        with pytest.raises(SystemExit) as exc:
            await _cmd_claim(only_uuid="00000000-0000-0000-0000-000000000000")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "No receipt found" in err


def test_argparse_new_flags_present():
    """Smoke test: argparse accepts the new flags without errors."""
    from app.main import _build_arg_parser

    parser = _build_arg_parser()
    # Should parse without complaint.
    args = parser.parse_args([
        "--receipts", "--failed", "--json", "--reap",
    ])
    assert args.receipts and args.failed and args.output_json and args.reap

    args = parser.parse_args([
        "--claim", "--include-retryable", "--uuid", "abc-123",
    ])
    assert args.claim and args.include_retryable and args.uuid == "abc-123"
