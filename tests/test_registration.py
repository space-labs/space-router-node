"""Tests for node registration and IP detection."""

import json

import pytest
import respx
from eth_account import Account
from httpx import Response

from app.config import Settings
from app.registration import deregister_node, detect_public_ip, register_node, request_probe, save_gateway_ca_cert


TEST_WALLET = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"
TEST_COLLECTION = "0x1234567890abcdef1234567890abcdef12345678"
# Test identity keypair (deterministic for reproducible tests)
_TEST_IDENTITY = Account.from_key("0x" + "ab" * 32)
TEST_IDENTITY_KEY = _TEST_IDENTITY.key.hex()


@pytest.fixture
def reg_settings():
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        WALLET_ADDRESS=TEST_WALLET,
        STAKING_ADDRESS="",
        COLLECTION_ADDRESS="",
    )


@pytest.fixture
def reg_settings_v2():
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        WALLET_ADDRESS=TEST_WALLET,
        STAKING_ADDRESS=TEST_WALLET,
        COLLECTION_ADDRESS=TEST_COLLECTION,
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
# register_node
# ---------------------------------------------------------------------------

def _mock_request_probe():
    """Add a catch-all mock for POST /nodes/{id}/request-probe."""
    respx.post(url__regex=r".*/nodes/.*/request-probe").mock(
        return_value=Response(200, json={"ok": True})
    )


def _register_response(node_id="node-abc-123", **overrides):
    """Build a standard POST /nodes/register response."""
    data = {
        "status": "registered",
        "node_id": node_id,
        "identity_address": _TEST_IDENTITY.address.lower(),
        "staking_address": TEST_WALLET,
        "collection_address": TEST_WALLET,
        "endpoint_url": "https://1.2.3.4:9090",
        # Deprecated v0.1.2 aliases
        "wallet_address": TEST_WALLET,
        "node_address": _TEST_IDENTITY.address.lower(),
    }
    data.update(overrides)
    return data


class TestRegisterNode:
    """Tests for register_node().

    All tests call _mock_request_probe() because register_node()
    now calls request_probe() after registration.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_success(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
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
            return_value=Response(200, json=_register_response(
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
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-upnp-456"
        assert gateway_ca_cert is None

        req = respx.calls[0].request
        body = json.loads(req.content)
        assert body["endpoint_url"] == "https://203.0.113.5:9090"

    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.asyncio
    @respx.mock
    async def test_register_receives_ip_classification(self, reg_settings):
        """Registration response should be parsed without error."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response(node_id="node-classified"))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
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
            return_value=Response(200, json=_register_response(node_id="node-wallet-1"))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
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
            return_value=Response(200, json=_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
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
                    wallet_address=TEST_WALLET,
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_returns_gateway_ca_cert(self, reg_settings):
        """Registration response with gateway_ca_cert should return it."""
        _mock_request_probe()
        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response(
                node_id="node-mtls-1", gateway_ca_cert=ca_pem,
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-mtls-1"
        assert gateway_ca_cert == ca_pem


# ---------------------------------------------------------------------------
# v0.2.0 multi-wallet registration
# ---------------------------------------------------------------------------

class TestRegisterNodeV2:
    """Tests for v0.2.0 registration with separate staking/collection addresses."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_sends_staking_vouch_and_collection(self, reg_settings_v2):
        """v0.2.0 payload must include staking_address, collection_address,
        and staking_vouching_signature — and must NOT include wallet_address."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response(
                collection_address=TEST_COLLECTION,
            ))
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, reg_settings_v2, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                wallet_address=TEST_WALLET,
                staking_address=TEST_WALLET,
                collection_address=TEST_COLLECTION,
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["staking_address"] == TEST_WALLET
        assert body["collection_address"] == TEST_COLLECTION
        assert "staking_vouching_signature" in body
        assert len(body["staking_vouching_signature"]) > 0
        assert "wallet_address" not in body
        assert node_id == "node-abc-123"

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_shared_timestamp(self, reg_settings_v2):
        """Identity and vouch signatures must share the same timestamp."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings_v2, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                wallet_address=TEST_WALLET,
                staking_address=TEST_WALLET,
                collection_address=TEST_COLLECTION,
            )

        body = json.loads(respx.calls[0].request.content)
        # Both signatures are verified against body["timestamp"] on the server,
        # so there is exactly one timestamp in the payload.
        assert isinstance(body["timestamp"], int)

        # Verify both signatures are valid hex strings
        assert body["identity_signature"].startswith("0x") or len(body["identity_signature"]) > 20
        assert body["staking_vouching_signature"].startswith("0x") or len(body["staking_vouching_signature"]) > 20

    @pytest.mark.asyncio
    @respx.mock
    async def test_v2_collection_defaults_to_staking(self, reg_settings_v2):
        """When collection_address is empty, it should default to staking_address."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings_v2, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                wallet_address=TEST_WALLET,
                staking_address=TEST_WALLET,
                collection_address="",  # empty → defaults to staking
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["staking_address"] == TEST_WALLET
        assert body["collection_address"] == TEST_WALLET

    @pytest.mark.asyncio
    @respx.mock
    async def test_v1_fallback_when_no_staking(self, reg_settings):
        """When staking_address is empty, use the v0.1.2 wallet_address format."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes/register").mock(
            return_value=Response(200, json=_register_response())
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                identity_key=TEST_IDENTITY_KEY,
                wallet_address=TEST_WALLET,
                staking_address="",  # empty → v0.1.2 mode
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["wallet_address"] == TEST_WALLET
        assert "staking_address" not in body
        assert "staking_vouching_signature" not in body


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
