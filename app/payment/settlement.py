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
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.payment.receipt_store import ReceiptStore, StoredReceipt, get_store

logger = logging.getLogger(__name__)

_ABI_PATH = Path(__file__).parent / "escrow_abi.json"


@dataclass
class ClaimResult:
    submitted: int
    tx_hash: str | None
    gas_used: int | None
    error: str | None = None


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


async def claim_all(settings: Settings, settlement_key: str) -> list[ClaimResult]:
    """Submit all unclaimed receipts in ``settings.CLAIM_BATCH_SIZE`` chunks.

    Returns one ClaimResult per attempted batch. Successful batches have
    their receipts marked as claimed in the local store.
    """
    if not settings.ESCROW_CONTRACT_ADDRESS or not settings.ESCROW_CHAIN_RPC:
        raise ValueError(
            "Claim requires SR_ESCROW_CONTRACT_ADDRESS and SR_ESCROW_CHAIN_RPC "
            "to be set (either in .env or environment)."
        )

    store = get_store(settings.RECEIPT_STORE_PATH)
    await store.initialize()

    results: list[ClaimResult] = []
    while True:
        batch = await store.unclaimed(limit=settings.CLAIM_BATCH_SIZE)
        if not batch:
            break

        result = await _submit_batch(settings, settlement_key, batch, store)
        results.append(result)
        if result.error:
            break  # stop on first failure so we don't retry a failing batch in a loop

    return results


async def _submit_batch(
    settings: Settings,
    settlement_key: str,
    batch: list[StoredReceipt],
    store: ReceiptStore,
) -> ClaimResult:
    """Submit one batch on-chain and mark claimed on confirmation."""
    def _do() -> ClaimResult:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(settings.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 30}))
        if not w3.is_connected():
            return ClaimResult(
                submitted=len(batch), tx_hash=None, gas_used=None,
                error=f"RPC unreachable: {settings.ESCROW_CHAIN_RPC}",
            )

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
            abi=_load_abi(),
        )
        account = Account.from_key(settlement_key)

        receipts_tuples = [_to_contract_tuple(sr) for sr in batch]
        signatures = [bytes.fromhex(sr.signature.removeprefix("0x")) for sr in batch]

        try:
            # Estimate with headroom; contract ~330K gas per receipt worst case
            gas_estimate = contract.functions.claimBatch(
                receipts_tuples, signatures,
            ).estimate_gas({"from": account.address})
            gas_limit = int(gas_estimate * 1.2)
        except Exception as e:
            gas_limit = 350_000 * len(batch)
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
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            return ClaimResult(
                submitted=len(batch), tx_hash=tx_hash.hex(),
                gas_used=receipt.gasUsed, error="tx reverted",
            )

        return ClaimResult(
            submitted=len(batch), tx_hash=tx_hash.hex(), gas_used=receipt.gasUsed,
        )

    result = await asyncio.to_thread(_do)
    if result.tx_hash and not result.error:
        marked = await store.mark_claimed(
            [sr.receipt.request_uuid for sr in batch], result.tx_hash,
        )
        logger.info(
            "Settled %d receipts in tx %s (gas=%s)",
            marked, result.tx_hash, result.gas_used,
        )
    elif result.error:
        logger.error("Batch settlement failed: %s", result.error)

    return result


async def list_unclaimed(settings: Settings) -> tuple[int, int, list[StoredReceipt]]:
    """Return (count, total_price_wei, first 50 unclaimed receipts) for display."""
    store = get_store(settings.RECEIPT_STORE_PATH)
    await store.initialize()
    count, total = await store.count_unclaimed()
    preview = await store.unclaimed(limit=50)
    return count, total, preview
