"""QA fixture harness — inject known failure states into the provider's
local receipt store so the GUI / CLI surfaces can be exercised end-to-end
without waiting for a real gateway rejection or a real chain revert.

This module is importable so the functionality ships inside the
PyInstaller binary (via ``app/main.py --qa-inject <scenario>``); the
legacy ``scripts/qa_escrow_inject.py`` delegates here to keep both
surfaces in lockstep for source-install users.

Safety: refuses unless ``SR_ALLOW_TEST_FIXTURES=1`` *and* the configured
``SR_ESCROW_CHAIN_RPC`` is not obviously a mainnet endpoint. A
misconfigured invocation on production state would silently corrupt
real receipts, so the guards are explicit and fail closed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path


# Map scenario name → (error_code, which_counter, detail)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "unregistered-node": (
        "SIGN_REJECTED_UNREGISTERED_NODE", "sign",
        "Injected: node not registered in escrow contract.",
    ),
    "byte-mismatch": (
        "SIGN_REJECTED_BYTE_MISMATCH", "sign",
        "Injected: dataAmount 123 exceeds observed 100 by 23 bytes.",
    ),
    "price-cap": (
        "SIGN_REJECTED_PRICE_CAP", "sign",
        "Injected: effective rate exceeds contract cap.",
    ),
    "claim-revert": (
        "CLAIM_REVERTED", "claim",
        "Injected: tx reverted on-chain.",
    ),
    "claim-timeout": (
        "CLAIM_TX_TIMEOUT", "claim",
        "Injected: tx wait timed out.",
    ),
}

# Scenarios not in ``SCENARIOS`` but still valid on the CLI.
META_SCENARIOS = ("lock-now", "clear")

# Scenarios that require the target row to already be signed.
_NEEDS_SIGNED = frozenset({"claim-revert", "claim-timeout", "lock-now"})


class FixtureRefused(Exception):
    """Raised when the safety guards decline to run."""


def all_scenarios() -> list[str]:
    """Canonical list of scenario names for argparse ``choices=``."""
    return list(SCENARIOS.keys()) + list(META_SCENARIOS)


def check_guards(chain_rpc: str | None) -> None:
    """Raises :class:`FixtureRefused` unless it's safe to mutate state."""
    if os.environ.get("SR_ALLOW_TEST_FIXTURES") != "1":
        raise FixtureRefused(
            "SR_ALLOW_TEST_FIXTURES must be set to 1. This harness "
            "mutates the local receipt store and will refuse to run "
            "without the explicit opt-in.",
        )
    rpc = (chain_rpc or "").lower()
    # Creditcoin mainnet RPC hint; refuse on obvious matches.
    if "mainnet" in rpc:
        raise FixtureRefused(
            "SR_ESCROW_CHAIN_RPC appears to target mainnet "
            f"({chain_rpc!r}). QA fixtures must only touch testnet state.",
        )


def resolve_db_path(explicit: str | None = None) -> str:
    """Figure out which ``receipts.db`` to mutate.

    Precedence: explicit arg → ``SR_RECEIPT_STORE_PATH`` env →
    legacy ``~/.spacerouter/receipts.db``. Fails if none exist.
    """
    if explicit:
        return explicit
    env_path = os.environ.get("SR_RECEIPT_STORE_PATH")
    if env_path:
        return env_path
    home_default = Path.home() / ".spacerouter" / "receipts.db"
    if home_default.exists():
        return str(home_default)
    raise FixtureRefused(
        "Could not find a receipts.db: SR_RECEIPT_STORE_PATH not set "
        "and ~/.spacerouter/receipts.db does not exist.",
    )


def _pick_uuid(db_path: str, preferred: str | None, need_signed: bool) -> str:
    if preferred:
        return preferred
    where = "signature IS NOT NULL" if need_signed else "signature IS NULL"
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            f"SELECT request_uuid FROM signed_receipts "
            f"WHERE {where} AND claimed_at IS NULL AND locked = 0 "
            f"ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    if not row:
        raise FixtureRefused(
            f"No suitable receipt found ({'signed' if need_signed else 'unsigned'}). "
            "Let the provider accumulate some traffic first.",
        )
    return row[0]


async def inject(
    scenario: str,
    uuid: str | None = None,
    *,
    db_path: str | None = None,
    chain_rpc: str | None = None,
) -> str:
    """Apply *scenario* to the receipt store; returns the affected uuid.

    Raises :class:`FixtureRefused` on guard failure or missing target row.
    """
    from app.payment.receipt_store import ReceiptStore

    check_guards(chain_rpc if chain_rpc is not None
                 else os.environ.get("SR_ESCROW_CHAIN_RPC", ""))
    db_path = resolve_db_path(db_path)

    if scenario == "clear":
        if not uuid:
            raise FixtureRefused("--uuid is required for scenario 'clear'.")
        with sqlite3.connect(db_path) as c:
            c.execute(
                "UPDATE signed_receipts "
                "   SET last_error_code=NULL, last_error_detail=NULL, "
                "       sign_attempts=0, claim_attempts=0, last_attempt_at=NULL, "
                "       locked=0 "
                " WHERE request_uuid = ?",
                (uuid,),
            )
        return uuid

    if scenario == "lock-now":
        target = _pick_uuid(db_path, uuid, need_signed=True)
        store = ReceiptStore(db_path)
        await store.initialize()
        await store.lock(target)
        return target

    if scenario not in SCENARIOS:
        raise FixtureRefused(f"Unknown scenario: {scenario!r}")

    code, which, detail = SCENARIOS[scenario]
    need_signed = scenario in _NEEDS_SIGNED
    target = _pick_uuid(db_path, uuid, need_signed=need_signed)

    store = ReceiptStore(db_path)
    await store.initialize()
    if which == "sign":
        await store.mark_sign_failed(target, code, detail)
    else:
        await store.mark_claim_failed([target], code, detail)

    if scenario == "claim-timeout":
        # Back-date last_attempt_at so the reaper treats it as past grace.
        with sqlite3.connect(db_path) as c:
            c.execute(
                "UPDATE signed_receipts SET last_attempt_at = ? "
                "WHERE request_uuid = ?",
                (int(time.time()) - 10_000, target),
            )
    return target


def run_cli(scenario: str, uuid: str | None = None) -> int:
    """Thin wrapper for ``app/main.py --qa-inject``. Returns exit code."""
    import asyncio

    try:
        target = asyncio.run(inject(scenario, uuid))
    except FixtureRefused as exc:
        print(f"qa-inject: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI entry
        print(f"qa-inject: unexpected error: {exc}", file=sys.stderr)
        return 1
    print(f"qa-inject: applied {scenario!r} to {target}")
    return 0
