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
import re
from typing import Literal, NamedTuple

import httpx

from app.config import Settings
from app.identity import sign_request, sign_vouch
from app.version import __version__

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
            logger.warning("IP detection failed via %s: %s", url, exc)

    logger.error("Public IP detection failed: all %d services unreachable", len(_IP_SERVICES))
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

    if settings.REFERRAL_CODE:
        payload["referral_code"] = settings.REFERRAL_CODE

    payload["node_version"] = __version__

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
    probe_ok = await request_probe(http_client, settings, node_id, identity_key=identity_key)
    if not probe_ok:
        logger.warning(
            "Initial health probe request failed for node %s — "
            "node is registered but may take longer to come online",
            node_id,
        )

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


class ProbeRequestResult(NamedTuple):
    """Outcome of a request_probe() call.

    - ``outcome="ok"``       → probe accepted (200) or node already online (400).
    - ``outcome="rate_limited"`` → server returned 429; ``retry_after_seconds``
      carries the server's hint (parsed from the ``detail`` field) so the
      caller can honour it instead of guessing a backoff.
    - ``outcome="failed"``   → other 4xx/5xx or network/parse exception.
    """

    outcome: Literal["ok", "rate_limited", "failed"]
    retry_after_seconds: int | None  # only set when outcome == "rate_limited"


# Default retry-after when the server's detail string can't be parsed.
_DEFAULT_RATE_LIMIT_RETRY_S = 300

# Server detail format: "Probe already requested recently. Try again in {N}s."
_RETRY_AFTER_RE = re.compile(r"Try again in (\d+)s")


async def request_probe(
    http_client: httpx.AsyncClient,
    settings: Settings,
    node_id: str,
    *,
    identity_key: str,
) -> ProbeRequestResult:
    """Request a health probe from the Coordination API (signed).

    Returns a :class:`ProbeRequestResult` describing the outcome:

    - ``ok``           on 200 (probe queued) or 400 (already online).
    - ``rate_limited`` on 429, with ``retry_after_seconds`` parsed from the
      server's ``detail`` field (defaults to 300s when parsing fails).
    - ``failed``       on any other status or exception.

    The structured return lets the caller respect the server's retry hint
    rather than applying a blind exponential backoff that compounds with
    the server's own rate limit.
    """
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
            return ProbeRequestResult("ok", None)
        if resp.status_code == 400:
            logger.info("Probe request returned 400 (node may already be online): %s", resp.text)
            return ProbeRequestResult("ok", None)
        if resp.status_code == 429:
            retry_after = _DEFAULT_RATE_LIMIT_RETRY_S
            try:
                detail = resp.json().get("detail", "")
                m = _RETRY_AFTER_RE.search(detail or "")
                if m:
                    retry_after = int(m.group(1))
            except Exception:
                # Body wasn't JSON or didn't contain detail — fall back to default.
                pass
            logger.warning(
                "Probe request rate-limited (429) for node %s — retry in %ds",
                node_id, retry_after,
            )
            return ProbeRequestResult("rate_limited", retry_after)
        logger.warning("Probe request failed: %s %s", resp.status_code, resp.text)
        return ProbeRequestResult("failed", None)
    except Exception as exc:
        logger.warning("Failed to request probe for node %s: %s", node_id, exc)
        return ProbeRequestResult("failed", None)


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
    """Set node status to offline (signed).

    Raises on HTTP/network failure so callers can distinguish a real
    "coord said offline" from a "we tried and the server 500'd". rc.8 #7b:
    the previous swallow-and-warn made ``deregister_best_effort_sync``
    report success on coord 500s, which surfaced as a misleading
    "Notified coordination API (status → offline)" line during --reset.
    Wrap calls in a try/except at the policy layer (e.g.
    ``deregister_best_effort_sync``) when failure must not abort the
    surrounding flow.
    """
    signature, timestamp = sign_request(identity_key, "update_status", node_id)

    url = f"{settings.COORDINATION_API_URL}/nodes/{node_id}/status"
    resp = await http_client.patch(url, json={
        "status": "offline",
        "wallet_address": _effective_wallet(settings),
        "signature": signature,
        "timestamp": timestamp,
    }, timeout=10.0)
    resp.raise_for_status()
    logger.info("Deregistered node %s (status → offline)", node_id)


def deregister_best_effort_sync(settings: Settings) -> bool:
    """Synchronous best-effort deregister — load identity from disk and
    PATCH status to offline.

    rc.6 MAJ-3: Reset Node / fresh restart didn't tell the coord we
    were going away, so the operator's node hung in "online" state on
    the dashboard for the full health-check timeout (~3 min) after a
    reset. This helper bridges the sync reset paths to the async
    deregister_node helper.

    Returns True on best-effort success (HTTP call dispatched), False
    on any failure (no identity, network error, etc.). The caller must
    NOT block reset on the return value — this is purely informational.
    """
    import asyncio

    try:
        from app.identity import load_or_create_identity
    except Exception as exc:
        # rc.10 #3: do NOT pass exc_info=True here — --reset runs against a
        # CLI logger whose StreamHandler would dump the full traceback
        # (httpx HTTPStatusError + Mozilla URL hint) to stderr BEFORE
        # _do_reset prints its honest "Coord deregister failed (likely
        # server issue)" message, scaring operators who just want to wipe.
        logger.warning(
            "Cannot import identity module for reset-time deregister: %s",
            exc,
        )
        return False

    try:
        identity_key, identity_address = load_or_create_identity(
            settings.IDENTITY_KEY_PATH,
            settings.IDENTITY_PASSPHRASE,
        )
    except Exception as exc:
        logger.warning(
            "Could not load identity for reset-time deregister; skipping: %s",
            exc,
        )
        return False

    async def _run() -> None:
        async with httpx.AsyncClient() as client:
            await deregister_node(
                client, settings, identity_address,
                identity_key=identity_key,
            )

    try:
        asyncio.run(_run())
        return True
    except Exception as exc:
        logger.warning(
            "Best-effort deregister failed; continuing with reset: %s",
            exc,
        )
        return False
