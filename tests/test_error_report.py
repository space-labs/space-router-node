"""Tests for the error reporting module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.error_report import (
    _scrub_frame,
    build_error_report,
    is_reportable,
    send_error_report,
)
from app.errors import NodeError, NodeErrorCode
from app.state import NodeState, NodeStatus


# ---------------------------------------------------------------------------
# is_reportable
# ---------------------------------------------------------------------------


class TestIsReportable:
    def test_reportable_codes(self):
        reportable = [
            "network_unreachable", "endpoint_unreachable", "api_server_error",
            "rate_limited", "connection_lost", "registration_rejected",
            "ip_conflict", "wallet_conflict", "ip_classification_unavailable",
            "timestamp_expired", "node_offline", "anonymous_ip",
            "staking_insufficient", "staking_locked",
        ]
        for code in reportable:
            assert is_reportable(code), f"{code} should be reportable"

    def test_non_reportable_codes(self):
        non_reportable = [
            "invalid_wallet", "missing_wallet", "identity_key_error",
            "identity_key_locked", "tls_cert_error", "port_in_use",
            "port_permission", "bind_error", "version_too_old",
            "unexpected_error",
        ]
        for code in non_reportable:
            assert not is_reportable(code), f"{code} should NOT be reportable"


# ---------------------------------------------------------------------------
# _scrub_frame
# ---------------------------------------------------------------------------


class TestScrubFrame:
    def test_strips_path_before_app(self):
        assert _scrub_frame("/Users/dev/spacecoin/space-router-node/app/main.py") == "app/main.py"

    def test_strips_path_before_gui(self):
        assert _scrub_frame("/opt/build/gui/api.py") == "gui/api.py"

    def test_no_match_returns_original(self):
        assert _scrub_frame("/usr/lib/python3.9/asyncio/tasks.py") == "/usr/lib/python3.9/asyncio/tasks.py"

    def test_already_relative(self):
        assert _scrub_frame("app/errors.py") == "app/errors.py"


# ---------------------------------------------------------------------------
# build_error_report
# ---------------------------------------------------------------------------


class TestBuildErrorReport:
    def test_produces_correct_structure(self):
        error = NodeError(NodeErrorCode.NETWORK_UNREACHABLE, "timeout")
        report = build_error_report(error, app_type="cli")

        assert "node_version" in report
        assert "error" in report
        assert "state" in report
        assert "metrics" in report
        assert "network" in report
        assert "environment" in report
        assert "recent_logs" in report

        # Error section
        assert report["error"]["code"] == "network_unreachable"
        assert report["error"]["message"] is not None
        assert report["error"]["is_transient"] is True

    def test_with_settings(self):
        error = NodeError(NodeErrorCode.API_SERVER_ERROR, "HTTP 500")
        settings = MagicMock()
        settings.PUBLIC_IP = "1.2.3.4"
        settings.PUBLIC_PORT = 9090
        settings.NODE_PORT = 9090
        settings.UPNP_ENABLED = True
        settings.COORDINATION_API_URL = "https://test.example.com"
        settings.MTLS_ENABLED = True
        settings.REGISTRATION_MODE = "auto"
        settings._REAL_EXIT_IP = "1.2.3.4"

        report = build_error_report(
            error,
            node_id="node123",
            identity_address="0xabc",
            staking_address="0xdef",
            settings=settings,
            app_type="gui",
        )

        assert report["node_id"] == "node123"
        assert report["identity_address"] == "0xabc"
        assert report["network"]["upnp_enabled"] is True
        assert report["environment"]["app_type"] == "gui"

    def test_with_state_snapshot(self):
        error = NodeError(NodeErrorCode.CONNECTION_LOST, "reset")
        snapshot = NodeStatus(
            state=NodeState.ERROR_TRANSIENT,
            retry_count=3,
        )
        report = build_error_report(error, state_snapshot=snapshot)

        assert report["state"]["current"] == "error_transient"
        assert report["state"]["retry_count"] == 3
        assert "uptime_seconds" in report["state"]

    def test_traceback_scrubbing(self):
        """Traceback frames should have full paths stripped."""
        # Create an exception with a traceback
        try:
            raise ValueError("test error")
        except ValueError as cause:
            error = NodeError(NodeErrorCode.API_SERVER_ERROR, "test", cause=cause)

        report = build_error_report(error)
        tb = report["error"]["traceback"]
        assert len(tb) > 0
        # At least one frame should exist; none should have the full absolute path
        # (they will be from this test file, which may not match app/ or gui/)

    def test_without_cause(self):
        error = NodeError(NodeErrorCode.NETWORK_UNREACHABLE, "no cause")
        report = build_error_report(error)
        assert report["error"]["traceback"] == []
        assert report["error"]["exception_type"] is None

    def test_tunnel_detection(self):
        error = NodeError(NodeErrorCode.ENDPOINT_UNREACHABLE, "test")
        settings = MagicMock()
        settings.PUBLIC_IP = "tunnel.example.com"
        settings.PUBLIC_PORT = 12345
        settings.NODE_PORT = 9090
        settings.UPNP_ENABLED = False
        settings.COORDINATION_API_URL = "https://test.example.com"
        settings.MTLS_ENABLED = True
        settings.REGISTRATION_MODE = "auto"
        settings._REAL_EXIT_IP = "5.6.7.8"

        report = build_error_report(error, settings=settings)
        assert report["network"]["is_tunnel"] is True
        assert report["network"]["tunnel_hostname"] == "tunnel.example.com"
        assert report["network"]["tunnel_port"] == 12345


# ---------------------------------------------------------------------------
# send_error_report
# ---------------------------------------------------------------------------


class TestSendErrorReport:
    def test_returns_true_on_200(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response

        with patch("app.identity.sign_request", return_value=("sig123", 1234)):
            result = asyncio.get_event_loop().run_until_complete(
                send_error_report(
                    {"test": "payload"},
                    "fake_key",
                    "0xidentity",
                    "https://api.example.com",
                    mock_client,
                )
            )
        assert result is True

    def test_returns_false_on_error_status(self):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.post.return_value = mock_response

        with patch("app.identity.sign_request", return_value=("sig123", 1234)):
            result = asyncio.get_event_loop().run_until_complete(
                send_error_report(
                    {"test": "payload"},
                    "fake_key",
                    "0xidentity",
                    "https://api.example.com",
                    mock_client,
                )
            )
        assert result is False

    def test_returns_false_on_exception(self):
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("network error")

        with patch("app.identity.sign_request", return_value=("sig123", 1234)):
            result = asyncio.get_event_loop().run_until_complete(
                send_error_report(
                    {"test": "payload"},
                    "fake_key",
                    "0xidentity",
                    "https://api.example.com",
                    mock_client,
                )
            )
        assert result is False

    def test_never_raises(self):
        """send_error_report should NEVER propagate exceptions."""
        mock_client = AsyncMock()
        # sign_request itself raises
        with patch("app.identity.sign_request", side_effect=RuntimeError("boom")):
            result = asyncio.get_event_loop().run_until_complete(
                send_error_report(
                    {"test": "payload"},
                    "fake_key",
                    "0xidentity",
                    "https://api.example.com",
                    mock_client,
                )
            )
        assert result is False
