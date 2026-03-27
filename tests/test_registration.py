"""Tests for node registration and IP detection."""

import json
import time

import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from httpx import Response
from web3 import Web3

from app.config import Settings
from app.identity import sign_vouch
import app.registration as registration_mod
from app.registration import (
    check_node_status,
    deregister_node,
    detect_public_ip,
    register_node,
    request_probe,
    save_gateway_ca_cert,
)

_w3 = Web3()

TEST_WALLET = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"
TEST_COLLECTION = "0x1234567890abcdef1234567890abcdef12345678"
# Test identity keypair (deterministic for reproducible tests)
_TEST_IDENTITY = Account.from_key("0x" + "ab" * 32)
TEST_IDENTITY_KEY = _TEST_IDENTITY.key.hex()
TEST_NODE_ADDRESS = _TEST_IDENTITY.address.lower()

# Separate staking/collection addresses for v0.2.0 tests
TEST_STAKING_ADDRESS = "0x1111111111111111111111111111111111111111"
TEST_COLLECTION_ADDRESS = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def reg_settings():
    """v0.1.2 (v1) registration settings — used by all legacy tests."""
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        STAKING_ADDRESS=TEST_WALLET,
        REGISTRATION_MODE="v1",
    )


@pytest.fixture
def v2_settings():
    """v0.2.0 (v2) registration settings with wallet collapsing."""
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        STAKING_ADDRESS=TEST_WALLET,
        REGISTRATION_MODE="v2",
    )


@pytest.fixture
def v2_multi_wallet_settings():
    """v0.2.0 (v2) with separate staking/collection wallets."""
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        STAKING_ADDRESS=TEST_STAKING_ADDRESS,
        COLLECTION_ADDRESS=TEST_COLLECTION_ADDRESS,
        REGISTRATION_MODE="v2",
    )


@pytest.fixture
def auto_settings():
    """Auto-mode registration settings."""
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        STAKING_ADDRESS=TEST_WALLET,
        REGISTRATION_MODE="auto",
    )


# ---------------------------------------------------------------------------
# detect_public_ip
# ---------------------------------------------------------------------------

