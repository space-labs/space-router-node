"""Leg 2: Provider generates receipts and exchanges them with the Gateway.

After a successful proxy relay, the Provider:
1. Builds a Receipt (provider is payee, gateway is payer)
2. Sends it as a length-prefixed JSON frame to the Gateway
3. Reads the Gateway's EIP-712 signed response
4. Returns the signed receipt for storage/settlement

Wire protocol: length-prefixed JSON frames (4-byte big-endian length + JSON).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import uuid

from app.payment.eip712 import Receipt, address_to_bytes32

logger = logging.getLogger(__name__)

FRAME_HEADER_SIZE = 4
MAX_FRAME_SIZE = 1024 * 1024  # 1 MiB
RECEIPT_EXCHANGE_TIMEOUT = 5.0  # seconds

# 1 GB in bytes
_GB = 1024 ** 3


def encode_frame(data: dict) -> bytes:
    """Encode a dict as a length-prefixed JSON frame."""
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


async def read_frame(
    reader: asyncio.StreamReader,
    timeout: float = RECEIPT_EXCHANGE_TIMEOUT,
) -> dict | None:
    """Read a length-prefixed JSON frame. Returns None on timeout/error."""
    try:
        header = await asyncio.wait_for(
            reader.readexactly(FRAME_HEADER_SIZE), timeout=timeout,
        )
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        return None

    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_SIZE:
        return None

    try:
        payload = await asyncio.wait_for(
            reader.readexactly(length), timeout=timeout,
        )
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        return None

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def build_receipt(
    gateway_address: str,
    node_address_bytes32: str,
    data_amount: int,
    rate_per_gb: int,
) -> Receipt:
    """Build a Leg 2 receipt (provider is payee, gateway is payer).

    Parameters
    ----------
    gateway_address : str
        Gateway's EVM address (the payer / clientAddress).
    node_address_bytes32 : str
        This provider's bytes32 identity (zero-padded EVM address).
    data_amount : int
        Total bytes transferred (request + response).
    rate_per_gb : int
        Price per GB in token's smallest unit.
    """
    total_price = (data_amount * rate_per_gb) // _GB if _GB > 0 else 0

    return Receipt(
        client_address=gateway_address,
        node_address=node_address_bytes32,
        request_uuid=str(uuid.uuid4()),
        data_amount=data_amount,
        total_price=total_price,
    )


async def exchange_receipt_with_gateway(
    gateway_reader: asyncio.StreamReader,
    gateway_writer: asyncio.StreamWriter,
    receipt: Receipt,
    timeout: float = RECEIPT_EXCHANGE_TIMEOUT,
) -> tuple[str, Receipt] | None:
    """Send a receipt to the gateway and read its signed response.

    Returns (signature, receipt) on success, None on failure.
    """
    # 1. Send receipt frame
    try:
        frame = encode_frame(receipt.to_json_dict())
        gateway_writer.write(frame)
        await gateway_writer.drain()
    except Exception as e:
        logger.warning("Failed to send receipt to gateway: %s", e)
        return None

    # 2. Read gateway's signed response
    response = await read_frame(gateway_reader, timeout=timeout)
    if response is None:
        logger.debug("No receipt response from gateway (likely v0.1.x gateway)")
        return None

    status = response.get("status")
    if status == "rejected":
        logger.warning(
            "Gateway rejected receipt: %s (uuid=%s)",
            response.get("error", "unknown"), receipt.request_uuid,
        )
        return None

    if status != "signed":
        logger.warning("Unexpected receipt response status: %s", status)
        return None

    signature = response.get("signature")
    if not signature:
        logger.warning("Gateway response missing signature")
        return None

    logger.info(
        "Leg 2 receipt signed by gateway: uuid=%s amount=%d price=%d",
        receipt.request_uuid, receipt.data_amount, receipt.total_price,
    )
    return signature, receipt
