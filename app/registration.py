"""Node registration with the Coordination API.

Lifecycle:
  1. detect_public_ip()   — determine the machine's public IP
  2. register_node()      — POST /nodes/register with identity signature
  3. request_probe()      — POST /nodes/{id}/request-probe (signed)
  4. deregister_node()    — PATCH /nodes/{id}/status → offline (signed)

All authenticated calls are signed with the node's identity private key.
"""

import logging
import os

import httpx

from app.config import Settings
from app.identity import sign_request

logger = logging.getLogger(__name__)

# Services tried in order for IP detection
_IP_SERVICES = [
    ("https://httpbin.org/ip", "origin"),
    ("https://api.ipify.org?format=json", "ip"),
    ("https://ifconfig.me/ip", None),  # plain-text response
]


async def detect_public_ip(http_client: httpx.AsyncClient) -> str:
    """Detect the machine's public IP by querying external services.

    Tries up to three services; returns the first successful result.
    Raises ``RuntimeError`` if all fail.
    """
    for url, json_key in _IP_SERVICES:
        try:
            resp = await http_client.get(url, timeout=10.0)
            resp.raise_for_status()
            if json_key:
                ip = resp.json()[json_key]
            else:
                ip = resp.text.strip()
            if ip:
                logger.info("Detected public IP: %s (via %s)", ip, url)
                return ip
        except Exception as exc:
            logger.debug("IP detection failed via %s: %s", url, exc)

    raise RuntimeError("Failed to detect public IP from all services")


async def register_node(
    http_client: httpx.AsyncClient,
    settings: Settings,
    public_ip: str,
    *,
    identity_key: str,
    wallet_address: str,
    upnp_endpoint: tuple | None = None,
) -> tuple[str, str | None]:
    """Register this node with the Coordination API.

    Uses the unified ``POST /nodes/register`` endpoint with an identity
    signature.  The server recovers the node identity address from the
    signature.

    Returns ``(node_id, gateway_ca_cert_pem_or_None)``.
    Raises on failure — the caller should abort startup.
    """
    if upnp_endpoint:
        upnp_ip, upnp_port = upnp_endpoint
        endpoint_url = f"https://{upnp_ip}:{upnp_port}"
    else:
        endpoint_url = f"https://{public_ip}:{settings.NODE_PORT}"

    # Sign: space-router:register:{wallet_address}:{timestamp}
    signature, timestamp = sign_request(identity_key, "register", wallet_address)

    payload = {
        "wallet_address": wallet_address,
        "endpoint_url": endpoint_url,
        "identity_signature": signature,
        "timestamp": timestamp,
    }
    if settings.NODE_LABEL:
        payload["label"] = settings.NODE_LABEL

    url = f"{settings.COORDINATION_API_URL}/nodes/register"
    logger.info(
        "Registering node at %s → endpoint=%s wallet=%s",
        url, endpoint_url, wallet_address,
    )

    resp = await http_client.post(url, json=payload, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    node_id = data["node_id"]
    gateway_ca_cert = data.get("gateway_ca_cert")
    node_address = data.get("node_address", "unknown")
    reg_status = data.get("status", "registered")

    logger.info(
        "Registered as node %s (status=%s, identity=%s, wallet=%s, mtls_ca=%s)",
        node_id, reg_status, node_address, wallet_address,
        "provided" if gateway_ca_cert else "not provided",
    )

    # Request a health probe so the Coordination API can verify us
    await request_probe(http_client, settings, node_id, identity_key=identity_key)

    return node_id, gateway_ca_cert


def save_gateway_ca_cert(pem_data: str, path: str) -> None:
    """Write the gateway CA certificate PEM to disk."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(pem_data)
    os.chmod(path, 0o644)
    logger.info("Gateway CA certificate saved to %s", path)


async def request_probe(
    http_client: httpx.AsyncClient,
    settings: Settings,
    node_id: str,
    *,
    identity_key: str,
) -> None:
    """Request a health probe from the Coordination API (signed)."""
    signature, timestamp = sign_request(identity_key, "request_probe", node_id)

    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}/request-probe"
    try:
        resp = await http_client.post(url, json={
            "wallet_address": settings.WALLET_ADDRESS.lower(),
            "signature": signature,
            "timestamp": timestamp,
        }, timeout=10.0)
        if resp.status_code == 200:
            logger.info("Health probe requested for node %s — waiting for verification", node_id)
        elif resp.status_code == 400:
            logger.info("Probe request returned 400 (node may already be online): %s", resp.text)
        else:
            logger.warning("Probe request failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Failed to request probe for node %s: %s", node_id, exc)


async def deregister_node(
    http_client: httpx.AsyncClient,
    settings: Settings,
    node_id: str,
    *,
    identity_key: str,
) -> None:
    """Set node status to offline (signed). Best-effort."""
    signature, timestamp = sign_request(identity_key, "update_status", node_id)

    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}/status"
    try:
        resp = await http_client.patch(url, json={
            "status": "offline",
            "wallet_address": settings.WALLET_ADDRESS.lower(),
            "signature": signature,
            "timestamp": timestamp,
        }, timeout=10.0)
        resp.raise_for_status()
        logger.info("Deregistered node %s (status → offline)", node_id)
    except Exception as exc:
        logger.warning("Failed to deregister node %s: %s", node_id, exc)
