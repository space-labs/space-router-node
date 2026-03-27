"""Classified node errors with user-friendly messages.

Every error the node can encounter is mapped to a NodeErrorCode with a
human-readable message and a flag indicating whether automatic retry is
appropriate.
"""

import enum
import logging

import httpx

logger = logging.getLogger(__name__)


class NodeErrorCode(enum.Enum):
    # ── Config / identity errors (permanent) ──
    INVALID_WALLET = "invalid_wallet"
    MISSING_WALLET = "missing_wallet"
    IDENTITY_KEY_ERROR = "identity_key_error"
    TLS_CERT_ERROR = "tls_cert_error"

    # ── Network / binding errors ──
    PORT_IN_USE = "port_in_use"
    PORT_PERMISSION = "port_permission"
    BIND_ERROR = "bind_error"

    # ── Registration errors ──
    NETWORK_UNREACHABLE = "network_unreachable"
    REGISTRATION_REJECTED = "registration_rejected"
    API_SERVER_ERROR = "api_server_error"

    # ── Runtime errors ──
    NODE_OFFLINE = "node_offline"
    UNEXPECTED_ERROR = "unexpected_error"


_USER_MESSAGES: dict[NodeErrorCode, str] = {
    NodeErrorCode.INVALID_WALLET: "Wallet address is invalid. Check Settings.",
    NodeErrorCode.MISSING_WALLET: "No wallet address configured.",
    NodeErrorCode.IDENTITY_KEY_ERROR: "Cannot load node identity key. Try Fresh Restart.",
    NodeErrorCode.TLS_CERT_ERROR: "Cannot create TLS certificates. Check disk permissions.",
    NodeErrorCode.PORT_IN_USE: "Port is already in use. Retrying...",
    NodeErrorCode.PORT_PERMISSION: "Permission denied for port. Try a port above 1024.",
    NodeErrorCode.BIND_ERROR: "Cannot start server.",
    NodeErrorCode.NETWORK_UNREACHABLE: "Cannot reach coordination server. Retrying...",
    NodeErrorCode.REGISTRATION_REJECTED: "Registration rejected by server. Check wallet and environment.",
    NodeErrorCode.API_SERVER_ERROR: "Coordination server error. Retrying...",
    NodeErrorCode.NODE_OFFLINE: "Node went offline. Reconnecting...",
    NodeErrorCode.UNEXPECTED_ERROR: "An unexpected error occurred.",
}

_TRANSIENT_CODES = frozenset({
    NodeErrorCode.PORT_IN_USE,
    NodeErrorCode.NETWORK_UNREACHABLE,
    NodeErrorCode.API_SERVER_ERROR,
    NodeErrorCode.NODE_OFFLINE,
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


def classify_error(exc: Exception) -> NodeError:
    """Map a raw exception to a classified NodeError."""

    # ── OSError (port binding) ──
    if isinstance(exc, OSError):
        err_num = getattr(exc, "errno", None)
        # errno 48 (macOS) / 98 (Linux) = address already in use
        if err_num in (48, 98):
            return NodeError(NodeErrorCode.PORT_IN_USE, str(exc), cause=exc)
        # errno 13 = permission denied
        if err_num == 13:
            return NodeError(NodeErrorCode.PORT_PERMISSION, str(exc), cause=exc)
        return NodeError(NodeErrorCode.BIND_ERROR, str(exc), cause=exc)

    # ── httpx errors (registration / health check) ──
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 422 from challenge verification is transient (node may not be reachable yet)
        if status == 422:
            body = exc.response.text[:300].lower()
            if "endpoint verification" in body or "connection_refused" in body or "timed out" in body:
                return NodeError(
                    NodeErrorCode.NETWORK_UNREACHABLE,
                    f"Endpoint verification failed (node not reachable). Retrying...",
                    cause=exc,
                )
            return NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP {status}: {exc.response.text[:200]}",
                cause=exc,
            )
        if status in (400, 401, 403):
            return NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                f"HTTP {status}: {exc.response.text[:200]}",
                cause=exc,
            )
        if status >= 500:
            return NodeError(
                NodeErrorCode.API_SERVER_ERROR,
                f"HTTP {status}",
                cause=exc,
            )
        if status in (429, 408):
            return NodeError(
                NodeErrorCode.NETWORK_UNREACHABLE,
                f"HTTP {status}: transient",
                cause=exc,
            )
        # Catch-all for any other unhandled HTTP status
        return NodeError(
            NodeErrorCode.REGISTRATION_REJECTED,
            f"HTTP {status}: {exc.response.text[:200]}",
            cause=exc,
        )

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        return NodeError(NodeErrorCode.NETWORK_UNREACHABLE, str(exc), cause=exc)

    # ── ValueError (wallet validation, key parsing) ──
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        if "wallet" in msg or "address" in msg:
            return NodeError(NodeErrorCode.INVALID_WALLET, str(exc), cause=exc)
        if "key" in msg or "identity" in msg:
            return NodeError(NodeErrorCode.IDENTITY_KEY_ERROR, str(exc), cause=exc)

    # ── Fallback ──
    return NodeError(NodeErrorCode.UNEXPECTED_ERROR, str(exc), cause=exc)