class TestDetectPublicIP:
    @pytest.mark.asyncio
    @respx.mock
    async def test_first_service_succeeds(self):
        respx.get("https://httpbin.org/ip").mock(
            return_value=Response(200, json={"origin": "1.2.3.4"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            ip = await detect_public_ip(client)
        assert ip == "1.2.3.4"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fallback_to_second_service(self):
        respx.get("https://httpbin.org/ip").mock(
            return_value=Response(500)
        )
        respx.get("https://api.ipify.org?format=json").mock(
            return_value=Response(200, json={"ip": "5.6.7.8"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            ip = await detect_public_ip(client)
        assert ip == "5.6.7.8"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fallback_to_third_service(self):
        respx.get("https://httpbin.org/ip").mock(
            return_value=Response(500)
        )
        respx.get("https://api.ipify.org?format=json").mock(
            return_value=Response(500)
        )
        respx.get("https://ifconfig.me/ip").mock(
            return_value=Response(200, text="9.10.11.12")
        )

        import httpx
        async with httpx.AsyncClient() as client:
            ip = await detect_public_ip(client)
        assert ip == "9.10.11.12"

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_services_fail(self):
        respx.get("https://httpbin.org/ip").mock(
            return_value=Response(500)
        )
        respx.get("https://api.ipify.org?format=json").mock(
            return_value=Response(500)
        )
        respx.get("https://ifconfig.me/ip").mock(
            return_value=Response(500)
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(RuntimeError, match="Failed to detect"):
                await detect_public_ip(client)


# ---------------------------------------------------------------------------
# register_node (v1 — legacy)
# ---------------------------------------------------------------------------

def _mock_request_probe():
    """Add a catch-all mock for POST /nodes/{id}/request-probe."""
    respx.post(url__regex=r".*/nodes/.*/request-probe").mock(
        return_value=Response(200, json={"ok": True})
    )


def _v1_register_response(node_id="node-abc-123", **overrides):
    """Build a standard v0.1.2 POST /nodes/register response."""
    data = {
        "status": "registered",
        "node_id": node_id,
        "identity_address": _TEST_IDENTITY.address.lower(),
        "staking_address": TEST_WALLET,
        "collection_address": TEST_WALLET,
        "endpoint_url": "https://1.2.3.4:9090",
        # Deprecated v0.1.2 aliases
        "wallet_address": TEST_WALLET,
        "node_address": TEST_NODE_ADDRESS,
    }
    data.update(overrides)
    return data


def _v2_register_response(node_id="node-v2-123", **overrides):
    """Build a v0.2.0 POST /nodes/register response."""
    data = {
        "status": "registered",
        "node_id": node_id,
        "identity_address": TEST_NODE_ADDRESS,
        "staking_address": TEST_WALLET,
        "collection_address": TEST_WALLET,
        "endpoint_url": "https://1.2.3.4:9090",
    }
    data.update(overrides)
    return data


class TestRegisterNode:
    """Tests for register_node() in v1 mode (v0.1.2 protocol).

    All tests call _mock_request_probe() because register_node()
    calls request_probe() after registration.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_success(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-abc-123"
        assert gateway_ca_cert is None

        # Verify the request payload includes identity signature
        req = respx.calls[0].request
        body = json.loads(req.content)
        assert body["endpoint_url"] == "https://1.2.3.4:9090"
        assert body["wallet_address"] == TEST_WALLET
        assert "identity_signature" in body
        assert "timestamp" in body
        assert body.get("label") == "test-node"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_with_upnp_endpoint(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response(
                node_id="node-upnp-456",
                endpoint_url="https://203.0.113.5:9090",
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                upnp_endpoint=("203.0.113.5", 9090),
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-upnp-456"
        assert gateway_ca_cert is None

        req = respx.calls[0].request
        body = json.loads(req.content)
        assert body["endpoint_url"] == "https://203.0.113.5:9090"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_receives_ip_classification(self, reg_settings):
        """Registration response should be parsed without error."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response(node_id="node-classified"))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-classified"
        assert gateway_ca_cert is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_sends_wallet_address(self, reg_settings):
        """wallet_address must always appear in the POST payload."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response(node_id="node-wallet-1"))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address="0x2c7536E3605D9C16a7a3D7b1898e529396a65c23",
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["wallet_address"] == "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"
        assert node_id == "node-wallet-1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_payload_has_identity_signature(self, reg_settings):
        """Payload must include identity_signature and timestamp."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert "identity_signature" in body
        assert "timestamp" in body
        # Server-only classification fields must NOT be in payload
        # (public_ip IS allowed — node sends its real exit IP for tunnel mode)
        for field in ("node_type", "region", "ip_type", "ip_region", "as_type"):
            assert field not in body, f"{field} should not be in registration payload"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_failure_raises(self, reg_settings):
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await register_node(
                    client, reg_settings, "1.2.3.4",
                    identity_key=TEST_IDENTITY_KEY,
                    node_address=TEST_NODE_ADDRESS,
                    wallet_address=TEST_WALLET,
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_returns_gateway_ca_cert(self, reg_settings):
        """Registration response with gateway_ca_cert should return it."""
        _mock_request_probe()
        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response(
                node_id="node-mtls-1", gateway_ca_cert=ca_pem,
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-mtls-1"
        assert gateway_ca_cert == ca_pem


# ---------------------------------------------------------------------------
# register_node (v2 — multi-wallet)
# ---------------------------------------------------------------------------

class TestRegisterNodeV2:
    """Tests for register_node() in v2 mode (v0.2.0 protocol)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_sends_multi_wallet_payload(self, v2_settings):
        """v2 payload must include identity, staking, collection, and vouching."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-v2-123"

        body = json.loads(respx.calls[0].request.content)
        assert body["identity_address"] == TEST_NODE_ADDRESS
        assert "staking_address" in body
        assert "collection_address" in body
        assert "staking_vouching_signature" in body
        assert "identity_signature" in body
        assert "timestamp" in body
        assert body.get("label") == "test-node"

        # v1 fields must NOT be in v2 payload
        assert "wallet_address" not in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_wallet_collapsing(self, v2_settings):
        """When COLLECTION_ADDRESS empty, it defaults to wallet_address (staking address)."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        # With empty COLLECTION_ADDRESS, it falls back to wallet_address (= staking address)
        assert body["staking_address"] == TEST_WALLET
        assert body["collection_address"] == TEST_WALLET

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_with_separate_wallets(self, v2_multi_wallet_settings):
        """Separate staking/collection addresses should be sent correctly."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response(
                staking_address=TEST_STAKING_ADDRESS,
                collection_address=TEST_COLLECTION_ADDRESS,
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_multi_wallet_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_STAKING_ADDRESS,
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["staking_address"] == TEST_STAKING_ADDRESS
        assert body["collection_address"] == TEST_COLLECTION_ADDRESS

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_normalizes_checksummed_addresses(self):
        """EIP-55 checksummed (mixed-case) addresses must be lowercased."""
        checksummed_staking = "0xAbCdEf1111111111111111111111111111111111"
        checksummed_collection = "0x2222222222222222222222222222222222AbCdEf"
        settings = Settings(
            NODE_PORT=9090,
            COORDINATION_API_URL="http://coordination:8000",
            NODE_LABEL="test-node",
            PUBLIC_IP="",
            STAKING_ADDRESS=checksummed_staking,
            COLLECTION_ADDRESS=checksummed_collection,
            REGISTRATION_MODE="v2",
        )
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response(
                staking_address=checksummed_staking.lower(),
                collection_address=checksummed_collection.lower(),
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=checksummed_staking,
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["staking_address"] == checksummed_staking.lower()
        assert body["collection_address"] == checksummed_collection.lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_vouching_signature_valid(self, v2_multi_wallet_settings):
        """Vouching signature must recover to the identity address."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_multi_wallet_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_STAKING_ADDRESS,
            )

        body = json.loads(respx.calls[0].request.content)
        vouching_sig = body["staking_vouching_signature"]
        ts = body["timestamp"]

        # Recover signer from vouching signature
        message_text = f"space-router:vouch:{TEST_STAKING_ADDRESS}:{TEST_COLLECTION_ADDRESS}:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=vouching_sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_returns_gateway_ca_cert(self, v2_settings):
        """v2 registration should return gateway_ca_cert when present."""
        _mock_request_probe()
        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response(
                node_id="node-v2-mtls", gateway_ca_cert=ca_pem,
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-v2-mtls"
        assert gateway_ca_cert == ca_pem

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_register_failure_raises(self, v2_settings):
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await register_node(
                    client, v2_settings, "1.2.3.4",
                    identity_key=TEST_IDENTITY_KEY,
                    node_address=TEST_NODE_ADDRESS,
                    wallet_address=TEST_WALLET,
                )


# ---------------------------------------------------------------------------
# register_node (auto mode)
# ---------------------------------------------------------------------------

class TestAutoModeRegistration:
    """Tests for auto-mode: try v2 first, fall back to v1 on 400/422."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_v2_succeeds(self, auto_settings):
        """When v2 succeeds, no fallback needed."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response(node_id="node-auto-v2"))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-auto-v2"
        # Only one registration call (v2 succeeded)
        reg_calls = [c for c in respx.calls if "/nodes/register" in str(c.request.url)
                     and "/request-probe" not in str(c.request.url)]
        assert len(reg_calls) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_falls_back_to_v1_on_400(self, auto_settings):
        """400 from v2 should trigger v1 fallback."""
        _mock_request_probe()
        # Use a side_effect to return 400 first (v2), then 200 (v1)
        call_count = {"n": 0}
        original_route = respx.post("http://coordination:8000/nodes/register")

        def _side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Response(400, json={"detail": "Unknown fields"})
            return Response(200, json=_v1_register_response(node_id="node-auto-v1"))

        original_route.mock(side_effect=_side_effect)

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-auto-v1"
        # Two registration calls: v2 (400) then v1 (200)
        reg_calls = [c for c in respx.calls if "/nodes/register" in str(c.request.url)
                     and "/request-probe" not in str(c.request.url)]
        assert len(reg_calls) == 2

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_falls_back_to_v1_on_422(self, auto_settings):
        """422 from v2 should also trigger v1 fallback."""
        _mock_request_probe()
        call_count = {"n": 0}

        def _side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Response(422, json={"detail": "Unprocessable"})
            return Response(200, json=_v1_register_response(node_id="node-fallback-422"))

        respx.post("http://coordination:8000/nodes/register").mock(side_effect=_side_effect)

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-fallback-422"

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_does_not_fallback_on_500(self, auto_settings):
        """500 from v2 should NOT trigger fallback — it propagates."""
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await register_node(
                    client, auto_settings, "1.2.3.4",
                    identity_key=TEST_IDENTITY_KEY,
                    node_address=TEST_NODE_ADDRESS,
                    wallet_address=TEST_WALLET,
                )
        assert exc_info.value.response.status_code == 500


# ---------------------------------------------------------------------------
# Backward compatibility: v1 payload isolation
# ---------------------------------------------------------------------------

class TestV1PayloadIsolation:
    """Verify v1 payloads never contain v2-only fields, even when
    v0.2.0 config (STAKING_ADDRESS, COLLECTION_ADDRESS) is set."""

    @pytest.fixture
    def v1_with_v2_config(self):
        """v1 mode but with STAKING/COLLECTION configured — must be ignored."""
        return Settings(
            NODE_PORT=9090,
            COORDINATION_API_URL="http://coordination:8000",
            NODE_LABEL="test-node",
            STAKING_ADDRESS=TEST_STAKING_ADDRESS,
            COLLECTION_ADDRESS=TEST_COLLECTION_ADDRESS,
            REGISTRATION_MODE="v1",
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_payload_excludes_v2_fields(self, reg_settings):
        """v1 payload must never include identity_address, staking_address,
        collection_address, or vouching_signature."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        for field in ("identity_address", "staking_address", "collection_address", "staking_vouching_signature"):
            assert field not in body, f"v1 payload must not contain {field}"
        # Must contain v1 fields
        assert "wallet_address" in body
        assert "identity_signature" in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_ignores_staking_collection_config(self, v1_with_v2_config):
        """Even when STAKING/COLLECTION_ADDRESS are configured, v1 mode
        sends only wallet_address."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v1_with_v2_config, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["wallet_address"] == TEST_WALLET
        assert "staking_address" not in body
        assert "collection_address" not in body


# ---------------------------------------------------------------------------
# Signature message format verification
# ---------------------------------------------------------------------------

class TestSignatureMessageFormats:
    """Verify that v1 and v2 sign different messages for registration."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_signature_signs_over_wallet_address(self, reg_settings):
        """v1 identity_signature message: space-router:register:{wallet_address}:{ts}."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        sig = body["identity_signature"]
        ts = body["timestamp"]

        message_text = f"space-router:register:{TEST_WALLET}:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_signature_signs_over_identity_address(self, v2_settings):
        """v2 identity_signature message: space-router:register:{identity_address}:{ts}."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        sig = body["identity_signature"]
        ts = body["timestamp"]

        message_text = f"space-router:register:{TEST_NODE_ADDRESS}:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_and_v2_signatures_differ(self, reg_settings, v2_settings):
        """v1 and v2 produce different identity_signatures (different message targets)."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        v1_body = json.loads(respx.calls[0].request.content)

        # Reset mocks for v2 call
        respx.reset()
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        v2_body = json.loads(respx.calls[0].request.content)

        # Signatures differ because target differs (wallet_address vs identity_address)
        # unless wallet_address == identity_address, which is unlikely in test data
        assert TEST_WALLET != TEST_NODE_ADDRESS, "test precondition: wallet != identity"
        assert v1_body["identity_signature"] != v2_body["identity_signature"]


# ---------------------------------------------------------------------------
# Auto mode: payload inspection and non-fallback errors
# ---------------------------------------------------------------------------

class TestAutoModePayloads:
    """Verify auto mode sends correct payload formats and handles edge cases."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_first_attempt_uses_v2_payload(self, auto_settings):
        """The first call in auto mode must use v2 payload format."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert "identity_address" in body
        assert "staking_vouching_signature" in body
        assert "wallet_address" not in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_fallback_second_call_uses_v1_payload(self, auto_settings):
        """When auto falls back, the second call must use v1 payload format."""
        _mock_request_probe()
        call_count = {"n": 0}

        def _side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Response(400, json={"detail": "Unknown fields"})
            return Response(200, json=_v1_register_response())

        respx.post("http://coordination:8000/nodes/register").mock(side_effect=_side_effect)

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        reg_calls = [c for c in respx.calls if "/nodes/register" in str(c.request.url)
                     and "/request-probe" not in str(c.request.url)]
        assert len(reg_calls) == 2

        # First call: v2 payload
        first_body = json.loads(reg_calls[0].request.content)
        assert "identity_address" in first_body
        assert "wallet_address" not in first_body

        # Second call: v1 payload
        second_body = json.loads(reg_calls[1].request.content)
        assert "wallet_address" in second_body
        assert "identity_address" not in second_body

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_does_not_fallback_on_403(self, auto_settings):
        """403 (insufficient stake) should NOT trigger fallback."""
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(403, json={"detail": "Insufficient stake"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await register_node(
                    client, auto_settings, "1.2.3.4",
                    identity_key=TEST_IDENTITY_KEY,
                    node_address=TEST_NODE_ADDRESS,
                    wallet_address=TEST_WALLET,
                )
        assert exc_info.value.response.status_code == 403

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_does_not_fallback_on_409(self, auto_settings):
        """409 (duplicate wallet) should NOT trigger fallback."""
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(409, json={"detail": "Wallet already registered"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await register_node(
                    client, auto_settings, "1.2.3.4",
                    identity_key=TEST_IDENTITY_KEY,
                    node_address=TEST_NODE_ADDRESS,
                    wallet_address=TEST_WALLET,
                )
        assert exc_info.value.response.status_code == 409


# ---------------------------------------------------------------------------
# _active_mode tracking
# ---------------------------------------------------------------------------

class TestActiveModeTracking:
    """Verify that _active_mode is set correctly after registration."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_sets_active_mode(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert registration_mod._active_mode == "v1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_sets_active_mode(self, v2_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert registration_mod._active_mode == "v2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_v2_success_sets_v2(self, auto_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert registration_mod._active_mode == "v2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_fallback_sets_v1(self, auto_settings):
        _mock_request_probe()
        call_count = {"n": 0}

        def _side_effect(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return Response(400, json={"detail": "Unknown"})
            return Response(200, json=_v1_register_response())

        respx.post("http://coordination:8000/nodes/register").mock(side_effect=_side_effect)

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, auto_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        assert registration_mod._active_mode == "v1"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestRegistrationModeConfig:
    def test_default_mode_is_v1(self):
        s = Settings(STAKING_ADDRESS=TEST_WALLET)
        assert s.REGISTRATION_MODE == "v1"

    def test_valid_modes_accepted(self):
        for mode in ("v1", "v2", "auto"):
            s = Settings(STAKING_ADDRESS=TEST_WALLET, REGISTRATION_MODE=mode)
            assert s.REGISTRATION_MODE == mode

    def test_invalid_mode_rejected(self):
        with pytest.raises(Exception):
            Settings(STAKING_ADDRESS=TEST_WALLET, REGISTRATION_MODE="v3")

    def test_default_collection_empty(self):
        s = Settings(STAKING_ADDRESS=TEST_WALLET)
        assert s.COLLECTION_ADDRESS == ""


# ---------------------------------------------------------------------------
# v2 with UPnP and label edge cases
# ---------------------------------------------------------------------------

class TestV2EdgeCases:
    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_with_upnp_endpoint(self, v2_settings):
        """v2 should use UPnP endpoint_url when provided."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response(
                endpoint_url="https://203.0.113.5:9090",
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, v2_settings, "1.2.3.4",
                upnp_endpoint=("203.0.113.5", 9090),
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["endpoint_url"] == "https://203.0.113.5:9090"
        assert "identity_address" in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_omits_label_when_empty(self):
        """When NODE_LABEL is empty, label should not appear in payload."""
        s = Settings(
            NODE_PORT=9090,
            COORDINATION_API_URL="http://coordination:8000",
            NODE_LABEL="",
            STAKING_ADDRESS=TEST_WALLET,
            REGISTRATION_MODE="v2",
        )
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v2_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, s, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert "label" not in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_omits_label_when_empty(self):
        """Same for v1: empty label should not appear in payload."""
        s = Settings(
            NODE_PORT=9090,
            COORDINATION_API_URL="http://coordination:8000",
            NODE_LABEL="",
            STAKING_ADDRESS=TEST_WALLET,
            REGISTRATION_MODE="v1",
        )
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_v1_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, s, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                node_address=TEST_NODE_ADDRESS,
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        assert "label" not in body


# ---------------------------------------------------------------------------
# Deregistration is unchanged across modes
# ---------------------------------------------------------------------------

class TestDeregisterPayloadConsistency:
    """Deregistration payload must be identical regardless of registration mode."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_deregister_payload_has_signed_fields(self, reg_settings):
        """Deregister payload must include status, wallet_address, signature, timestamp."""
        respx.patch("http://coordination:8000/nodes/node-123/status").mock(
            return_value=Response(200, json={"ok": True})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await deregister_node(client, reg_settings, "node-123", identity_key=TEST_IDENTITY_KEY)

        body = json.loads(respx.calls[0].request.content)
        assert body["status"] == "offline"
        assert body["wallet_address"] == TEST_WALLET.lower()
        assert "signature" in body
        assert "timestamp" in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_deregister_signature_recovers_correctly(self, reg_settings):
        """Deregister signature must be space-router:update_status:{node_id}:{ts}."""
        respx.patch("http://coordination:8000/nodes/node-xyz/status").mock(
            return_value=Response(200, json={"ok": True})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await deregister_node(client, reg_settings, "node-xyz", identity_key=TEST_IDENTITY_KEY)

        body = json.loads(respx.calls[0].request.content)
        sig = body["signature"]
        ts = body["timestamp"]

        message_text = f"space-router:update_status:node-xyz:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    @pytest.mark.asyncio
    @respx.mock
    async def test_deregister_uses_same_format_with_v2_settings(self, v2_settings):
        """Even after v2 registration, deregistration uses the same v1 payload format."""
        respx.patch("http://coordination:8000/nodes/node-v2-abc/status").mock(
            return_value=Response(200, json={"ok": True})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await deregister_node(client, v2_settings, "node-v2-abc", identity_key=TEST_IDENTITY_KEY)

        body = json.loads(respx.calls[0].request.content)
        assert body["status"] == "offline"
        assert body["wallet_address"] == TEST_WALLET.lower()
        assert "signature" in body
        assert "timestamp" in body
        # Must NOT contain v2-only fields
        assert "identity_address" not in body
        assert "staking_vouching_signature" not in body


# ---------------------------------------------------------------------------
# sign_vouch
# ---------------------------------------------------------------------------

class TestSignVouch:
    def test_vouch_signature_format(self):
        """Vouching message must be space-router:vouch:{staking}:{collection}:{timestamp}."""
        sig, ts = sign_vouch(TEST_IDENTITY_KEY, TEST_STAKING_ADDRESS, TEST_COLLECTION_ADDRESS)

        message_text = f"space-router:vouch:{TEST_STAKING_ADDRESS}:{TEST_COLLECTION_ADDRESS}:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    def test_vouch_signature_recovers_to_signer(self):
        """Recovered address must match the identity key's address."""
        sig, ts = sign_vouch(TEST_IDENTITY_KEY, TEST_WALLET, TEST_WALLET)

        message_text = f"space-router:vouch:{TEST_WALLET}:{TEST_WALLET}:{ts}"
        message = encode_defunct(text=message_text)
        recovered = _w3.eth.account.recover_message(message, signature=sig)
        assert recovered.lower() == TEST_NODE_ADDRESS

    def test_vouch_different_addresses_produce_different_signatures(self):
        """Different staking/collection addresses must produce different signatures."""
        ts = int(time.time())
        sig1, _ = sign_vouch(TEST_IDENTITY_KEY, TEST_STAKING_ADDRESS, TEST_COLLECTION_ADDRESS, timestamp=ts)
        sig2, _ = sign_vouch(TEST_IDENTITY_KEY, TEST_COLLECTION_ADDRESS, TEST_STAKING_ADDRESS, timestamp=ts)
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# request_probe
# ---------------------------------------------------------------------------

class TestRequestProbe:
    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_success(self, reg_settings):
        respx.post("http://coordination:8000/nodes/node-abc-123/request-probe").mock(
            return_value=Response(200, json={"ok": True, "message": "Probe queued"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await request_probe(client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY)

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_400_already_online(self, reg_settings):
        """If node is already online, 400 should be handled gracefully."""
        respx.post("http://coordination:8000/nodes/node-abc-123/request-probe").mock(
            return_value=Response(400, json={"detail": "Node is already online"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            # Should not raise
            await request_probe(client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY)

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_failure_logged_not_raised(self, reg_settings):
        """Probe request failure should be logged, not raised."""
        respx.post("http://coordination:8000/nodes/node-abc-123/request-probe").mock(
            return_value=Response(503, json={"detail": "Service unavailable"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            # Should not raise
            await request_probe(client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY)


# ---------------------------------------------------------------------------
# check_node_status
# ---------------------------------------------------------------------------

class TestCheckNodeStatus:
    @pytest.mark.asyncio
    @respx.mock
    async def test_check_status_returns_status(self, reg_settings):
        """check_node_status should return the status string."""
        respx.get("http://coordination:8000/nodes/node-abc-123/status").mock(
            return_value=Response(200, json={"status": "online"})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            status = await check_node_status(
                client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY,
            )
        assert status == "online"


# ---------------------------------------------------------------------------
# save_gateway_ca_cert
# ---------------------------------------------------------------------------

class TestSaveGatewayCACert:
    def test_save_creates_file(self, tmp_path):
        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        path = str(tmp_path / "certs" / "gateway-ca.crt")
        save_gateway_ca_cert(ca_pem, path)

        with open(path) as f:
            assert f.read() == ca_pem

    def test_save_sets_permissions(self, tmp_path):
        import os
        import stat

        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        path = str(tmp_path / "gateway-ca.crt")
        save_gateway_ca_cert(ca_pem, path)

        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o644


# ---------------------------------------------------------------------------
# deregister_node
# ---------------------------------------------------------------------------

class TestDeregisterNode:
    @pytest.mark.asyncio
    @respx.mock
    async def test_deregister_success(self, reg_settings):
        respx.patch("http://coordination:8000/nodes/node-abc-123/status").mock(
            return_value=Response(200, json={"ok": True})
        )

        import httpx
        async with httpx.AsyncClient() as client:
            # Should not raise
            await deregister_node(client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY)

        req = respx.calls[0].request
        import json
        body = json.loads(req.content)
        assert body["status"] == "offline"

    @pytest.mark.asyncio
    @respx.mock
    async def test_deregister_failure_logged_not_raised(self, reg_settings):
        respx.patch("http://coordination:8000/nodes/node-abc-123/status").mock(
            return_value=Response(500)
        )

        import httpx
        async with httpx.AsyncClient() as client:
            # Should NOT raise — deregister is best-effort
            await deregister_node(client, reg_settings, "node-abc-123", identity_key=TEST_IDENTITY_KEY)
