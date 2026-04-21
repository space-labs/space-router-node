"""On-chain settlement of locally-stored Leg 2 receipts.

Called from the ``claim`` CLI command. Reads unclaimed receipts from the
local SQLite store and submits them to ``TokenPaymentEscrow.claimBatch()``
in batches. The contract silently skips any receipt whose client has
insufficient balance / whose nonce is already used / whose node is not
registered — so we mark a batch as claimed only if the tx confirms.

Web3 calls run in ``asyncio.to_thread`` (web3.py is sync).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.payment import reasons
from app.payment.receipt_store import ReceiptStore, StoredReceipt, get_store

logger = logging.getLogger(__name__)

_ABI_PATH = Path(__file__).parent / "escrow_abi.json"


@dataclass
class ClaimResult:
    submitted: int
    tx_hash: str | None
    gas_used: int | None
    error: str | None = None
    reason_code: str | None = None
    skipped_as_already_claimed: int = 0
    locked_after_failure: int = 0


def _load_abi() -> list[dict]:
    """The bundled abi file is ``{"escrow": [...], "erc20": [...]}``; extract escrow."""
    with open(_ABI_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data["escrow"]
    return data  # already a flat list


def _to_contract_tuple(sr: StoredReceipt) -> tuple:
    """Convert a StoredReceipt to the tuple format the contract expects."""
    r = sr.receipt
    from eth_utils import to_bytes, to_checksum_address

    node_bytes = to_bytes(hexstr=r.node_address)
    return (
        to_checksum_address(r.client_address),
        node_bytes,
        r.request_uuid,
        int(r.data_amount),
        int(r.total_price),
    )


async def claim_all(
    settings: Settings,
    settlement_key: str,
    include_retryable: bool = False,
    only_uuids: list[str] | None = None,
) -> list[ClaimResult]:
    """Submit outstanding receipts in ``settings.CLAIM_BATCH_SIZE`` chunks.

    ``include_retryable=True`` picks up rows that previously hit
    ``CLAIM_REVERTED`` and are still under the attempt cap — used by
    explicit retry flows. Default behaviour (fresh claims only) matches
    pre-v1.5 semantics so ``--claim`` in a scheduled cron never snowballs
    into retry storms on terminally broken receipts.

    ``only_uuids`` restricts the claim to a specific set (single-receipt
    retry from the GUI / CLI).

    Returns one :class:`ClaimResult` per attempted batch. Unlike the
    pre-v1.5 behaviour this does NOT short-circuit after the first bad
    batch — a reverted batch records its failure, then the loop advances
    to the next batch so one bad receipt can't block unrelated ones.
    """
    if not settings.ESCROW_CONTRACT_ADDRESS or not settings.ESCROW_CHAIN_RPC:
        raise ValueError(
            "Claim requires SR_ESCROW_CONTRACT_ADDRESS and SR_ESCROW_CHAIN_RPC "
            "to be set (either in .env or environment)."
        )

    store = get_store(settings.RECEIPT_STORE_PATH)
    await store.initialize()

    # Pre-claim: any receipt whose nonce is already used on-chain was
    # settled out-of-band (another settler, earlier crashed run). Mark
    # them claimed with a synthetic tx hash so they don't re-enter the
    # claim batch and force a guaranteed revert.
    pre_claimed = await _reconcile_already_claimed(settings, store)

    results: list[ClaimResult] = []
    seen_uuids: set[str] = set()
    if pre_claimed:
        results.append(ClaimResult(
            submitted=0, tx_hash=None, gas_used=None,
            skipped_as_already_claimed=pre_claimed,
        ))

    while True:
        batch = await _next_batch(
            store, settings.CLAIM_BATCH_SIZE,
            include_retryable=include_retryable, only_uuids=only_uuids,
        )
        # Strip anything we already processed this run (defensive guard
        # against an idempotency gap where a batch appears twice — e.g.
        # a reverted batch that never transitioned to locked because
        # attempts was already at cap, which can't actually happen but
        # the guard is cheap).
        batch = [sr for sr in batch if sr.receipt.request_uuid not in seen_uuids]
        if not batch:
            break
        seen_uuids.update(sr.receipt.request_uuid for sr in batch)

        result = await _submit_batch(settings, settlement_key, batch, store)
        results.append(result)
        # Only RPC-unreachable stops the whole run — every other failure
        # (revert, timeout) is a per-batch outcome and we continue.
        if result.reason_code == reasons.CLAIM_RPC_UNREACHABLE:
            break

    return results


async def _next_batch(
    store: ReceiptStore,
    batch_size: int,
    include_retryable: bool,
    only_uuids: list[str] | None,
) -> list[StoredReceipt]:
    if only_uuids:
        picked: list[StoredReceipt] = []
        for uuid_str in only_uuids:
            sr = await store.get_by_uuid(uuid_str)
            if sr and sr.view in ("claimable", "failed_retryable"):
                picked.append(sr)
        return picked[:batch_size]
    return await store.unclaimed(
        limit=batch_size, include_retryable=include_retryable,
    )


async def _reconcile_already_claimed(
    settings: Settings, store: ReceiptStore,
) -> int:
    """Mark locally-pending receipts as claimed if the chain already knows them.

    Cheap per-row ``isNonceUsed(client, uuid)`` call — only runs on rows
    currently in the claim queue, so it's bounded by the batch size, not
    the full history.
    """
    candidates = await store.unclaimed(limit=settings.CLAIM_BATCH_SIZE,
                                       include_retryable=True)
    if not candidates:
        return 0

    def _check() -> list[str]:
        from web3 import Web3
        from eth_utils import to_checksum_address

        w3 = Web3(Web3.HTTPProvider(
            settings.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 10},
        ))
        if not w3.is_connected():
            return []
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
            abi=_load_abi(),
        )
        already: list[str] = []
        for sr in candidates:
            try:
                used = contract.functions.isNonceUsed(
                    to_checksum_address(sr.receipt.client_address),
                    sr.receipt.request_uuid,
                ).call()
            except Exception:
                continue
            if used:
                already.append(sr.receipt.request_uuid)
        return already

    already = await asyncio.to_thread(_check)
    if not already:
        return 0

    marked = await store.mark_claimed(already, tx_hash="external")
    if marked:
        logger.info(
            "Reconciled %d receipt(s) as already-claimed on-chain", marked,
        )
    return marked


async def _submit_batch(
    settings: Settings,
    settlement_key: str,
    batch: list[StoredReceipt],
    store: ReceiptStore,
) -> ClaimResult:
    """Submit one batch on-chain and mark claimed on confirmation.

    Returns a :class:`ClaimResult` tagged with a reason code on failure
    so the caller can distinguish retry-worthy from fatal outcomes.
    Failures always propagate to the store: ``CLAIM_REVERTED`` /
    ``CLAIM_TX_TIMEOUT`` increment ``claim_attempts`` and may lock rows
    at the attempt cap; ``CLAIM_RPC_UNREACHABLE`` is silent (transient).
    """
    def _do() -> ClaimResult:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(
            settings.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 30},
        ))
        if not w3.is_connected():
            return ClaimResult(
                submitted=len(batch), tx_hash=None, gas_used=None,
                error=f"RPC unreachable: {settings.ESCROW_CHAIN_RPC}",
                reason_code=reasons.CLAIM_RPC_UNREACHABLE,
            )

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
            abi=_load_abi(),
        )
        account = Account.from_key(settlement_key)

        receipts_tuples = [_to_contract_tuple(sr) for sr in batch]
        signatures = [bytes.fromhex(sr.signature.removeprefix("0x")) for sr in batch]

        GAS_CAP = 12_000_000
        try:
            gas_estimate = contract.functions.claimBatch(
                receipts_tuples, signatures,
            ).estimate_gas({"from": account.address})
            gas_limit = min(int(gas_estimate * 1.2), GAS_CAP)
        except Exception as e:
            gas_limit = min(350_000 * len(batch), GAS_CAP)
            logger.warning("Gas estimation failed (%s); falling back to %d", e, gas_limit)

        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.claimBatch(receipts_tuples, signatures).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
            "chainId": settings.ESCROW_CHAIN_ID,
        })
        signed = account.sign_transaction(tx)
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:
            # Pre-confirmation failures (malformed tx, connection drop during
            # broadcast) — treat as transient RPC issue.
            return ClaimResult(
                submitted=len(batch), tx_hash=None, gas_used=None,
                error=f"broadcast failed: {e}",
                reason_code=reasons.CLAIM_RPC_UNREACHABLE,
            )

        tx_hex = tx_hash.hex()
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception as e:
            # The tx may still land — timeout is ambiguous. Store the
            # hash and let the reaper resolve via isNonceUsed on the
            # next tick.
            return ClaimResult(
                submitted=len(batch), tx_hash=tx_hex, gas_used=None,
                error=f"tx wait timed out: {e}",
                reason_code=reasons.CLAIM_TX_TIMEOUT,
            )

        if rcpt.status != 1:
            return ClaimResult(
                submitted=len(batch), tx_hash=tx_hex,
                gas_used=rcpt.gasUsed, error="tx reverted",
                reason_code=reasons.CLAIM_REVERTED,
            )

        return ClaimResult(
            submitted=len(batch), tx_hash=tx_hex, gas_used=rcpt.gasUsed,
        )

    result = await asyncio.to_thread(_do)
    uuids = [sr.receipt.request_uuid for sr in batch]

    if result.tx_hash and not result.error:
        marked = await store.mark_claimed(uuids, result.tx_hash)
        logger.info(
            "Settled %d receipts in tx %s (gas=%s)",
            marked, result.tx_hash, result.gas_used,
        )
        return result

    if result.reason_code:
        # Record the failure on every row in the batch. mark_claim_failed
        # handles the "transient doesn't count" rule internally.
        detail = result.tx_hash or result.error
        await store.mark_claim_failed(uuids, result.reason_code, detail)

        if reasons.counts_against_retry_budget(result.reason_code):
            # Count how many of these rows are now locked — the caller
            # surfaces this in the CLI summary so the user sees exactly
            # what just became terminal.
            locked_now = 0
            for u in uuids:
                sr = await store.get_by_uuid(u)
                if sr and sr.locked:
                    locked_now += 1
            result.locked_after_failure = locked_now

    logger.error(
        "Batch settlement failed reason=%s tx=%s detail=%s",
        result.reason_code, result.tx_hash, result.error,
    )
    return result


async def list_unclaimed(settings: Settings) -> tuple[int, int, list[StoredReceipt]]:
    """Return (count, total_price_wei, first 50 unclaimed receipts) for display."""
    store = get_store(settings.RECEIPT_STORE_PATH)
    await store.initialize()
    count, total = await store.count_unclaimed()
    preview = await store.unclaimed(limit=50)
    return count, total, preview
