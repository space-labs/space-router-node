"""Tests for X-SpaceRouter-Request-Id tracing (Issue #51)."""

import asyncio
import os

import pytest

from app.config import Settings
from app.proxy_handler import (
    _bad_gateway,
    _bad_request,
    _error_response,
    _forbidden,
    _gateway_timeout,
    handle_client,
)

# Allow loopback targets for integration tests
os.environ.setdefault("SR_ALLOW_LOOPBACK_TARGETS", "1")


def _settings(**overrides) -> Settings:
    defaults = {
        "NODE_PORT": 0,
        "COORDINATION_API_URL": "http://localhost:8000",
        "PUBLIC_IP": "127.0.0.1",
        "UPNP_ENABLED": False,
        "REQUEST_TIMEOUT": 5.0,
        "RELAY_TIMEOUT": 5.0,
        "MAX_CONNECTIONS": 100,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class _MockWriter:
    def __init__(self):
        self.data = b""
        self._closed = False
        self._extra = {"peername": ("127.0.0.1", 9999)}

    def write(self, data: bytes):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self._closed = True

    async def wait_closed(self):
        pass

    def get_extra_info(self, key, default=None):
        return self._extra.get(key, default)


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# Error response helpers include request_id
# ---------------------------------------------------------------------------


class TestErrorResponseRequestId:
    def test_error_response_without_request_id(self):
        resp = _error_response(400, "Bad Request", "test")
        assert b"X-SpaceRouter-Request-Id" not in resp

    def test_error_response_with_request_id(self):
        resp = _error_response(400, "Bad Request", "test", request_id="abc-123")
        assert b"X-SpaceRouter-Request-Id: abc-123" in resp

    def test_bad_request_with_request_id(self):
        resp = _bad_request("test", request_id="rid-1")
        assert b"X-SpaceRouter-Request-Id: rid-1" in resp
        assert b"400 Bad Request" in resp

    def test_forbidden_with_request_id(self):
        resp = _forbidden("test", request_id="rid-2")
        assert b"X-SpaceRouter-Request-Id: rid-2" in resp
        assert b"403 Forbidden" in resp

    def test_bad_gateway_with_request_id(self):
        resp = _bad_gateway("test", request_id="rid-3")
        assert b"X-SpaceRouter-Request-Id: rid-3" in resp
        assert b"502 Bad Gateway" in resp

    def test_gateway_timeout_with_request_id(self):
        resp = _gateway_timeout("test", request_id="rid-4")
        assert b"X-SpaceRouter-Request-Id: rid-4" in resp
        assert b"504 Gateway Timeout" in resp

    def test_bad_request_without_request_id(self):
        resp = _bad_request("test")
        assert b"X-SpaceRouter-Request-Id" not in resp

    def test_forbidden_without_request_id(self):
        resp = _forbidden("test")
        assert b"X-SpaceRouter-Request-Id" not in resp


# ---------------------------------------------------------------------------
# Request-ID extraction and forwarding in handle_client
# ---------------------------------------------------------------------------


class TestRequestIdInHandleClient:
    @pytest.mark.asyncio
    async def test_request_id_in_forbidden_response(self):
        """When CONNECT targets a private IP, the error includes request_id."""
        settings = _settings()
        request = (
            b"CONNECT 127.0.0.1:80 HTTP/1.1\r\n"
            b"X-SpaceRouter-Request-Id: test-rid-001\r\n"
            b"\r\n"
        )
        reader = _make_reader(request)
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"403 Forbidden" in writer.data
        assert b"X-SpaceRouter-Request-Id: test-rid-001" in writer.data

    @pytest.mark.asyncio
    async def test_request_id_in_http_forward_forbidden(self):
        """HTTP forward to private target includes request_id in error."""
        settings = _settings()
        request = (
            b"GET http://192.168.1.1/ HTTP/1.1\r\n"
            b"Host: 192.168.1.1\r\n"
            b"X-SpaceRouter-Request-Id: test-rid-002\r\n"
            b"\r\n"
        )
        reader = _make_reader(request)
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"403 Forbidden" in writer.data
        assert b"X-SpaceRouter-Request-Id: test-rid-002" in writer.data

    @pytest.mark.asyncio
    async def test_no_request_id_still_works(self):
        """Without request_id header, error responses still work (no crash)."""
        settings = _settings()
        request = (
            b"CONNECT 127.0.0.1:80 HTTP/1.1\r\n"
            b"\r\n"
        )
        reader = _make_reader(request)
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"403 Forbidden" in writer.data
        assert b"X-SpaceRouter-Request-Id" not in writer.data

    @pytest.mark.asyncio
    async def test_lowercase_header_extraction(self):
        """Lowercase x-spacerouter-request-id is also extracted."""
        settings = _settings()
        request = (
            b"CONNECT 127.0.0.1:80 HTTP/1.1\r\n"
            b"x-spacerouter-request-id: lowercase-rid\r\n"
            b"\r\n"
        )
        reader = _make_reader(request)
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"X-SpaceRouter-Request-Id: lowercase-rid" in writer.data

    @pytest.mark.asyncio
    async def test_request_id_on_blocked_port(self):
        """CONNECT to blocked port includes request_id in error."""
        settings = _settings()
        request = (
            b"CONNECT example.com:22 HTTP/1.1\r\n"
            b"X-SpaceRouter-Request-Id: blocked-port-rid\r\n"
            b"\r\n"
        )
        reader = _make_reader(request)
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"403 Forbidden" in writer.data
        assert b"X-SpaceRouter-Request-Id: blocked-port-rid" in writer.data

    @pytest.mark.asyncio
    async def test_malformed_request_no_crash(self):
        """Malformed request (no headers parsed) doesn't crash."""
        settings = _settings()
        reader = _make_reader(b"GARBAGE\r\n")
        writer = _MockWriter()
        await handle_client(reader, writer, settings)
        assert b"400 Bad Request" in writer.data
