"""Tests for error classification in app.errors.classify_error()."""

from __future__ import annotations

import httpx
import pytest

from app.errors import NodeErrorCode, classify_error


# ── Helpers ──────────────────────────────────────────────────────────────

def _http_error(status_code: int, text: str = "", json_body: dict | None = None) -> httpx.HTTPStatusError:
    """Build a fake httpx.HTTPStatusError for a given status code."""
    body = ""
    if json_body is not None:
        import json
        body = json.dumps(json_body)
    else:
        body = text

    response = httpx.Response(
        status_code=status_code,
        text=body,
        request=httpx.Request("POST", "https://example.com/nodes/register"),
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=response.request,
        response=response,
    )


# ── HTTP 429 → RATE_LIMITED ──────────────────────────────────────────────

def test_http_429_rate_limited():
    err = classify_error(_http_error(429))
    assert err.code == NodeErrorCode.RATE_LIMITED
    assert err.is_transient is True
    assert "rate limited" in err.detail.lower()


# ── HTTP 408 → API_SERVER_ERROR ──────────────────────────────────────────

def test_http_408_server_timeout():
    err = classify_error(_http_error(408))
    assert err.code == NodeErrorCode.API_SERVER_ERROR
    assert err.is_transient is True
    assert "408" in err.detail


# ── httpx.ConnectError → NETWORK_UNREACHABLE ─────────────────────────────

def test_connect_error_network_unreachable():
    exc = httpx.ConnectError("Name or service not known")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.NETWORK_UNREACHABLE
    assert err.is_transient is True


def test_connect_timeout_network_unreachable():
    exc = httpx.ConnectTimeout("Timed out connecting")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.NETWORK_UNREACHABLE
    assert err.is_transient is True


# ── httpx.TimeoutException (non-connect) → CONNECTION_LOST ───────────────

def test_read_timeout_connection_lost():
    exc = httpx.ReadTimeout("Read timed out")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


def test_write_timeout_connection_lost():
    exc = httpx.WriteTimeout("Write timed out")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


def test_pool_timeout_connection_lost():
    exc = httpx.PoolTimeout("Pool timed out")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


# ── httpx.NetworkError (non-connect) → CONNECTION_LOST ───────────────────

def test_read_error_connection_lost():
    exc = httpx.ReadError("Connection reset by peer")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


def test_write_error_connection_lost():
    exc = httpx.WriteError("Broken pipe")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


# ── ConnectionRefusedError → API_SERVER_ERROR ────────────────────────────

def test_connection_refused_server_error():
    exc = ConnectionRefusedError("Connection refused")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.API_SERVER_ERROR
    assert err.is_transient is True


# ── ConnectionResetError → CONNECTION_LOST ───────────────────────────────

def test_connection_reset_connection_lost():
    exc = ConnectionResetError("Connection reset by peer")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.CONNECTION_LOST
    assert err.is_transient is True


# ── Verify existing classifications still work ───────────────────────────

def test_http_500_api_server_error():
    err = classify_error(_http_error(500))
    assert err.code == NodeErrorCode.API_SERVER_ERROR
    assert err.is_transient is True


def test_http_426_version_too_old():
    err = classify_error(_http_error(426, text="Upgrade required"))
    assert err.code == NodeErrorCode.VERSION_TOO_OLD
    assert err.is_transient is False


def test_http_409_ip_conflict():
    err = classify_error(_http_error(409, json_body={"detail": "IP already registered"}))
    assert err.code == NodeErrorCode.IP_CONFLICT


def test_http_409_wallet_conflict():
    err = classify_error(_http_error(409, json_body={"detail": "staking_address already registered"}))
    assert err.code == NodeErrorCode.WALLET_CONFLICT


def test_http_422_endpoint_unreachable():
    err = classify_error(_http_error(422, json_body={"detail": "Endpoint verification failed: timed out"}))
    assert err.code == NodeErrorCode.ENDPOINT_UNREACHABLE
    assert err.is_transient is True


def test_http_403_timestamp_expired():
    err = classify_error(_http_error(403, json_body={"detail": "Timestamp expired"}))
    assert err.code == NodeErrorCode.TIMESTAMP_EXPIRED
    assert err.is_transient is True


def test_http_403_staking_insufficient():
    err = classify_error(_http_error(403, json_body={"detail": "Insufficient stake: 0 < 1000"}))
    assert err.code == NodeErrorCode.STAKING_INSUFFICIENT
    assert err.is_transient is False


def test_http_403_anonymous_ip():
    err = classify_error(_http_error(403, json_body={"detail": "Anonymous VPN IP detected"}))
    assert err.code == NodeErrorCode.ANONYMOUS_IP
    assert err.is_transient is False


def test_port_in_use_macos():
    exc = OSError(48, "Address already in use")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.PORT_IN_USE


def test_port_permission_denied():
    exc = OSError(13, "Permission denied")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.PORT_PERMISSION


def test_unexpected_error_fallback():
    exc = RuntimeError("something unexpected")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.UNEXPECTED_ERROR
    assert err.is_transient is False


# ── Identity keystore passphrase classification ─────────────────────────


def test_keystore_wrong_passphrase_classified_as_locked():
    """Wrong passphrase must classify as IDENTITY_KEY_LOCKED so the GUI
    re-prompts — never as IDENTITY_KEY_ERROR ("Try Fresh Restart"),
    which would invite the user to destroy their identity in response
    to a typo. user_message is overridden to surface the "incorrect"
    distinction in CLI logs too (the GUI dialog reads status.detail).
    """
    from app.identity import KeystoreWrongPassphrase

    exc = KeystoreWrongPassphrase("passphrase is incorrect")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.IDENTITY_KEY_LOCKED
    assert "incorrect" in err.detail.lower()
    assert "incorrect" in err.user_message.lower()


def test_keystore_passphrase_required_classified_as_locked():
    """Missing-passphrase case keeps its existing IDENTITY_KEY_LOCKED
    classification — same dialog, slightly different prompt wording.
    """
    from app.identity import KeystorePassphraseRequired

    exc = KeystorePassphraseRequired("passphrase required")
    err = classify_error(exc)
    assert err.code == NodeErrorCode.IDENTITY_KEY_LOCKED


def test_identity_key_error_message_no_longer_promises_fresh_restart_blindly():
    """Pre-rc.3 the IDENTITY_KEY_ERROR message read "Try Fresh Restart"
    with no qualifier — the same text shown for wrong-passphrase cases
    where Fresh Restart deletes the identity. The new wording calls
    out the corruption case explicitly and warns about the re-stake
    consequence so the user makes an informed choice.
    """
    from app.errors import _USER_MESSAGES
    msg = _USER_MESSAGES[NodeErrorCode.IDENTITY_KEY_ERROR]
    # Must mention the corruption case so the user knows when this fires.
    assert "corrupt" in msg.lower()
    # Must warn about losing the wallet (re-stake).
    assert "stake" in msg.lower() or "wallet" in msg.lower()
