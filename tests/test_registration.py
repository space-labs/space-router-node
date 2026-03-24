"""Tests for node registration and IP detection."""

import json

import pytest
import respx
from httpx import Response

from app.config import Settings
from app.registration import deregister_node, detect_public_ip, register_node, request_probe, save_gateway_ca_cert


TEST_WALLET = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"


@pytest.fixture
def reg_settings():
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL="http://coordination:8000",
        NODE_LABEL="test-node",
        PUBLIC_IP="",
        WALLET_ADDRESS=TEST_WALLET,
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


class TestRegisterNode:
    """Tests for register_node().

    All tests call _mock_request_probe() because register_node()
    now calls request_probe() after registration.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_success(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-abc-123",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "ip_type": "residential",
                "ip_region": "KR",
                "wallet_address": TEST_WALLET,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-abc-123"
        assert gateway_ca_cert is None

        # Verify the request payload
        req = respx.calls[0].request
        body = json.loads(req.content)
        assert body["endpoint_url"] == "https://1.2.3.4:9090"
        assert body["wallet_address"] == TEST_WALLET
        assert body["connectivity_type"] == "direct"
        assert body.get("label") == "test-node"
        # These must NOT be in the payload (server-computed)
        for field in ("public_ip", "node_type", "region"):
            assert field not in body, f"{field} should not be in registration payload"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_with_upnp_endpoint(self, reg_settings):
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-upnp-456",
                "endpoint_url": "https://203.0.113.5:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                upnp_endpoint=("203.0.113.5", 9090),
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-upnp-456"
        assert gateway_ca_cert is None

        req = respx.calls[0].request
        body = json.loads(req.content)
        assert body["endpoint_url"] == "https://203.0.113.5:9090"
        assert body["connectivity_type"] == "upnp"
        assert "public_ip" not in body

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_receives_ip_classification(self, reg_settings):
        """Registration response with ip_type/ip_region should be parsed without error."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-classified",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "ip_type": "residential",
                "ip_region": "Portland, US",
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-classified"
        assert gateway_ca_cert is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_handles_missing_ip_classification(self, reg_settings):
        """Registration response without ip_type/ip_region should default to 'unknown'."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-no-class",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-no-class"
        assert gateway_ca_cert is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_sends_wallet_address(self, reg_settings):
        """wallet_address must always appear in the POST payload."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-wallet-1",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, _ = await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address="0x2c7536E3605D9C16a7a3D7b1898e529396a65c23",
            )

        body = json.loads(respx.calls[0].request.content)
        assert body["wallet_address"] == "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"
        assert node_id == "node-wallet-1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_payload_excludes_server_only_fields(self, reg_settings):
        """public_ip, node_type, region, ip_type, ip_region, as_type must NOT be sent."""
        _mock_request_probe()
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-clean-payload",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address=TEST_WALLET,
            )

        body = json.loads(respx.calls[0].request.content)
        for field in ("public_ip", "node_type", "region", "ip_type", "ip_region", "as_type"):
            assert field not in body, f"{field} should not be in registration payload"

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_failure_raises(self, reg_settings):
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(500, text="Internal Server Error")
        )

        import httpx
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await register_node(
                    client, reg_settings, "1.2.3.4",
                    wallet_address=TEST_WALLET,
                )

    @pytest.mark.asyncio
    @respx.mock
    async def test_register_returns_gateway_ca_cert(self, reg_settings):
        """Registration response with gateway_ca_cert should return it."""
        _mock_request_probe()
        ca_pem = "-----BEGIN CERTIFICATE-----\nTESTDATA\n-----END CERTIFICATE-----"
        respx.post("http://coordination:8000/nodes").mock(
            return_value=Response(201, json={
                "id": "node-mtls-1",
                "endpoint_url": "https://1.2.3.4:9090",
                "node_type": "residential",
                "status": "online",
                "health_score": 1.0,
                "gateway_ca_cert": ca_pem,
                "created_at": "2026-01-01T00:00:00Z",
            })
        )

        import httpx
        async with httpx.AsyncClient() as client:
            node_id, gateway_ca_cert = await register_node(
                client, reg_settings, "1.2.3.4",
                wallet_address=TEST_WALLET,
            )

        assert node_id == "node-mtls-1"
        assert gateway_ca_cert == ca_pem


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
            await request_probe(client, reg_settings, "node-abc-123")

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
            await request_probe(client, reg_settings, "node-abc-123")

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
            await request_probe(client, reg_settings, "node-abc-123")


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
            await deregister_node(client, reg_settings, "node-abc-123")

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
            await deregister_node(client, reg_settings, "node-abc-123")
