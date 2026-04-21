#!/usr/bin/env python3
"""QA fixture harness — inject known failure states into a provider's
receipt store so the GUI / CLI surfaces can be exercised end-to-end
without waiting for a real gateway rejection or a real chain revert.

Usage:
    SR_RECEIPT_STORE_PATH=~/.spacerouter/receipts.db \\
    SR_ALLOW_TEST_FIXTURES=1 \\
    python scripts/qa_escrow_inject.py --scenario <name> [--uuid <uuid>]

Scenarios:
  unregistered-node     Sign-side: SIGN_REJECTED_UNREGISTERED_NODE
  byte-mismatch         Sign-side: SIGN_REJECTED_BYTE_MISMATCH
  price-cap             Sign-side: SIGN_REJECTED_PRICE_CAP
  claim-revert          Claim-side: CLAIM_REVERTED
  claim-timeout         Claim-side: CLAIM_TX_TIMEOUT (past grace)
  lock-now              Force a row into failed_terminal
  clear                 Clear test-injected state on a uuid

Safety: refuses unless SR_ALLOW_TEST_FIXTURES=1 is set AND
SR_ESCROW_CHAIN_RPC points at a testnet RPC (chainId != 102030 =
mainnet). A misconfigured invocation on production state is a nasty
bug magnet, so the guards are explicit.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make the provider's ``app`` package importable when this script is run
# directly (systemd / manual invocation from /root/space-router-node).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Map CLI scenario name → (error_code, which_counter, detail)
SCENARIOS = {
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


def _guard_environment(chain_rpc: str) -> None:
    if os.environ.get("SR_ALLOW_TEST_FIXTURES") != "1":
        sys.exit(
            "Refusing to run: SR_ALLOW_TEST_FIXTURES must be set to 1. "
            "This script mutates the local receipt store.",
        )
    rpc = (chain_rpc or "").lower()
    # Creditcoin mainnet RPC; refuse if it's set.
    if "mainnet" in rpc:
        sys.exit(
            "Refusing to run: SR_ESCROW_CHAIN_RPC appears to be mainnet. "
            "QA fixtures must only touch testnet state.",
        )


def _pick_uuid(db_path: str, preferred: str | None, need_signed: bool) -> str:
    """Pick a uuid to mutate — the given one if set, else the oldest row
    that matches the scenario's signature requirement."""
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
        sys.exit(
            f"No suitable receipt found ({'signed' if need_signed else 'unsigned'}). "
            f"Let the provider accumulate some traffic first."
        )
    return row[0]


async def _inject(db_path: str, uuid: str, scenario: str) -> None:
    from app.payment.receipt_store import ReceiptStore

    if scenario == "clear":
        with sqlite3.connect(db_path) as c:
            c.execute(
                "UPDATE signed_receipts "
                "   SET last_error_code=NULL, last_error_detail=NULL, "
                "       sign_attempts=0, claim_attempts=0, last_attempt_at=NULL, "
                "       locked=0 "
                " WHERE request_uuid = ?",
                (uuid,),
            )
        print(f"cleared fixture state on {uuid}")
        return

    if scenario == "lock-now":
        store = ReceiptStore(db_path)
        await store.initialize()
        ok = await store.lock(uuid)
        print(f"lock({uuid}) → {ok}")
        return

    if scenario not in SCENARIOS:
        sys.exit(f"Unknown scenario: {scenario!r}")

    code, which, detail = SCENARIOS[scenario]
    store = ReceiptStore(db_path)
    await store.initialize()

    if which == "sign":
        ok = await store.mark_sign_failed(uuid, code, detail)
    else:
        ok = await store.mark_claim_failed([uuid], code, detail)
    print(f"injected {scenario} ({code}) on {uuid} → updated={ok}")

    if scenario == "claim-timeout":
        # Back-date last_attempt_at so the reaper treats it as past grace.
        with sqlite3.connect(db_path) as c:
            c.execute(
                "UPDATE signed_receipts SET last_attempt_at = ? WHERE request_uuid = ?",
                (int(time.time()) - 10_000, uuid),
            )
        print(
            f"back-dated last_attempt_at so reaper's 300s grace has elapsed"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", required=True,
        choices=list(SCENARIOS.keys()) + ["lock-now", "clear"],
        help="Failure scenario to inject.",
    )
    parser.add_argument(
        "--uuid",
        help=(
            "Target receipt UUID. Defaults to the oldest row matching "
            "the scenario's signature requirement."
        ),
    )
    args = parser.parse_args()

    db_path = os.environ.get("SR_RECEIPT_STORE_PATH")
    if not db_path:
        home_default = Path.home() / ".spacerouter" / "receipts.db"
        if home_default.exists():
            db_path = str(home_default)
        else:
            sys.exit(
                "SR_RECEIPT_STORE_PATH not set and no ~/.spacerouter/"
                "receipts.db found.",
            )

    _guard_environment(os.environ.get("SR_ESCROW_CHAIN_RPC", ""))

    # Scenarios that need a signed receipt vs those that work on any row.
    needs_signed = args.scenario in ("claim-revert", "claim-timeout", "lock-now")
    uuid = _pick_uuid(db_path, args.uuid, need_signed=needs_signed) \
        if args.scenario != "clear" \
        else (args.uuid or sys.exit("--uuid required for --scenario clear"))

    asyncio.run(_inject(db_path, uuid, args.scenario))


if __name__ == "__main__":
    main()
