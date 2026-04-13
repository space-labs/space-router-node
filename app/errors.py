"""Classified node errors with user-friendly messages.

Every error the node can encounter is mapped to a NodeErrorCode with a
human-readable message and a flag indicating whether automatic retry is
appropriate.
"""

from __future__ import annotations

import enum
import logging

import httpx

logger = logging.getLogger(__name__)


class NodeErrorCode(enum.Enum):
    # ── Config / identity errors (permanent) ──
    INVALID_WALLET = "invalid_wallet"
    MISSING_WALLET = "missing_wallet"
    IDENTITY_KEY_ERROR = "identity_key_error"
    IDENTITY_KEY_LOCKED = "identity_key_locked"
    TLS_CERT_ERROR = "tls_cert_error"

    # ── Network / binding errors ──
    PORT_IN_USE = "port_in_use"
    PORT_PERMISSION = "port_permission"
    BIND_ERROR = "bind_error"

    # ── Registration errors ──
    NETWORK_UNREACHABLE = "network_unreachable"
    REGISTRATION_REJECTED = "registration_rejected"
    IP_CONFLICT = "ip_conflict"
    WALLET_CONFLICT = "wallet_conflict"
    API_SERVER_ERROR = "api_server_error"
    VERSION_TOO_OLD = "version_too_old"
    IP_CLASSIFICATION_UNAVAILABLE = "ip_classification_unavailable"
    TIMESTAMP_EXPIRED = "timestamp_expired"
    STAKING_INSUFFICIENT = "staking_insufficient"
    STAKING_LOCKED = "staking_locked"
    ANONYMOUS_IP = "anonymous_ip"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"

    # ── Runtime errors ──
    NODE_OFFLINE = "node_offline"
    UNEXPECTED_ERROR = "unexpected_error"


_USER_MESSAGES: dict[NodeErrorCode, str] = {
    NodeErrorCode.INVALID_WALLET: "Wallet address is invalid. Check Settings.",
    NodeErrorCode.MISSING_WALLET: "No wallet address configured.",
    NodeErrorCode.IDENTITY_KEY_ERROR: "Cannot load node identity key. Try Fresh Restart.",
    NodeErrorCode.IDENTITY_KEY_LOCKED: "Identity key is encrypted. Passphrase required to unlock.",
    NodeErrorCode.TLS_CERT_ERROR: "Cannot create TLS certificates. Check disk permissions.",
    NodeErrorCode.PORT_IN_USE: "Port is already in use. Retrying...",
    NodeErrorCode.PORT_PERMISSION: "Permission denied for port. Try a port above 1024.",
    NodeErrorCode.BIND_ERROR: "Cannot start server.",
    NodeErrorCode.NETWORK_UNREACHABLE: "Cannot reach coordination server. Retrying...",
    NodeErrorCode.REGISTRATION_REJECTED: "Registration rejected by server. Check wallet and environment.",
    NodeErrorCode.IP_CONFLICT: "Another node is already using this IP address. Only one node per IP is allowed.",
    NodeErrorCode.WALLET_CONFLICT: "Wallet address is already registered to another node.",
    NodeErrorCode.API_SERVER_ERROR: "Coordination server error. Retrying...",
    NodeErrorCode.VERSION_TOO_OLD: "This version is outdated. Please download the latest update.",
    NodeErrorCode.IP_CLASSIFICATION_UNAVAILABLE: "IP classification service temporarily unavailable. Retrying...",
    NodeErrorCode.TIMESTAMP_EXPIRED: "Request timestamp expired. Retrying... (check your system clock if this persists)",
    NodeErrorCode.STAKING_INSUFFICIENT: "Insufficient SPACE staked. Check your staking balance.",
    NodeErrorCode.STAKING_LOCKED: "Staking account is locked. Unlock your stake on-chain.",
    NodeErrorCode.ANONYMOUS_IP: "Anonymous IP detected. VPN, proxy, and Tor connections are not allowed.",
    NodeErrorCode.ENDPOINT_UNREACHABLE: "Coordination server cannot reach this node. Retrying...",
    NodeErrorCode.NODE_OFFLINE: "Node went offline. Reconnecting...",
    NodeErrorCode.UNEXPECTED_ERROR: "An unexpected error occurred.",
}

