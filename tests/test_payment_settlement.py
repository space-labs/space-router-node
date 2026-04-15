"""Tests for provider payment receipt exchange (Phase 4).

Covers:
- Receipt building (price calculation, bytes32 node address)
- Frame protocol (encode/decode/roundtrip)
- Receipt exchange with gateway (happy path, rejection, timeout, EOF)
- Config payment fields
"""

import asyncio
import json
import struct
import uuid

import pytest

from app.payment.eip712 import (
    EIP712Domain,
    Receipt,
    address_to_bytes32,
    sign_receipt,
)
from app.payment.receipt_exchange import (
    build_receipt,
    encode_frame,
    exchange_receipt_with_gateway,
    read_frame,
)

# ── Test constants ────────────────────────────────────────────────────

GATEWAY_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
GATEWAY_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
NODE_ADDRESS = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
NODE_B32 = address_to_bytes32(NODE_ADDRESS)

TEST_DOMAIN = EIP712Domain(
    name="TokenPaymentEscrow",
    version="1",
    chain_id=102031,
    verifying_contract="0xC5740e4e9175301a24FB6d22bA184b8ec0762852",
)


# ── build_receipt ─────────────────────────────────────────────────────


class TestBuildReceipt:
    def test_builds_correct_receipt(self):
        r = build_receipt(
            gateway_address=GATEWAY_ADDRESS,
            node_address_bytes32=NODE_B32,
            data_amount=1024 ** 3,  # 1 GB
            rate_per_gb=10 ** 18,   # 1 SPACE / GB
        )
        assert r.client_address == GATEWAY_ADDRESS
        assert r.node_address == NODE_B32
        assert r.data_amount == 1024 ** 3
        assert r.total_price == 10 ** 18
        uuid.UUID(r.request_uuid)  # Should be valid UUID

    def test_price_calculation_small(self):
        r = build_receipt(
            gateway_address=GATEWAY_ADDRESS,
            node_address_bytes32=NODE_B32,
            data_amount=1024,  # 1 KB
            rate_per_gb=10 ** 18,
        )
        expected = (1024 * 10 ** 18) // (1024 ** 3)
        assert r.total_price == expected

    def test_zero_rate(self):
        r = build_receipt(
            gateway_address=GATEWAY_ADDRESS,
            node_address_bytes32=NODE_B32,
            data_amount=10000,
            rate_per_gb=0,
        )
        assert r.total_price == 0

    def test_unique_uuids(self):
        r1 = build_receipt(GATEWAY_ADDRESS, NODE_B32, 100, 10)
        r2 = build_receipt(GATEWAY_ADDRESS, NODE_B32, 100, 10)
        assert r1.request_uuid != r2.request_uuid


# ── Frame Protocol ────────────────────────────────────────────────────


class TestNodeFrameProtocol:
    def test_encode_frame(self):
        data = {"test": "value"}
        frame = encode_frame(data)
        length = struct.unpack(">I", frame[:4])[0]
        payload = json.loads(frame[4:4 + length])
        assert payload == data

    @pytest.mark.asyncio
    async def test_read_frame_success(self):
        data = {"hello": "world"}
        frame = encode_frame(data)
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        result = await read_frame(reader, timeout=1.0)
        assert result == data

    @pytest.mark.asyncio
    async def test_read_frame_timeout(self):
        reader = asyncio.StreamReader()
        result = await read_frame(reader, timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_read_frame_eof(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        result = await read_frame(reader, timeout=0.5)
        assert result is None


# ── Receipt Exchange with Gateway ─────────────────────────────────────


class TestReceiptExchangeWithGateway:
    @pytest.mark.asyncio
    async def test_successful_exchange(self):
        """Gateway accepts receipt and returns valid signature."""
        receipt = build_receipt(GATEWAY_ADDRESS, NODE_B32, 5000, 10 ** 18)

        # Simulate gateway response: signed
        sig = sign_receipt(GATEWAY_KEY, receipt, TEST_DOMAIN)
        response = encode_frame({
            "status": "signed",
            "signature": sig,
            "receipt": receipt.to_json_dict(),
        })

        reader = asyncio.StreamReader()
        reader.feed_data(response)

        written = bytearray()

        class MockWriter:
            def write(self, data): written.extend(data)
            async def drain(self): pass

        result = await exchange_receipt_with_gateway(
            reader, MockWriter(), receipt, timeout=2.0,
        )
        assert result is not None
        returned_sig, returned_receipt = result
        assert returned_sig == sig
        assert returned_receipt.request_uuid == receipt.request_uuid

    @pytest.mark.asyncio
    async def test_gateway_rejects(self):
        receipt = build_receipt(GATEWAY_ADDRESS, NODE_B32, 5000, 10 ** 18)
        response = encode_frame({"status": "rejected", "error": "clientAddress mismatch"})

        reader = asyncio.StreamReader()
        reader.feed_data(response)

        class MockWriter:
            def write(self, data): pass
            async def drain(self): pass

        result = await exchange_receipt_with_gateway(
            reader, MockWriter(), receipt, timeout=2.0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout(self):
        receipt = build_receipt(GATEWAY_ADDRESS, NODE_B32, 5000, 10 ** 18)
        reader = asyncio.StreamReader()

        class MockWriter:
            def write(self, data): pass
            async def drain(self): pass

        result = await exchange_receipt_with_gateway(
            reader, MockWriter(), receipt, timeout=0.1,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_eof_from_gateway(self):
        receipt = build_receipt(GATEWAY_ADDRESS, NODE_B32, 5000, 10 ** 18)
        reader = asyncio.StreamReader()
        reader.feed_eof()

        class MockWriter:
            def write(self, data): pass
            async def drain(self): pass

        result = await exchange_receipt_with_gateway(
            reader, MockWriter(), receipt, timeout=0.5,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_signature_in_response(self):
        receipt = build_receipt(GATEWAY_ADDRESS, NODE_B32, 5000, 10 ** 18)
        response = encode_frame({"status": "signed"})  # No signature field

        reader = asyncio.StreamReader()
        reader.feed_data(response)

        class MockWriter:
            def write(self, data): pass
            async def drain(self): pass

        result = await exchange_receipt_with_gateway(
            reader, MockWriter(), receipt, timeout=2.0,
        )
        assert result is None


# ── Config Fields ─────────────────────────────────────────────────────


class TestPaymentConfig:
    def test_payment_fields_exist(self):
        """Verify payment config fields exist with correct types."""
        from app.config import Settings
        fields = Settings.model_fields
        assert "PAYMENT_ENABLED" in fields
        assert "NODE_RATE_PER_GB" in fields
        assert "NODE_IDENTITY_ADDRESS" in fields

    def test_payment_config_from_env(self, monkeypatch):
        monkeypatch.setenv("SR_PAYMENT_ENABLED", "true")
        monkeypatch.setenv("SR_NODE_RATE_PER_GB", "1000000000000000000")
        monkeypatch.setenv("SR_NODE_IDENTITY_ADDRESS", NODE_ADDRESS)
        from app.config import Settings
        s = Settings()
        assert s.PAYMENT_ENABLED is True
        assert s.NODE_RATE_PER_GB == 10 ** 18
        assert s.NODE_IDENTITY_ADDRESS == NODE_ADDRESS
