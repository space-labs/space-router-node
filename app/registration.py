"""Node registration with the Coordination API.

Supports two protocol versions selected via ``SR_REGISTRATION_MODE``:

- **v1** (v0.1.2): Single ``wallet_address`` + ``identity_signature``.
- **v2** (v0.2.0): Multi-wallet with ``staking_address``,
  ``collection_address``, and ``staking_vouching_signature``.
- **auto** (default): v0.2.0 when staking_address is set, v0.1.2 otherwise.

Lifecycle:
  1. detect_public_ip()   — determine the machine's public IP
  2. register_node()      — dispatch to v1 or v2 registration
  3. request_probe()      — POST /nodes/{id}/request-probe (signed)
  4. deregister_node()    — PATCH /nodes/{id}/status → offline (signed)

All authenticated calls are signed with the node's identity private key.
"""

import logging
import os

import httpx

from app.config import Settings
from app.identity import sign_request, sign_vouch

logger = logging.getLogger(__name__)

# Services tried in order for IP detection
_IP_SERVICES = [
    ("https://httpbin.org/ip", "origin"),
    ("https://api.ipify.org?format=json", "ip"),
    ("https://ifconfig.me/ip", None),  # plain-text response
]

# Tracks which registration mode actually succeeded so deregistration
# can match the protocol.  Set by register_node() after success.
_active_mode: str | None = None


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


# ---------------------------------------------------------------------------
# v0.1.2 registration (legacy)
# ---------------------------------------------------------------------------

