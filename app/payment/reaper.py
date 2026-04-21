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

# Reorg reconciliation: how far back to re-verify claimed rows.
# Creditcoin finality is short — 30 minutes is comfortably past even a
# deep reorg. Reads one isNonceUsed per row, rate-limited by limit.
_REORG_LOOKBACK_SECONDS = int(
    os.environ.get("SR_RECEIPT_REORG_LOOKBACK_SECONDS", "1800"),
)
_REORG_BATCH_LIMIT = int(
    os.environ.get("SR_RECEIPT_REORG_BATCH_LIMIT", "50"),
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
        """Single reaper pass. Returns a summary dict for tests / CLI.

        Two passes:

        1. **Timeout resolution** — for rows stuck in ``CLAIM_TX_TIMEOUT``,
           query ``isNonceUsed`` and either mark claimed or clear the
           error.
        2. **Reorg reconciliation** — for rows recently marked claimed
           with a real tx_hash, re-verify the nonce still shows used.
           A chain reorg that undid the tx is rare on Creditcoin but
           possible; if we detect it, undo the claim so the row
           re-enters the queue on the next ``--claim``.
        """
        store = get_store(self._settings.RECEIPT_STORE_PATH)
        await store.initialize()

        timeout_summary = await self._tick_timeouts(store)
        reorg_summary = await self._tick_reorgs(store)
        return {**timeout_summary, **reorg_summary}

    async def _tick_timeouts(self, store) -> dict:
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

    async def _tick_reorgs(self, store) -> dict:
        """Verify recently-claimed rows still show as settled on-chain.

        A chain reorg that un-does our claim tx leaves the local DB
        saying "claimed" while ``isNonceUsed`` returns false. On
        Creditcoin reorgs are rare and shallow, so this is mostly a
        defensive measure. When we do detect drift, we ``revert_claimed``
        the row so ``--claim`` picks it up again.

        Reads capped at ``_REORG_BATCH_LIMIT`` per tick to bound RPC load.
        """
        rows = await store.list_recently_claimed(
            younger_than_seconds=_REORG_LOOKBACK_SECONDS,
            limit=_REORG_BATCH_LIMIT,
        )
        if not rows:
            return {"reorg_checked": 0, "reorg_reverted": 0}

        def _check(rs) -> list[str]:
            from web3 import Web3
            from eth_utils import to_checksum_address

            w3 = Web3(Web3.HTTPProvider(
                self._settings.ESCROW_CHAIN_RPC,
                request_kwargs={"timeout": 10},
            ))
            if not w3.is_connected():
                return []
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(
                    self._settings.ESCROW_CONTRACT_ADDRESS,
                ),
                abi=_load_abi_once(),
            )
            reverted: list[str] = []
            for sr in rs:
                try:
                    used = contract.functions.isNonceUsed(
                        to_checksum_address(sr.receipt.client_address),
                        sr.receipt.request_uuid,
                    ).call()
                except Exception as e:
                    logger.debug(
                        "isNonceUsed (reorg) failed uuid=%s: %s",
                        sr.receipt.request_uuid, e,
                    )
                    continue
                if not used:
                    reverted.append(sr.receipt.request_uuid)
            return reverted

        needs_revert = await asyncio.to_thread(_check, rows)
        reverted_count = 0
        for u in needs_revert:
            if await store.revert_claimed(u):
                reverted_count += 1

        if reverted_count:
            logger.warning(
                "Reaper: chain reorg detected — reverted %d rows from "
                "claimed → claimable (tx %s rolled back on-chain)",
                reverted_count,
                ", ".join(u[:8] for u in needs_revert[:5]),
            )

        return {"reorg_checked": len(rows), "reorg_reverted": reverted_count}


_abi_cache: list[dict] | None = None


def _load_abi_once() -> list[dict]:
    global _abi_cache
    if _abi_cache is not None:
        return _abi_cache
    from app.payment.settlement import _load_abi
    _abi_cache = _load_abi()
    return _abi_cache
