"""On-chain settlement of locally-stored Leg 2 receipts.

Called from the ``claim`` CLI command. Reads unclaimed receipts from the
local SQLite store and submits them to ``TokenPaymentEscrow.claimBatch()``
in batches. The contract silently skips any receipt whose client has
insufficient balance / whose nonce is already used / whose node is not
registered — so we mark a batch as claimed only if the tx confirms.

Web3 calls run in ``asyncio.to_thread`` (web3.py is sync).

Two hardening behaviours from P3 (v1.5 plan):

- **L5: claim-in-flight sentinel.** Before broadcasting we compute the
  raw tx hash deterministically (``account.sign_transaction`` produces
  a hash without needing the chain) and persist it as
  ``claim_tx_pending`` on every UUID in the batch. A crash between
  broadcast and ``mark_claimed`` no longer creates a re-claim revert
  loop — :mod:`app.payment.inflight_reconciler` resolves the row via
  ``isNonceUsed`` on the next daemon startup.
- **L6: per-receipt local sig verify.** Before submission we recover
  the signer of each receipt's EIP-712 signature locally and drop any
  row whose recovered signer doesn't match
  ``settings.GATEWAY_PAYER_ADDRESS``. Otherwise one corrupt signature
  reverts the whole batch atomically and burns ``claim_attempts`` on
  every other row in it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.payment import reasons
from app.payment.eip712 import (
    ESCROW_DOMAIN_NAME,
    ESCROW_DOMAIN_VERSION,
    EIP712Domain,
    recover_receipt_signer,
)
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
    sig_verify_dropped: int = 0


def _load_abi() -> list[dict]:
    """The bundled abi file is ``{"escrow": [...], "erc20": [...]}``; extract escrow."""
    with open(_ABI_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data["escrow"]
    return data  # already a flat list


def _is_insufficient_funds_error(exc: BaseException) -> bool:
    """Sniff a web3 broadcast exception for "wallet has no CTC for gas".

    The CC3 testnet RPC returns this as a JSON-RPC error with code -32603
    and message ``insufficient funds for gas * price + value``. Other
    EVM RPCs use slight phrasing variants (``insufficient funds for
    intrinsic transaction cost``, ``gas required exceeds allowance``,
    etc.). We match on the canonical "insufficient funds" substring
    case-insensitive — narrow enough to not catch unrelated errors,
    broad enough to survive minor phrasing changes.
    """
    text = str(exc).lower()
    if "insufficient funds" in text:
        return True
    # web3.py wraps some errors in a dict on .args[0]
    args = getattr(exc, "args", ())
    for a in args:
        if isinstance(a, dict):
            msg = str(a.get("message", "")).lower()
            if "insufficient funds" in msg:
                return True
    return False


def _is_already_known_error(exc: BaseException) -> bool:
    """Match the "tx is already in the mempool" family of broadcast errors.

    A retried claim deterministically signs the same tx_hash; a prior
    attempt may have leaked it into the mempool before the RPC blip,
    so the next ``send_raw_transaction`` rejects with one of:

    * geth: ``already known``
    * geth (some forks): ``transaction already exists``
    * besu / nethermind: ``AlreadyKnown`` / ``ALREADY_EXISTS``
    * generic phrasing: ``already_known``

    All of them mean the tx is in mempool and we should fall through to
    ``wait_for_transaction_receipt`` rather than treating it as a hard
    RPC failure (which is what pre-rc.5 did, classifying the retry as
    CLAIM_RPC_UNREACHABLE forever). Mirrors the case-insensitive
    substring style of :py:func:`_is_insufficient_funds_error`.
    """
    text = str(exc).lower()
    needles = (
        "already known",
        "alreadyknown",
        "already_known",
        "already exists",
        "already_exists",
        "transaction already exists",
    )
    if any(n in text for n in needles):
        return True
    args = getattr(exc, "args", ())
    for a in args:
        if isinstance(a, dict):
            msg = str(a.get("message", "")).lower()
            if any(n in msg for n in needles):
                return True
    return False


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
        # RPC-unreachable AND insufficient-gas stop the whole run —
        # the next batch would just hit the same condition and we'd
        # spam every receipt's last_error_code with the same noise.
        # Every other failure (revert, timeout) is a per-batch outcome
        # and we continue.
        if result.reason_code in (
            reasons.CLAIM_RPC_UNREACHABLE,
            reasons.CLAIM_INSUFFICIENT_GAS,
        ):
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


def _verify_signatures_locally(
    settings: Settings, batch: list[StoredReceipt],
) -> tuple[list[StoredReceipt], list[str]]:
    """Pre-flight EIP-712 signer recovery against ``GATEWAY_PAYER_ADDRESS``.

    Returns ``(kept, dropped_uuids)``. Any receipt whose recovered
    signer doesn't match the configured gateway payer is excluded from
    the returned batch; its UUID is in ``dropped_uuids`` for the
    caller to mark ``failed_terminal``/``SIGN_VERIFY_FAILED``.

    If ``GATEWAY_PAYER_ADDRESS`` is unset (older configs / dev fixtures
    that haven't fetched the coord ``/config``), this function is a
    no-op — verifying against an empty address would reject every row.
    The settlement still works because the on-chain ``claimBatch``
    enforces the same check; we'd just lose the per-receipt isolation.
    """
    payer = (settings.GATEWAY_PAYER_ADDRESS or "").strip()
    if not payer:
        return list(batch), []

    from eth_utils import to_checksum_address
    try:
        expected = to_checksum_address(payer)
    except Exception:
        # Bad config — log and skip rather than dropping all rows.
        logger.warning(
            "GATEWAY_PAYER_ADDRESS=%r is not a valid address; skipping "
            "local signature verification for this batch.", payer,
        )
        return list(batch), []

    domain = EIP712Domain(
        name=ESCROW_DOMAIN_NAME,
        version=ESCROW_DOMAIN_VERSION,
        chain_id=int(settings.ESCROW_CHAIN_ID),
        verifying_contract=settings.ESCROW_CONTRACT_ADDRESS,
    )

    kept: list[StoredReceipt] = []
    dropped: list[str] = []
    for sr in batch:
        sig = sr.signature
        if not sig:
            # Should never reach _submit_batch unsigned, but be defensive.
            dropped.append(sr.receipt.request_uuid)
            continue
        try:
            recovered = recover_receipt_signer(sr.receipt, sig, domain)
        except Exception as e:
            logger.debug(
                "Local sig recovery raised for uuid=%s: %s",
                sr.receipt.request_uuid, e,
            )
            dropped.append(sr.receipt.request_uuid)
            continue
        if to_checksum_address(recovered) != expected:
            dropped.append(sr.receipt.request_uuid)
        else:
            kept.append(sr)
    return kept, dropped


async def _drop_corrupt_signatures(
    store: ReceiptStore, dropped_uuids: list[str],
) -> None:
    """Mark each corrupt-sig UUID terminal with ``SIGN_VERIFY_FAILED``.

    A receipt whose locally-recovered signer doesn't match the
    configured gateway payer can never settle on-chain — the contract
    runs the same recovery and would revert the whole batch atomically.
    There's no retry that fixes this, so we move the row to
    ``failed_terminal`` immediately.

    We stamp the failure code via :func:`mark_claim_failed` (so
    ``last_error_code`` and ``last_attempt_at`` are consistent with
    other terminal outcomes) and then call :func:`lock` to set
    ``locked=1`` regardless of the attempt counter. The reason code is
    a sign-side code, not a chain-side one, so the existing
    ``counts_against_retry_budget`` rule wouldn't lock it on its own
    after a single occurrence.
    """
    for u in dropped_uuids:
        await store.mark_claim_failed(
            [u], reasons.SIGN_VERIFY_FAILED,
            "Local EIP-712 recovery did not match GATEWAY_PAYER_ADDRESS.",
        )
        await store.lock(u)


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

    Two pre-flight steps before the chain is touched (P3/L5+L6):

    - ``_verify_signatures_locally`` drops any receipt whose
      gateway-signed signature doesn't recover to
      ``GATEWAY_PAYER_ADDRESS``. Otherwise one corrupt sig reverts the
      whole batch atomically and burns ``claim_attempts`` on every
      other row.
    - ``set_claim_tx_pending`` persists the deterministic tx hash on
      every UUID before broadcast so a crash mid-flight is recoverable
      via :mod:`app.payment.inflight_reconciler`.
    """
    # L6 — local signature verify before submission.
    kept_batch, dropped_uuids = _verify_signatures_locally(settings, batch)
    if dropped_uuids:
        logger.info(
            "Settlement: dropped %d/%d receipt(s) from batch — local "
            "EIP-712 recovery did not match GATEWAY_PAYER_ADDRESS. "
            "Submitting %d remaining.",
            len(dropped_uuids), len(batch), len(kept_batch),
        )
        await _drop_corrupt_signatures(store, dropped_uuids)

    if not kept_batch:
        # All receipts in this batch had corrupt sigs — return a
        # synthetic empty result so the caller advances. No chain tx.
        return ClaimResult(
            submitted=0, tx_hash=None, gas_used=None,
            sig_verify_dropped=len(dropped_uuids),
        )

    def _build_and_sign() -> tuple:
        """Build tx, sign locally, return (tx_hash_hex, signed, w3_state)."""
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(
            settings.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 30},
        ))
        if not w3.is_connected():
            return None, None, None, None  # type: ignore[return-value]

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
            abi=_load_abi(),
        )
        account = Account.from_key(settlement_key)

        receipts_tuples = [_to_contract_tuple(sr) for sr in kept_batch]
        signatures = [bytes.fromhex(sr.signature.removeprefix("0x")) for sr in kept_batch]

        GAS_CAP = 12_000_000
        try:
            gas_estimate = contract.functions.claimBatch(
                receipts_tuples, signatures,
            ).estimate_gas({"from": account.address})
            gas_limit = min(int(gas_estimate * 1.2), GAS_CAP)
        except Exception as e:
            gas_limit = min(350_000 * len(kept_batch), GAS_CAP)
            logger.warning("Gas estimation failed (%s); falling back to %d", e, gas_limit)

        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.claimBatch(
            receipts_tuples, signatures,
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": w3.eth.gas_price,
            "chainId": settings.ESCROW_CHAIN_ID,
        })
        signed = account.sign_transaction(tx)
        # eth_account exposes the deterministic tx hash before broadcast
        # so we can persist it as the in-flight breadcrumb.
        try:
            tx_hash_hex = signed.hash.hex()
        except AttributeError:
            # Older eth_account uses tx_hash; use as fallback.
            tx_hash_hex = signed.tx_hash.hex()  # type: ignore[attr-defined]
        if not tx_hash_hex.startswith("0x"):
            tx_hash_hex = "0x" + tx_hash_hex
        return w3, signed, tx_hash_hex, gas_limit

    built = await asyncio.to_thread(_build_and_sign)
    w3, signed, tx_hash_hex, _gas_limit = built
    if w3 is None:
        result = ClaimResult(
            submitted=len(kept_batch), tx_hash=None, gas_used=None,
            error=f"RPC unreachable: {settings.ESCROW_CHAIN_RPC}",
            reason_code=reasons.CLAIM_RPC_UNREACHABLE,
            sig_verify_dropped=len(dropped_uuids),
        )
        kept_uuids = [sr.receipt.request_uuid for sr in kept_batch]
        await store.mark_claim_failed(
            kept_uuids, result.reason_code, result.error,
        )
        return result

    kept_uuids = [sr.receipt.request_uuid for sr in kept_batch]
    # L5 — persist the breadcrumb BEFORE broadcast so a crash here is
    # recoverable. mark_claim_failed for any RPC issue happens after.
    await store.set_claim_tx_pending(kept_uuids, tx_hash_hex)

    def _broadcast_and_wait() -> ClaimResult:
        # Default the tx_hex to the deterministic hash we computed at
        # build time. ``send_raw_transaction`` returns the same value on
        # success; on the "already known" path we rely on this fallback
        # because the broadcast rejected synchronously.
        tx_hex = tx_hash_hex
        broadcast_succeeded = False
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            broadcast_succeeded = True
        except Exception as e:
            # "Already known" — a prior attempt at this exact deterministic
            # tx_hash already populated the mempool. Treat as a successful
            # broadcast and fall through to wait_for_transaction_receipt;
            # otherwise the retry stays stuck on CLAIM_RPC_UNREACHABLE
            # forever even though the tx is in fact about to mine.
            if _is_already_known_error(e):
                logger.info(
                    "claim batch broadcast: tx %s already in mempool from a "
                    "prior attempt — falling through to receipt wait",
                    tx_hex,
                )
            else:
                error_text = f"broadcast failed: {e}"
                # Distinguish "wallet has no CTC for gas" from generic RPC
                # failures — the resolution is operator-actionable (fund the
                # identity wallet) and should not look like a transient
                # network blip in the GUI.
                if _is_insufficient_funds_error(e):
                    code = reasons.CLAIM_INSUFFICIENT_GAS
                else:
                    code = reasons.CLAIM_RPC_UNREACHABLE
                return ClaimResult(
                    submitted=len(kept_batch), tx_hash=None, gas_used=None,
                    error=error_text,
                    reason_code=code,
                    sig_verify_dropped=len(dropped_uuids),
                )

        # Resolve the bytes form of the tx hash for the receipt wait.
        if broadcast_succeeded:
            tx_hex = tx_hash.hex()
            if not tx_hex.startswith("0x"):
                tx_hex = "0x" + tx_hex
            wait_arg = tx_hash
        else:
            # "already known" path — rebuild the bytes form from the
            # hex we computed at build time. wait_for_transaction_receipt
            # accepts either bytes or hex string, but we use bytes for
            # uniformity with the success path.
            wait_arg = bytes.fromhex(tx_hex.removeprefix("0x"))

        try:
            rcpt = w3.eth.wait_for_transaction_receipt(wait_arg, timeout=120)
        except Exception as e:
            return ClaimResult(
                submitted=len(kept_batch), tx_hash=tx_hex, gas_used=None,
                error=f"tx wait timed out: {e}",
                reason_code=reasons.CLAIM_TX_TIMEOUT,
                sig_verify_dropped=len(dropped_uuids),
            )

        if rcpt.status != 1:
            return ClaimResult(
                submitted=len(kept_batch), tx_hash=tx_hex,
                gas_used=rcpt.gasUsed, error="tx reverted",
                reason_code=reasons.CLAIM_REVERTED,
                sig_verify_dropped=len(dropped_uuids),
            )

        return ClaimResult(
            submitted=len(kept_batch), tx_hash=tx_hex, gas_used=rcpt.gasUsed,
            sig_verify_dropped=len(dropped_uuids),
        )

    result = await asyncio.to_thread(_broadcast_and_wait)

    if result.tx_hash and not result.error:
        marked = await store.mark_claimed(kept_uuids, result.tx_hash)
        # mark_claimed leaves claim_tx_pending populated; clear it now
        # for tidiness so the in-flight reconciler sees a clean state.
        for u in kept_uuids:
            await store.clear_claim_tx_pending(u)
        logger.info(
            "Settled %d receipts in tx %s (gas=%s)",
            marked, result.tx_hash, result.gas_used,
        )
        return result

    if result.reason_code:
        detail = result.tx_hash or result.error
        await store.mark_claim_failed(kept_uuids, result.reason_code, detail)

        # Only confirmed terminal failures clear the breadcrumb:
        # - CLAIM_REVERTED: chain executed and reverted; tx is on-chain.
        # - CLAIM_INSUFFICIENT_GAS: RPC rejected synchronously; tx never
        #   broadcast.
        # - SIG_VERIFY: pre-flight; we never reached broadcast.
        # CLAIM_RPC_UNREACHABLE and CLAIM_TX_TIMEOUT both leave the
        # breadcrumb in place — in either case the tx may have leaked
        # to mempool before the network blip, and the reaper /
        # in-flight reconciler will resolve it via isNonceUsed on the
        # next pass. Clearing the breadcrumb on RPC_UNREACHABLE
        # invites a double-claim if the leaked tx eventually mines.
        if result.reason_code in (
            reasons.CLAIM_REVERTED, reasons.CLAIM_INSUFFICIENT_GAS,
        ):
            for u in kept_uuids:
                await store.clear_claim_tx_pending(u)

        if reasons.counts_against_retry_budget(result.reason_code):
            locked_now = 0
            for u in kept_uuids:
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
