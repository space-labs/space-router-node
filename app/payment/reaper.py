"""Background reaper that resolves stuck ``CLAIM_TX_TIMEOUT`` receipts.

When a claim tx is broadcast but the provider doesn't see confirmation
within the 120s wait window, the row is marked ``CLAIM_TX_TIMEOUT``
without incrementing ``claim_attempts`` — a timeout is ambiguous, the
tx might still land on-chain.

The reaper periodically walks those rows and calls
``isNonceUsed(client, uuid)`` on the escrow contract:

* If the nonce IS used, the tx landed — mark the row claimed with a
  synthetic ``tx_hash="external"``.
* If the nonce is NOT used after ``_SETTLE_GRACE_SECONDS``, the tx
  was dropped from the mempool — clear the error so the next normal
  ``claim_all`` picks it up.

The reaper never increments ``claim_attempts`` — it's a recovery path,
not a retry.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.config import Settings
from app.payment import reasons
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)

# How old a CLAIM_TX_TIMEOUT row must be before the reaper considers it.
# Five minutes is well beyond the longest reasonable mempool delay on
# Creditcoin's block cadence, so a tx that still hasn't confirmed by
# then is either dropped or already included.
_SETTLE_GRACE_SECONDS = int(
    os.environ.get("SR_RECEIPT_REAPER_GRACE_SECONDS", "300"),
)

_REAPER_INTERVAL_SECONDS = int(
    os.environ.get("SR_RECEIPT_REAPER_INTERVAL_SECONDS", "300"),
)


class ClaimReaper:
    """Periodically reconciles timed-out claims with chain state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.ESCROW_CHAIN_RPC
            and self._settings.ESCROW_CONTRACT_ADDRESS
        )

    async def start(self) -> None:
        if self._task is not None or not self.enabled:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Claim reaper started (interval=%ds, grace=%ds)",
            _REAPER_INTERVAL_SECONDS, _SETTLE_GRACE_SECONDS,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        # Run once at startup so a crash-and-restart resolves in-flight
        # timeouts immediately instead of waiting a full interval.
        try:
            await self.tick()
        except Exception:
            logger.exception("Claim reaper initial tick failed")

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=_REAPER_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self.tick()
            except Exception:
                logger.exception("Claim reaper tick failed")

    async def tick(self) -> dict:
        """Single reaper pass. Returns a summary dict for tests / CLI."""
        store = get_store(self._settings.RECEIPT_STORE_PATH)
        await store.initialize()

        rows = await store.list_timed_out_claims(
            older_than_seconds=_SETTLE_GRACE_SECONDS,
        )
        if not rows:
            return {"checked": 0, "reconciled": 0, "cleared": 0}

        def _check(rs) -> tuple[list[str], list[str]]:
            from web3 import Web3
            from eth_utils import to_checksum_address

            w3 = Web3(Web3.HTTPProvider(
                self._settings.ESCROW_CHAIN_RPC,
                request_kwargs={"timeout": 10},
            ))
            if not w3.is_connected():
                return [], []
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(
                    self._settings.ESCROW_CONTRACT_ADDRESS,
                ),
                abi=_load_abi_once(),
            )
            landed: list[str] = []
            dropped: list[str] = []
            for sr in rs:
                try:
                    used = contract.functions.isNonceUsed(
                        to_checksum_address(sr.receipt.client_address),
                        sr.receipt.request_uuid,
                    ).call()
                except Exception as e:
                    logger.debug(
                        "isNonceUsed failed for uuid=%s: %s",
                        sr.receipt.request_uuid, e,
                    )
                    continue
                if used:
                    landed.append(sr.receipt.request_uuid)
                else:
                    dropped.append(sr.receipt.request_uuid)
            return landed, dropped

        landed, dropped = await asyncio.to_thread(_check, rows)

        reconciled = 0
        if landed:
            reconciled = await store.mark_claimed(landed, tx_hash="external")
            logger.info(
                "Reaper: reconciled %d timed-out claims as landed", reconciled,
            )

        cleared = 0
        for u in dropped:
            if await store.clear_error(u):
                cleared += 1
        if cleared:
            logger.info(
                "Reaper: cleared %d dropped-tx timeouts back to claimable",
                cleared,
            )

        return {"checked": len(rows), "reconciled": reconciled, "cleared": cleared}


_abi_cache: list[dict] | None = None


def _load_abi_once() -> list[dict]:
    global _abi_cache
    if _abi_cache is not None:
        return _abi_cache
    from app.payment.settlement import _load_abi
    _abi_cache = _load_abi()
    return _abi_cache
