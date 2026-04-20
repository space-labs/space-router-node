"""Leg 2 receipt submission + background sync with the coord API.

By topology rule the provider never connects to the gateway. Interaction
goes through the coord API:

1. After a relay, generate a Receipt from the provider's own byte count
   and hand it to the submitter. Submitter records it locally as
   ``unsigned`` (signature=None) then fires a best-effort POST to the
   coord API.
2. Happy path: coord API returns 200 with the signed receipt immediately
   (gateway's pending_leg2 row already existed). Submitter stores the
   signature locally.
3. Slow path: coord API returns 202 (accepted for async signing). Nothing
   else to do — the provider's local record stays unsigned and will be
   filled in by the poller.
4. ``ReceiptPoller`` runs in the background every ``poll_interval_seconds``
   and does a single short GET to ``/nodes/{id}/signed-receipts?since=<ts>``
   to pick up any signed copies for locally-unsigned rows. Short HTTP
   timeouts throughout (5s).

All failures are non-fatal: local state is the source of truth, and the
poller will pick up signatures on its next tick.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from app.config import Settings
from app.payment.eip712 import Receipt, address_to_bytes32
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Short timeouts — no request should wait for another service's work.
SUBMIT_TIMEOUT_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 10.0

# Absorbs clock skew between coord API DB and provider host — after each
# poll, cursor is rolled back by this much so a row inserted with an
# older timestamp (e.g. cron-side sync path finishing after a fast GET)
# still gets picked up on the next tick. mark_signed is idempotent.
POLL_CURSOR_BUFFER_SECONDS = 60

# On start, back the cursor up by this much — catches anything signed
# between the node's previous shutdown and this startup.
POLL_INITIAL_LOOKBACK_HOURS = 24


def _build_receipt(
    gateway_payer_address: str,
    node_wallet_address: str,
    rate_per_gb: int,
    data_amount: int,
) -> Receipt:
    total_price = (data_amount * rate_per_gb) // _GB
    return Receipt(
        client_address=gateway_payer_address,
        node_address=address_to_bytes32(node_wallet_address),
        request_uuid=str(uuid.uuid4()),
        data_amount=int(data_amount),
        total_price=int(total_price),
    )


def _receipt_body_hash(receipt: Receipt) -> str:
    """Deterministic sha256 over the receipt body — used to bind the
    identity signature to the exact payload so a MITM can't tamper with
    ``dataAmount``/``totalPrice`` using a captured signature.
    """
    canonical = (
        f"{receipt.client_address.lower()}|{receipt.node_address.lower()}|"
        f"{receipt.request_uuid}|{int(receipt.data_amount)}|{int(receipt.total_price)}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sign_submission(
    identity_key: str,
    node_id: str,
    request_id: str,
    timestamp: int,
    receipt_hash: str,
) -> str:
    msg = (
        f"space-router:submit-receipt:{node_id}:{request_id}"
        f":{receipt_hash}:{timestamp}"
    )
    account = Account.from_key(identity_key)
    signed = account.sign_message(encode_defunct(text=msg))
    return "0x" + signed.signature.hex()


def _sign_list_request(identity_key: str, node_id: str, timestamp: int) -> str:
    msg = f"space-router:list-signed-receipts:{node_id}:{timestamp}"
    account = Account.from_key(identity_key)
    signed = account.sign_message(encode_defunct(text=msg))
    return "0x" + signed.signature.hex()


class ReceiptSubmitter:
    """Builds receipts, records them locally, and fires the best-effort POST."""

    def __init__(
        self,
        settings: Settings,
        node_id: str,
        identity_key: str,
        identity_address: str,
        gateway_payer_address: str,
        node_wallet_address: str,
    ) -> None:
        self._settings = settings
        self._node_id = node_id
        self._identity_key = identity_key if identity_key.startswith("0x") else "0x" + identity_key
        self._identity_address = identity_address.lower()
        self._gateway_payer_address = gateway_payer_address
        self._node_wallet_address = node_wallet_address

    @property
    def ready(self) -> bool:
        return bool(
            self._gateway_payer_address
            and self._node_wallet_address
            and self._identity_key
            and self._node_id
        )

    async def submit(self, request_id: str, data_amount: int) -> None:
        """Generate + store unsigned + fire async POST.

        Returns after the POST completes (or times out) but never
        raises — the poller will fill in the signature later if the POST
        didn't already.
        """
        if not self.ready or data_amount <= 0:
            return

        receipt = _build_receipt(
            gateway_payer_address=self._gateway_payer_address,
            node_wallet_address=self._node_wallet_address,
            rate_per_gb=self._settings.NODE_RATE_PER_GB,
            data_amount=data_amount,
        )

        # Persist locally as unsigned *before* firing the POST, so a
        # crash/timeout doesn't lose the receipt.
        try:
            store = get_store(self._settings.RECEIPT_STORE_PATH)
            await store.initialize()
            await store.store_unsigned(receipt, request_id=request_id)
        except Exception:
            logger.exception("Failed to persist unsigned receipt uuid=%s", receipt.request_uuid)
            return

        # Best-effort submit.
        await self._fire_submit(receipt, request_id)

    async def _fire_submit(self, receipt: Receipt, request_id: str) -> None:
        timestamp = int(time.time())
        receipt_hash = _receipt_body_hash(receipt)
        signature = _sign_submission(
            self._identity_key, self._node_id, request_id, timestamp, receipt_hash,
        )
        url = self._settings.COORDINATION_API_URL.rstrip("/") + f"/nodes/{self._node_id}/receipts"
        payload = {
            "request_id": request_id,
            "receipt": receipt.to_json_dict(),
            "signature": signature,
            "timestamp": timestamp,
        }

        try:
            async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.debug("Leg 2 submit network error uuid=%s: %s — poller will retry",
                         receipt.request_uuid, exc)
            return

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return
            if body.get("status") == "signed" and body.get("signature"):
                try:
                    store = get_store(self._settings.RECEIPT_STORE_PATH)
                    await store.mark_signed(receipt.request_uuid, body["signature"])
                    logger.info(
                        "Leg 2 receipt signed synchronously uuid=%s amount=%d",
                        receipt.request_uuid, receipt.data_amount,
                    )
                except Exception:
                    logger.exception("Failed to mark receipt signed uuid=%s",
                                     receipt.request_uuid)
        elif resp.status_code == 202:
            logger.debug(
                "Leg 2 receipt queued for async signing uuid=%s", receipt.request_uuid,
            )
        else:
            logger.debug(
                "Leg 2 submit got %d uuid=%s — poller will retry",
                resp.status_code, receipt.request_uuid,
            )


class ReceiptPoller:
    """Background loop that fetches signed receipts and fills in local signatures.

    Runs every ``POLL_INTERVAL_SECONDS``. Each tick is a single short GET.
    Uses ``created_at`` as a cursor (persisted through the next tick via
    the store's last-seen-timestamp).
    """

    def __init__(
        self,
        settings: Settings,
        node_id: str,
        identity_key: str,
        node_wallet_address: str,
    ) -> None:
        self._settings = settings
        self._node_id = node_id
        self._identity_key = identity_key if identity_key.startswith("0x") else "0x" + identity_key
        self._node_wallet_address = node_wallet_address
        self._cursor: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        # Initial cursor = now - 24h so first tick picks up anything signed
        # while the node was offline. mark_signed is idempotent, so
        # re-fetching already-stored rows is safe.
        from datetime import timedelta
        self._cursor = datetime.now(timezone.utc) - timedelta(hours=POLL_INITIAL_LOOKBACK_HOURS)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("Leg 2 receipt poller started (interval=%ds)", POLL_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Leg 2 poller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        store = get_store(self._settings.RECEIPT_STORE_PATH)
        await store.initialize()

        # Only poll when we have unsigned receipts waiting — saves API calls.
        unsigned_count = await store.count_unsigned()
        if unsigned_count == 0:
            return

        timestamp = int(time.time())
        sig = _sign_list_request(self._identity_key, self._node_id, timestamp)
        params = {"ts": timestamp, "sig": sig, "limit": 50}
        if self._cursor is not None:
            params["since"] = self._cursor.isoformat()

        url = self._settings.COORDINATION_API_URL.rstrip("/") + f"/nodes/{self._node_id}/signed-receipts"
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, params=params)
        except httpx.RequestError as exc:
            logger.debug("Leg 2 poll network error: %s", exc)
            return

        if resp.status_code != 200:
            logger.debug("Leg 2 poll got %d", resp.status_code)
            return

        try:
            rows = resp.json()
        except Exception:
            return

        if not rows:
            return

        newest_cursor = self._cursor
        for r in rows:
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            except Exception:
                created = None
            try:
                await store.mark_signed(r["request_uuid"], r["signature"])
            except Exception:
                logger.exception("Failed to mark receipt signed uuid=%s",
                                 r.get("request_uuid"))
            if created and (newest_cursor is None or created > newest_cursor):
                newest_cursor = created

        if newest_cursor is not None:
            # Roll back the cursor by POLL_CURSOR_BUFFER_SECONDS so a row
            # inserted with an older timestamp (clock skew, late sync-path
            # commit) gets picked up on the next tick. mark_signed's
            # WHERE signature IS NULL guard makes duplicates a no-op.
            from datetime import timedelta
            self._cursor = newest_cursor - timedelta(seconds=POLL_CURSOR_BUFFER_SECONDS)

        logger.debug("Leg 2 poller: updated %d signatures from coord API", len(rows))


# Module-level singleton used by the proxy_handler's post-relay hook so
# it can call into the submitter without threading the instance through
# every function signature. The poller has a dedicated slot on ctx
# (main.py) — no singleton needed because only shutdown reads it.
_submitter: ReceiptSubmitter | None = None


def set_submitter(submitter: ReceiptSubmitter | None) -> None:
    global _submitter
    _submitter = submitter


def get_submitter() -> ReceiptSubmitter | None:
    return _submitter
