"""Opt-in error reporting for SpaceRouter Node.

Builds a diagnostic payload from the current node state and sends it to
the coordination API.  The report is ONLY sent when the user explicitly
consents (GUI modal or CLI prompt).

No private keys or personal data are included in the payload.
"""

from __future__ import annotations

import logging
import platform
import time
import traceback as _traceback_mod
from typing import Any

logger = logging.getLogger(__name__)

# Error codes eligible for reporting — excludes purely local config issues.
_REPORTABLE_CODES = frozenset({
    "network_unreachable",
    "endpoint_unreachable",
    "api_server_error",
    "rate_limited",
    "connection_lost",
    "registration_rejected",
    "ip_conflict",
    "wallet_conflict",
    "ip_classification_unavailable",
    "timestamp_expired",
    "node_offline",
    "anonymous_ip",
    "staking_insufficient",
    "staking_locked",
})


def is_reportable(error_code: str) -> bool:
    """Return True if the error code is eligible for opt-in reporting."""
    return error_code in _REPORTABLE_CODES


def _scrub_frame(filename: str) -> str:
    """Strip absolute path prefixes, keeping only ``app/…`` or ``gui/…``."""
    for prefix in ("app/", "gui/"):
        idx = filename.find(prefix)
        if idx != -1:
            return filename[idx:]
    return filename


def _format_traceback(cause: BaseException | None) -> list[str]:
    """Return a compact traceback from *cause* (max 15 frames)."""
    if cause is None or cause.__traceback__ is None:
        return []
    frames = _traceback_mod.extract_tb(cause.__traceback__)
    result: list[str] = []
    for frame in frames[-15:]:
        short = _scrub_frame(frame.filename)
        result.append(f"{short}:{frame.lineno} in {frame.name}")
    return result


def build_error_report(
    error: Any,  # NodeError
    *,
    node_id: str | None = None,
    identity_address: str | None = None,
    staking_address: str | None = None,
    collection_address: str | None = None,
    settings: Any | None = None,  # Settings
    upnp_endpoint: tuple | None = None,
    app_type: str = "cli",
    state_snapshot: Any | None = None,  # NodeStatus
) -> dict:
    """Build the error report payload.

    Accepts individual context fields so it works even when the full
    node context is unavailable (e.g. error during early init).
    """
    from app.node_logging import activity, get_recent_logs
    from app.variant import BUILD_VARIANT
    from app.version import __version__

    # ── error section ──
    cause = getattr(error, "cause", None)
    error_section: dict[str, Any] = {
        "code": getattr(error, "code", None) and error.code.value,
        "message": getattr(error, "user_message", str(error)),
        "detail": getattr(error, "detail", ""),
        "is_transient": getattr(error, "is_transient", False),
        "exception_type": type(cause).__name__ if cause else None,
        "traceback": _format_traceback(cause),
    }

    # ── state section ──
    state_section: dict[str, Any] = {}
    if state_snapshot is not None:
        state_section["current"] = state_snapshot.state.value
        state_section["retry_count"] = state_snapshot.retry_count
    uptime = time.time() - activity.start_time
    state_section["uptime_seconds"] = round(uptime, 1)

    # ── metrics section ──
    last_health_ago: float | None = None
    if activity.last_health_check is not None:
        last_health_ago = round(time.time() - activity.last_health_check, 1)

    metrics_section: dict[str, Any] = {
        "connections_served": activity.connections_served,
        "connections_active": activity.connections_active,
        "bytes_relayed": activity.bytes_relayed,
        "health_check_count": activity.health_check_count,
        "health_check_failures": activity.health_check_failures,
        "reconnect_count": activity.reconnect_count,
        "last_health_status": activity.last_health_status,
        "last_health_check_ago_seconds": last_health_ago,
    }

    # ── network section ──
    network_section: dict[str, Any] = {}
    if settings is not None:
        real_exit_ip = getattr(settings, "_REAL_EXIT_IP", None)
        is_tunnel = bool(settings.PUBLIC_IP and real_exit_ip and settings.PUBLIC_IP != real_exit_ip)

        endpoint_host = settings.PUBLIC_IP or "auto"
        endpoint_port = settings.PUBLIC_PORT or settings.NODE_PORT
        endpoint_url = f"{endpoint_host}:{endpoint_port}"

        network_section = {
            "upnp_enabled": settings.UPNP_ENABLED,
            "upnp_active": upnp_endpoint is not None,
            "public_ip_configured": bool(settings.PUBLIC_IP),
            "endpoint_url": endpoint_url,
            "node_port": settings.NODE_PORT,
            "public_port": settings.PUBLIC_PORT or settings.NODE_PORT,
            "is_tunnel": is_tunnel,
            "tunnel_hostname": settings.PUBLIC_IP if is_tunnel else None,
            "tunnel_port": (settings.PUBLIC_PORT or settings.NODE_PORT) if is_tunnel else None,
        }

    # ── environment section ──
    env_section: dict[str, Any] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "app_type": app_type,
        "build_variant": BUILD_VARIANT,
    }
    if settings is not None:
        env_section["coordination_api_url"] = settings.COORDINATION_API_URL
        env_section["mtls_enabled"] = settings.MTLS_ENABLED
        env_section["registration_mode"] = settings.REGISTRATION_MODE

    # ── assemble payload ──
    payload: dict[str, Any] = {
        "node_version": __version__,
        "node_id": node_id,
        "identity_address": identity_address,
        "staking_address": staking_address,
        "collection_address": collection_address,
        "error": error_section,
        "state": state_section,
        "metrics": metrics_section,
        "network": network_section,
        "environment": env_section,
        "recent_logs": get_recent_logs(),
    }
    return payload


