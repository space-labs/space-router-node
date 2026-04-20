"""Submit Leg 2 receipts to the coordination API after each relay.

Providers generate their own receipts (using their own byte count) then POST
them here, signing the submission with their identity key via EIP-191. The
coord API brokers the signing with the gateway and returns the signed copy,
which we persist in local SQLite for future ``--claim`` CLI usage.

By product topology, providers never connect directly to the gateway.
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from app.config import Settings
from app.payment.eip712 import Receipt, address_to_bytes32
from app.payment.receipt_store import get_store

logger = logging.getLogger(__name__)

_GB = 1024 ** 3


class ReceiptSubmitter:
    """Builds, submits, and persists Leg 2 receipts."""

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

    def _build_receipt(self, data_amount: int) -> Receipt:
        total_price = (data_amount * self._settings.NODE_RATE_PER_GB) // _GB
        return Receipt(
            client_address=self._gateway_payer_address,
            node_address=address_to_bytes32(self._node_wallet_address),
            request_uuid=str(uuid.uuid4()),
            data_amount=int(data_amount),
            total_price=int(total_price),
        )

    def _sign_submission(self, request_id: str, timestamp: int) -> str:
        msg = f"space-router:submit-receipt:{self._node_id}:{request_id}:{timestamp}"
        account = Account.from_key(self._identity_key)
        signed = account.sign_message(encode_defunct(text=msg))
        return "0x" + signed.signature.hex()

    async def submit(self, request_id: str, data_amount: int) -> None:
        """Build, submit to coord API, and persist the signed receipt locally.

        Best-effort: all failure modes log a warning and return. A later relay
        can retry for its own request, and the operator can always run
        ``--claim`` on whatever receipts did land in the local store.
        """
        if not self.ready:
            logger.debug("ReceiptSubmitter not ready — skipping for request %s", request_id)
            return
        if data_amount <= 0:
            return

        receipt = self._build_receipt(data_amount)
        timestamp = int(time.time())
        signature = self._sign_submission(request_id, timestamp)

        url = self._settings.COORDINATION_API_URL.rstrip("/") + f"/nodes/{self._node_id}/receipts"
        payload = {
            "request_id": request_id,
            "receipt": receipt.to_json_dict(),
            "signature": signature,
            "timestamp": timestamp,
        }

        try:
            # Coord API retries internally for up to ~10s while waiting for the
            # gateway to finalise its pending_leg2 row, so we need a longer
            # client-side timeout than the usual 10s default.
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.warning("Leg 2 receipt submission failed (network): %s", exc)
            return

        if resp.status_code != 200:
            try:
                err = resp.json().get("detail") or resp.text
            except Exception:
                err = resp.text
            logger.warning(
                "Leg 2 receipt rejected by coord API (status=%d): %s",
                resp.status_code, err,
            )
            return

        body = resp.json()
        if body.get("status") != "signed":
            logger.warning("Unexpected coord API response: %s", body)
            return

        signed_receipt = Receipt.from_json_dict(body["receipt"])
        gw_sig = body["signature"]

        try:
            store = get_store(self._settings.RECEIPT_STORE_PATH)
            await store.initialize()
            await store.store(signed_receipt, gw_sig)
        except Exception:
            logger.exception(
                "Leg 2 receipt signed but failed to persist locally (uuid=%s). "
                "Signature recoverable from coord API signed_receipts table.",
                signed_receipt.request_uuid,
            )
            return

        logger.info(
            "Leg 2 receipt submitted and stored: uuid=%s amount=%d price=%d",
            signed_receipt.request_uuid,
            signed_receipt.data_amount,
            signed_receipt.total_price,
        )


# Module-level singleton so proxy_handler's post-relay hook can find it
# without threading the submitter through every function signature.
_submitter: ReceiptSubmitter | None = None


def set_submitter(submitter: ReceiptSubmitter | None) -> None:
    global _submitter
    _submitter = submitter


def get_submitter() -> ReceiptSubmitter | None:
    return _submitter
