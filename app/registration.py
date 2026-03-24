"""Node registration with the Coordination API.

Lifecycle:
  1. detect_public_ip()   — determine the machine's public IP
  2. register_node()      — POST /nodes to announce ourselves (starts offline)
  3. request_probe()      — POST /nodes/{id}/request-probe to go online
  4. deregister_node()    — PATCH /nodes/{id}/status → offline on shutdown

Nodes cannot set themselves online directly.  The Coordination API
controls online status via health probes (OTP proxy-through challenge).
"""

import logging
import os

import httpx

from app.config import Settings

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
    upnp_endpoint: tuple[str, int] | None = None,
    wallet_address: str,
) -> tuple[str, str | None]:
    """Register this node with the Coordination API.

    If *upnp_endpoint* is provided (``(external_ip, external_port)``),
    the ``endpoint_url`` uses the UPnP-mapped address.  Otherwise falls
    back to the public IP with the configured port.

    Returns ``(node_id, gateway_ca_cert_pem_or_None)``.
    Raises on failure — the caller should abort startup.
    """
    if upnp_endpoint:
        upnp_ip, upnp_port = upnp_endpoint
        endpoint_url = f"https://{upnp_ip}:{upnp_port}"
        connectivity_type = "upnp"
    else:
        endpoint_url = f"https://{public_ip}:{settings.NODE_PORT}"
        connectivity_type = "direct"

    payload = {
        "endpoint_url": endpoint_url,
        "wallet_address": wallet_address,
        "connectivity_type": connectivity_type,
    }
    if settings.NODE_LABEL:
        payload["label"] = settings.NODE_LABEL

    url = f"{settings.COORDINATION_API_URL}/nodes"
    logger.info(
        "Registering node at %s → endpoint=%s public_ip=%s connectivity=%s",
        url, endpoint_url, public_ip, connectivity_type,
    )

    resp = await http_client.post(url, json=payload, timeout=15.0)
    if resp.status_code == 409:
        # Already registered with this wallet — recover the existing node_id
        logger.info("Node already registered (409), recovering existing registration…")
        list_resp = await http_client.get(
            f"{settings.COORDINATION_API_URL}/nodes",
            params={"wallet_address": wallet_address},
            timeout=10.0,
        )
        list_resp.raise_for_status()
        nodes = list_resp.json()
        matching = [n for n in nodes if n.get("wallet_address") == wallet_address]
        if not matching:
            raise RuntimeError("409 but no matching node found for wallet")
        data = matching[0]
    else:
        resp.raise_for_status()
        data = resp.json()
    node_id = data["id"]

    # Request a health probe so the Coordination API can verify us and
    # set our status to online.  The node cannot set itself online directly.
    await request_probe(http_client, settings, node_id)
    gateway_ca_cert = data.get("gateway_ca_cert")
    ip_type = data.get("ip_type", "unknown")
    ip_region = data.get("ip_region", "unknown")
    logger.info(
        "Registered as node %s (ip_type=%s, ip_region=%s, mtls_ca=%s)",
        node_id, ip_type, ip_region,
        "provided" if gateway_ca_cert else "not provided",
    )
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
) -> None:
    """Request a health probe from the Coordination API.

    The API will send an OTP challenge through this node to verify it can
    forward traffic.  If the probe passes, the node is marked online.
    This is fire-and-forget — the probe runs asynchronously on the server.
    """
    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}/request-probe"
    try:
        resp = await http_client.post(url, timeout=10.0)
        if resp.status_code == 200:
            logger.info("Health probe requested for node %s — waiting for verification", node_id)
        elif resp.status_code == 400:
            # Node might already be online
            logger.info("Probe request returned 400 (node may already be online): %s", resp.text)
        else:
            logger.warning("Probe request failed: %s %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Failed to request probe for node %s: %s", node_id, exc)


async def deregister_node(
    http_client: httpx.AsyncClient,
    settings: Settings,
    node_id: str,
) -> None:
    """Set node status to offline. Best-effort — failures are logged, not raised."""
    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}/status"
    try:
        resp = await http_client.patch(url, json={"status": "offline"}, timeout=10.0)
        resp.raise_for_status()
        logger.info("Deregistered node %s (status → offline)", node_id)
    except Exception as exc:
        logger.warning("Failed to deregister node %s: %s", node_id, exc)
