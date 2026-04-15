"""Tests for node payment receipt exchange and settlement (Phase 4).

Covers:
- Receipt building (price calculation, bytes32 node address)
- Frame protocol (encode/decode/roundtrip)
- Receipt exchange with gateway (happy path, rejection, timeout, EOF)
- SettlementManager (store, batch, mark settled/failed, stats, lifecycle)
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
from app.payment.settlement import SettlementManager, SettlementStats

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


# ── SettlementManager ─────────────────────────────────────────────────


class TestSettlementManager:
    def _make_receipt(self) -> Receipt:
        return Receipt(
            client_address=GATEWAY_ADDRESS,
            node_address=NODE_B32,
            request_uuid=str(uuid.uuid4()),
            data_amount=5000,
            total_price=100,
        )

    def test_add_and_get_batch(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        r = self._make_receipt()
        mgr.add_receipt(r, "0xfakesig")

        batch = mgr.get_unsettled_batch()
        assert len(batch) == 1
        assert batch[0]["request_uuid"] == r.request_uuid
        assert batch[0]["source"] == "node_leg2"

    def test_batch_limit(self):
        mgr = SettlementManager(batch_size=3, settlement_interval=0)
        for _ in range(10):
            mgr.add_receipt(self._make_receipt(), "0xsig")
        assert len(mgr.get_unsettled_batch()) == 3

    def test_mark_settled(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        r = self._make_receipt()
        mgr.add_receipt(r, "0xsig")
        mgr.mark_settled([r.request_uuid], "0xtxhash")

        assert len(mgr.get_unsettled_batch()) == 0
        stats = mgr.get_stats()
        assert stats.settled == 1

    def test_mark_failed(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        r = self._make_receipt()
        mgr.add_receipt(r, "0xsig")
        mgr.mark_failed([r.request_uuid])

        stats = mgr.get_stats()
        assert stats.failed == 1
        assert stats.unsettled == 0

    def test_get_stats(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        for i in range(5):
            mgr.add_receipt(self._make_receipt(), f"0xsig{i}")

        # Settle 2, fail 1
        uuids = [r["request_uuid"] for r in mgr._receipts]
        mgr.mark_settled(uuids[:2], "0xtx1")
        mgr.mark_failed(uuids[2:3])

        stats = mgr.get_stats()
        assert stats.total == 5
        assert stats.settled == 2
        assert stats.failed == 1
        assert stats.unsettled == 2

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=1)
        await mgr.start()
        assert mgr._settlement_task is not None
        await mgr.stop()
        assert mgr._settlement_task.cancelled() or mgr._settlement_task.done()

    def test_claim_batch_empty_raises(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        with pytest.raises(ValueError, match="Empty"):
            mgr.claim_batch([], [])

    def test_claim_batch_mismatch_raises(self):
        mgr = SettlementManager(batch_size=10, settlement_interval=0)
        r = self._make_receipt()
        with pytest.raises(ValueError, match="same length"):
            mgr.claim_batch([r], [])


# ── Config Fields ─────────────────────────────────────────────────────


class TestPaymentConfig:
    def test_payment_fields_exist(self):
        """Verify payment config fields exist with correct types."""
        from app.config import Settings
        fields = Settings.model_fields
        assert "PAYMENT_ENABLED" in fields
        assert "NODE_RATE_PER_GB" in fields
        assert "SETTLEMENT_ENABLED" in fields
        assert "EIP712_DOMAIN_NAME" in fields

    def test_payment_config_from_env(self, monkeypatch):
        monkeypatch.setenv("SR_PAYMENT_ENABLED", "true")
        monkeypatch.setenv("SR_NODE_RATE_PER_GB", "1000000000000000000")
        monkeypatch.setenv("SR_NODE_IDENTITY_ADDRESS", NODE_ADDRESS)
        from app.config import Settings
        s = Settings()
        assert s.PAYMENT_ENABLED is True
        assert s.NODE_RATE_PER_GB == 10 ** 18
        assert s.NODE_IDENTITY_ADDRESS == NODE_ADDRESS