async def send_error_report(
    report: dict,
    identity_key: str,
    identity_address: str,
    coordination_api_url: str,
    http_client: Any,  # httpx.AsyncClient
) -> bool:
    """POST the error report to the coordination API.

    Returns True on 2xx, False on any error. NEVER raises.
    """
    try:
        from app.identity import sign_request

        ts = int(time.time())
        message_target = f"{identity_address.lower()}:{ts}"
        signature, timestamp = sign_request(
            identity_key,
            "error_report",
            identity_address.lower(),
            timestamp=ts,
        )

        url = f"{coordination_api_url.rstrip('/')}/nodes/error-reports"
        body = {
            "signature": signature,
            "timestamp": timestamp,
            "identity_address": identity_address.lower(),
            "payload": report,
        }

        resp = await http_client.post(url, json=body, timeout=15.0)
        if 200 <= resp.status_code < 300:
            logger.info("Error report sent successfully")
            return True
        else:
            logger.warning("Error report rejected: HTTP %d", resp.status_code)
            return False
    except Exception as exc:
        logger.warning("Failed to send error report: %s", exc)
        return False


def send_error_report_sync(
    report: dict,
    identity_key: str,
    identity_address: str,
    coordination_api_url: str,
) -> bool:
    """Synchronous wrapper for sending an error report.

    Creates a temporary httpx client. Useful when the node's event loop
    is already closed (e.g. called from the GUI thread or CLI after shutdown).

    Returns True on 2xx, False on any error. NEVER raises.
    """
    try:
        import httpx

        from app.identity import sign_request

        ts = int(time.time())
        signature, timestamp = sign_request(
            identity_key,
            "error_report",
            identity_address.lower(),
            timestamp=ts,
        )

        url = f"{coordination_api_url.rstrip('/')}/nodes/error-reports"
        body = {
            "signature": signature,
            "timestamp": timestamp,
            "identity_address": identity_address.lower(),
            "payload": report,
        }

        with httpx.Client() as client:
            resp = client.post(url, json=body, timeout=15.0)
            if 200 <= resp.status_code < 300:
                logger.info("Error report sent successfully")
                return True
            else:
                logger.warning("Error report rejected: HTTP %d", resp.status_code)
                return False
    except Exception as exc:
        logger.warning("Failed to send error report: %s", exc)
        return False