_TRANSIENT_CODES = frozenset({
    NodeErrorCode.PORT_IN_USE,
    NodeErrorCode.NETWORK_UNREACHABLE,
    NodeErrorCode.API_SERVER_ERROR,
    NodeErrorCode.NODE_OFFLINE,
    NodeErrorCode.IP_CLASSIFICATION_UNAVAILABLE,
    NodeErrorCode.TIMESTAMP_EXPIRED,
    NodeErrorCode.ENDPOINT_UNREACHABLE,
})


class NodeError(Exception):
    """A classified node error with a user-friendly message."""

    def __init__(
        self,
        code: NodeErrorCode,
        detail: str = "",
        cause: Exception | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.cause = cause
        self.user_message = _USER_MESSAGES.get(code, "An error occurred.")
        self.is_transient = code in _TRANSIENT_CODES
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


def _extract_server_detail(exc: httpx.HTTPStatusError) -> str:
    """Extract the ``detail`` field from a FastAPI JSON error response."""
    try:
        return exc.response.json().get("detail", "")
    except Exception:
        return ""


def classify_error(exc: Exception) -> NodeError:
    """Map a raw exception to a classified NodeError."""

    # ── OSError (port binding) ──
    if isinstance(exc, OSError):
        err_num = getattr(exc, "errno", None)
        # errno 48 (macOS) / 98 (Linux) = address already in use
        if err_num in (48, 98):
            logger.warning("Error classified: PORT_IN_USE errno=%s: %s", err_num, exc)
            return NodeError(NodeErrorCode.PORT_IN_USE, str(exc), cause=exc)
        # errno 13 = permission denied
        if err_num == 13:
            logger.warning("Error classified: PORT_PERMISSION errno=%s: %s", err_num, exc)
            return NodeError(NodeErrorCode.PORT_PERMISSION, str(exc), cause=exc)
        logger.warning("Error classified: BIND_ERROR errno=%s: %s", err_num, exc)
        return NodeError(NodeErrorCode.BIND_ERROR, str(exc), cause=exc)

    # ── httpx errors (registration / health check) ──
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        server_detail = _extract_server_detail(exc)

        # 426 — node version below minimum required
        if status == 426:
            logger.warning("Error classified: VERSION_TOO_OLD (HTTP 426): %s", server_detail)
            return NodeError(
                NodeErrorCode.VERSION_TOO_OLD,
                server_detail or exc.response.text[:200],
                cause=exc,
            )

        # 424 — IPinfo / external dependency unavailable (transient)
        if status == 424:
            logger.warning("Error classified: IP_CLASSIFICATION_UNAVAILABLE (HTTP 424): %s", server_detail)
            return NodeError(
                NodeErrorCode.IP_CLASSIFICATION_UNAVAILABLE,
                server_detail or "IP classification service unreachable",
                cause=exc,
            )

        # 409 — distinguish IP conflict vs wallet conflict
        if status == 409:
            body = (server_detail or exc.response.text[:300]).lower()
            if "ip" in body and ("already registered" in body or "already" in body):
                logger.warning("Error classified: IP_CONFLICT (HTTP 409): %s", server_detail)
                return NodeError(
                    NodeErrorCode.IP_CONFLICT,
                    server_detail or exc.response.text[:200],
                    cause=exc,
                )
            if "staking_address" in body or "collection_address" in body or "wallet" in body:
                logger.warning("Error classified: WALLET_CONFLICT (HTTP 409): %s", server_detail)
                return NodeError(
                    NodeErrorCode.WALLET_CONFLICT,
                    server_detail or exc.response.text[:200],
                    cause=exc,
                )
            # Fallback for unknown 409
            logger.warning("Error classified: REGISTRATION_REJECTED (HTTP 409 unknown): %s", server_detail)
            return NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP 409: {server_detail or exc.response.text[:200]}",
                cause=exc,
            )

        # 422 — endpoint challenge verification or malformed request
        if status == 422:
            body = (server_detail or exc.response.text[:300]).lower()
            if "endpoint verification" in body or "connection_refused" in body or "timed out" in body:
                logger.warning("Error classified: ENDPOINT_UNREACHABLE (HTTP 422): %s", server_detail)
                err = NodeError(
                    NodeErrorCode.ENDPOINT_UNREACHABLE,
                    server_detail or "Endpoint verification failed",
                    cause=exc,
                )
                if server_detail:
                    err.user_message = server_detail
                return err
            logger.warning("Error classified: REGISTRATION_REJECTED (HTTP 422): %s", server_detail)
            return NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP {status}: {server_detail or exc.response.text[:200]}",
                cause=exc,
            )

        # 403 — distinguish specific rejection reasons
        if status == 403:
            body = (server_detail or exc.response.text[:300]).lower()

            if "timestamp" in body and "expired" in body:
                logger.warning("Error classified: TIMESTAMP_EXPIRED (HTTP 403): %s", server_detail)
                return NodeError(
                    NodeErrorCode.TIMESTAMP_EXPIRED,
                    server_detail or "Timestamp expired",
                    cause=exc,
                )

            if "insufficient stake" in body:
                logger.warning("Error classified: STAKING_INSUFFICIENT (HTTP 403): %s", server_detail)
                err = NodeError(
                    NodeErrorCode.STAKING_INSUFFICIENT,
                    server_detail,
                    cause=exc,
                )
                if server_detail:
                    err.user_message = server_detail
                return err

            if "locked" in body:
                logger.warning("Error classified: STAKING_LOCKED (HTTP 403): %s", server_detail)
                err = NodeError(
                    NodeErrorCode.STAKING_LOCKED,
                    server_detail,
                    cause=exc,
                )
                if server_detail:
                    err.user_message = server_detail
                return err

            if "anonymous" in body or "vpn" in body or "proxy" in body or "tor" in body:
                logger.warning("Error classified: ANONYMOUS_IP (HTTP 403): %s", server_detail)
                err = NodeError(
                    NodeErrorCode.ANONYMOUS_IP,
                    server_detail,
                    cause=exc,
                )
                if server_detail:
                    err.user_message = server_detail
                return err

            # Other 403 — generic rejection with server detail
            logger.warning("Error classified: REGISTRATION_REJECTED (HTTP 403): %s", server_detail)
            err = NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP 403: {server_detail or exc.response.text[:200]}",
                cause=exc,
            )
            if server_detail:
                err.user_message = server_detail
            return err

        # 400, 401 — permanent rejections with server detail
        if status in (400, 401):
            logger.warning("Error classified: REGISTRATION_REJECTED (HTTP %d): %s", status, server_detail)
            err = NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP {status}: {server_detail or exc.response.text[:200]}",
                cause=exc,
            )
            if server_detail:
                err.user_message = server_detail
            return err

        # 429, 408 — transient rate-limit or timeout
        if status in (429, 408):
            logger.warning("Error classified: NETWORK_UNREACHABLE (HTTP %d transient)", status)
            return NodeError(
                NodeErrorCode.NETWORK_UNREACHABLE,
                f"HTTP {status}: transient",
                cause=exc,
            )

        # 5xx — server errors (transient)
        if status >= 500:
            logger.warning("Error classified: API_SERVER_ERROR (HTTP %d): %s", status, server_detail)
            return NodeError(
                NodeErrorCode.API_SERVER_ERROR,
                f"HTTP {status}: {server_detail or 'server error'}",
                cause=exc,
            )

        # Catch-all for any other unhandled HTTP status
        logger.warning("Error classified: REGISTRATION_REJECTED (HTTP %d unhandled): %s", status, server_detail)
        return NodeError(
            NodeErrorCode.REGISTRATION_REJECTED,
            f"HTTP {status}: {server_detail or exc.response.text[:200]}",
            cause=exc,
        )

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        logger.warning("Error classified: NETWORK_UNREACHABLE (connect error): %s", exc)
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        logger.warning("Error classified: NETWORK_UNREACHABLE (timeout/network): %s", exc)
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        logger.warning("Error classified: NETWORK_UNREACHABLE (connection refused/reset): %s", exc)
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    # ── ValueError (wallet validation, key parsing) ──
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        if "wallet" in msg or "address" in msg:
            logger.warning("Error classified: INVALID_WALLET: %s", exc)
            return NodeError(NodeErrorCode.INVALID_WALLET, str(exc), cause=exc)
        if "key" in msg or "identity" in msg:
            logger.warning("Error classified: IDENTITY_KEY_ERROR: %s", exc)
            return NodeError(NodeErrorCode.IDENTITY_KEY_ERROR, str(exc), cause=exc)

    # ── Fallback ──
    logger.warning("Error classified: UNEXPECTED_ERROR (unhandled %s): %s", type(exc).__name__, exc)
    return NodeError(NodeErrorCode.UNEXPECTED_ERROR, str(exc), cause=exc)