async def _do_register(
    http_client: httpx.AsyncClient,
    settings: Settings,
    public_ip: str,
    *,
    identity_key: str,
    wallet_address: str,
    staking_address: str = "",
    collection_address: str = "",
    upnp_endpoint: tuple | None = None,
) -> tuple[str, str | None]:
    """Register this node with the Coordination API.

    Uses the unified ``POST /nodes/register`` endpoint with an identity
    signature.  The server recovers the node identity address from the
    signature.

    When *staking_address* is provided, sends the v0.2.0 multi-wallet
    payload (staking_address + collection_address + vouch signature).
    Otherwise falls back to the v0.1.2 single wallet_address format.

    Returns ``(node_id, gateway_ca_cert_pem_or_None)``.
    Raises on failure — the caller should abort startup.
    """
    if upnp_endpoint:
        upnp_ip, upnp_port = upnp_endpoint
        endpoint_url = f"https://{upnp_ip}:{upnp_port}"
    else:
        advertised_port = settings.PUBLIC_PORT if settings.PUBLIC_PORT else settings.NODE_PORT
        endpoint_url = f"https://{public_ip}:{advertised_port}"

    use_v2 = bool(staking_address)

    # For tunnel setups (ngrok, bore), public_ip is the tunnel hostname
    # but real_exit_ip is the node's actual residential IP for classification
    real_exit_ip = getattr(settings, "_REAL_EXIT_IP", None)

    if use_v2:
        effective_collection = collection_address or staking_address

        # Both signatures share the same timestamp — the Coordination
        # API verifies both against the single body.timestamp value.
        signature, timestamp = sign_request(
            identity_key, "register", staking_address,
        )
        vouch_signature, _ = sign_vouch(
            identity_key, staking_address, effective_collection, timestamp=timestamp,
        )

        payload = {
            "staking_address": staking_address,
            "collection_address": effective_collection,
            "staking_vouching_signature": vouch_signature,
            "endpoint_url": endpoint_url,
            "identity_signature": signature,
            "timestamp": timestamp,
        }
        log_wallet = f"staking={staking_address}, collection={effective_collection}"
    else:
        # v0.1.2 fallback — single wallet_address
        signature, timestamp = sign_request(
            identity_key, "register", wallet_address,
        )
        payload = {
            "wallet_address": wallet_address,
            "endpoint_url": endpoint_url,
            "identity_signature": signature,
            "timestamp": timestamp,
        }
        log_wallet = f"wallet={wallet_address}"

    # Send real exit IP for IPinfo classification (tunnel mode)
    if real_exit_ip:
        payload["public_ip"] = real_exit_ip

    if settings.NODE_LABEL:
        payload["label"] = settings.NODE_LABEL

    url = f"{settings.COORDINATION_API_URL}/nodes/register"
    logger.info(
        "Registering node at %s → endpoint=%s %s (protocol=%s)",
        url, endpoint_url, log_wallet, "v0.2.0" if use_v2 else "v0.1.2",
    )

    resp = await http_client.post(url, json=payload, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    node_id = data["node_id"]
    gateway_ca_cert = data.get("gateway_ca_cert")
    identity_address = data.get("identity_address") or data.get("node_address", "unknown")
    reg_status = data.get("status", "registered")

    logger.info(
        "Registered as node %s (status=%s, identity=%s, %s, mtls_ca=%s)",
        node_id, reg_status, identity_address, log_wallet,
        "provided" if gateway_ca_cert else "not provided",
    )

    # Request a health probe so the Coordination API can verify us
    await request_probe(http_client, settings, node_id, identity_key=identity_key)

    return node_id, gateway_ca_cert


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

async def register_node(
    http_client: httpx.AsyncClient,
    settings: Settings,
    public_ip: str,
    *,
    identity_key: str,
    wallet_address: str,
    staking_address: str = "",
    collection_address: str = "",
    upnp_endpoint: tuple | None = None,
) -> tuple[str, str | None]:
    """Register this node with the Coordination API.

    Uses ``settings.REGISTRATION_MODE`` to decide protocol version:

    - **auto** (default): sends v0.2.0 multi-wallet payload when
      staking_address is provided, otherwise v0.1.2 single-wallet.
    - **v1**: forces v0.1.2 single-wallet (ignores staking_address).
    - **v2**: forces v0.2.0 multi-wallet (requires staking_address).

    Returns ``(node_id, gateway_ca_cert_pem_or_None)``.
    """
    global _active_mode  # noqa: PLW0603
    mode = settings.REGISTRATION_MODE

    if mode == "v1":
        # Force v0.1.2: ignore staking/collection, use wallet_address only
        result = await _do_register(
            http_client, settings, public_ip,
            identity_key=identity_key,
            wallet_address=wallet_address,
            upnp_endpoint=upnp_endpoint,
        )
        _active_mode = "v1"
        return result

    if mode == "v2":
        # Force v0.2.0: require staking_address
        if not staking_address:
            raise ValueError(
                "REGISTRATION_MODE=v2 requires SR_STAKING_ADDRESS to be set"
            )
        result = await _do_register(
            http_client, settings, public_ip,
            identity_key=identity_key,
            wallet_address=wallet_address,
            staking_address=staking_address,
            collection_address=collection_address,
            upnp_endpoint=upnp_endpoint,
        )
        _active_mode = "v2"
        return result

    # auto: use v0.2.0 when staking_address is set, v0.1.2 otherwise
    assert mode == "auto"
    result = await _do_register(
        http_client, settings, public_ip,
        identity_key=identity_key,
        wallet_address=wallet_address,
        staking_address=staking_address,
        collection_address=collection_address,
        upnp_endpoint=upnp_endpoint,
    )
    _active_mode = "v2" if staking_address else "v1"
    return result


def save_gateway_ca_cert(pem_data: str, path: str) -> None:
    """Write the gateway CA certificate PEM to disk."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(pem_data)
    os.chmod(path, 0o644)
    logger.info("Gateway CA certificate saved to %s", path)


def _effective_wallet(settings: Settings) -> str:
    """Return the best wallet address for authenticated requests."""
    return settings.STAKING_ADDRESS.lower()


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
            "wallet_address": _effective_wallet(settings),
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


async def check_node_status(
    http_client: httpx.AsyncClient,
    settings: Settings,
    node_id: str,
    *,
    identity_key: str,
) -> dict:
    """Check the node's status as seen by the coordination API.

    Returns the full node data dict containing at minimum:
    ``status``, ``health_score``, ``staking_status``.
    """
    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}"
    resp = await http_client.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


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
            "wallet_address": _effective_wallet(settings),
            "signature": signature,
            "timestamp": timestamp,
        }, timeout=10.0)
        resp.raise_for_status()
        logger.info("Deregistered node %s (status → offline)", node_id)
    except Exception as exc:
        logger.warning("Failed to deregister node %s: %s", node_id, exc)
