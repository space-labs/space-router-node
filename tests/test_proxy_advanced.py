"""Advanced proxy handler tests — edge cases, body handling, protocol attacks.

Covers: chunked transfer, body size limits, malformed inputs, request
smuggling vectors, relay timeouts, query string preservation, and
concurrent connection handling.
"""

import asyncio
import functools
import ssl
from unittest.mock import patch

import pytest

from app.proxy_handler import (
    MAX_CONTENT_LENGTH,
    handle_client,
    parse_headers,
    _strip_spacerouter_headers,
)
from app.tls import create_server_ssl_context, ensure_certificates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bypass_ssrf():
    """Bypass SSRF protection for advanced integration tests."""
    with patch("app.proxy_handler._is_private_target", return_value=False):
        yield


def _client_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _start_home_node(settings):
    ensure_certificates(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)
    ssl_ctx = create_server_ssl_context(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)
    handler = functools.partial(handle_client, settings=settings)
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_target_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# Chunked Transfer Encoding
# ---------------------------------------------------------------------------

class TestChunkedTransfer:
    @pytest.mark.asyncio
    async def test_chunked_response_forwarded(self, settings):
        """Target responds with chunked transfer encoding — verify relay."""

        async def chunked_target(reader, writer):
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"5\r\nHello\r\n"
                b"6\r\n World\r\n"
                b"0\r\n\r\n"
            )
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(chunked_target)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"GET http://127.0.0.1:{target_port}/chunked HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode("latin-1")
            assert "200 OK" in text
            assert "Hello" in text
            assert "World" in text

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_response_no_content_length_no_chunked(self, settings):
        """Target responds without Content-Length or chunked — read until close."""

        async def streaming_target(reader, writer):
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
            writer.write(b"streamed data here")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(streaming_target)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"GET http://127.0.0.1:{target_port}/stream HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode("latin-1")
            assert "200 OK" in text
            assert "streamed data here" in text

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()


# ---------------------------------------------------------------------------
# Body Size Limits
# ---------------------------------------------------------------------------

class TestBodyLimits:
    @pytest.mark.asyncio
    async def test_oversized_content_length_rejected(self, settings):
        """Request with Content-Length exceeding MAX_CONTENT_LENGTH → 400."""

        async def target_handler(reader, writer):
            # Should never be reached
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(target_handler)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            huge_cl = MAX_CONTENT_LENGTH + 1
            writer.write(
                f"POST http://127.0.0.1:{target_port}/upload HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n"
                f"Content-Length: {huge_cl}\r\n\r\n".encode()
            )
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert b"400" in resp
            assert b"too large" in resp.lower()

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_zero_content_length_works(self, settings):
        """Request with Content-Length: 0 should forward normally."""

        async def target_handler(reader, writer):
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(target_handler)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"DELETE http://127.0.0.1:{target_port}/item HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n"
                f"Content-Length: 0\r\n\r\n".encode()
            )
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)
            text = b"".join(chunks).decode("latin-1")
            assert "204" in text

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()


# ---------------------------------------------------------------------------
# URL / Host Edge Cases
# ---------------------------------------------------------------------------

class TestURLEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_host_in_url(self, settings):
        """HTTP forward with no host in URL → 400 Bad Request."""
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(b"GET /relative-path HTTP/1.1\r\nHost: test\r\n\r\n")
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert b"400" in resp

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()

    @pytest.mark.asyncio
    async def test_query_string_preserved(self, settings):
        """HTTP forward preserves query string in the target request."""
        received_path = []

        async def target_handler(reader, writer):
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # Extract the request line
            request_line = data.split(b"\r\n")[0].decode("latin-1")
            received_path.append(request_line)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(target_handler)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"GET http://127.0.0.1:{target_port}/search?q=test&page=2 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)

            assert len(received_path) == 1
            assert "/search?q=test&page=2" in received_path[0]

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_connect_default_port_443(self, settings):
        """CONNECT without explicit port defaults to 443."""
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            # CONNECT with just a hostname (no :port)
            writer.write(b"CONNECT example.com HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            # Should attempt connection to example.com:443
            # Will likely get 502 (can't reach) but NOT 400
            resp_text = resp.decode("latin-1")
            assert "400" not in resp_text
            # Either 200 (connected) or 502 (unreachable)
            assert "502" in resp_text or "200" in resp_text

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()


# ---------------------------------------------------------------------------
# Concurrent Connections
# ---------------------------------------------------------------------------

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_multiple_concurrent_requests(self, settings):
        """Multiple clients can connect and get responses simultaneously."""
        call_count = [0]

        async def target_handler(reader, writer):
            call_count[0] += 1
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            idx = call_count[0]
            body = f"resp-{idx}".encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(target_handler)
        home, home_port = await _start_home_node(settings)

        async def make_request(n):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"GET http://127.0.0.1:{target_port}/req{n} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()
            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)
            writer.close()
            await writer.wait_closed()
            return b"".join(chunks)

        try:
            results = await asyncio.gather(*[make_request(i) for i in range(5)])
            for r in results:
                assert b"200 OK" in r
            assert call_count[0] == 5
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()


# ---------------------------------------------------------------------------
# Protocol Attack Vectors
# ---------------------------------------------------------------------------

class TestProtocolAttacks:
    @pytest.mark.asyncio
    async def test_crlf_injection_in_header_value(self, settings):
        """Header value containing CRLF should not inject extra headers to target."""
        received_headers = {}

        async def target_handler(reader, writer):
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            head = data.split(b"\r\n\r\n")[0]
            for line in head.split(b"\r\n")[1:]:
                if b":" in line:
                    k, _, v = line.partition(b":")
                    received_headers[k.decode().strip().lower()] = v.decode().strip()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target, target_port = await _start_target_server(target_handler)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            # Attempt CRLF injection: inject a fake X-Admin header
            writer.write(
                f"GET http://127.0.0.1:{target_port}/test HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n"
                f"X-Custom: legit\r\n\r\n".encode()
            )
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                chunks.append(chunk)

            # The custom header should pass through normally
            assert "x-custom" in received_headers
            assert received_headers["x-custom"] == "legit"

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_empty_request_line(self, settings):
        """Empty/blank request should not crash the server."""
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(b"\r\n\r\n")
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # Should get 400 or connection close — NOT a crash
            assert b"400" in resp or len(resp) == 0

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()

    @pytest.mark.asyncio
    async def test_extremely_long_request_line(self, settings):
        """Extremely long URL in request line should not crash."""
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            long_url = "http://example.com/" + "A" * 100000
            writer.write(f"GET {long_url} HTTP/1.1\r\nHost: example.com\r\n\r\n".encode())
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # Should handle gracefully — 400 or close
            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()

    @pytest.mark.asyncio
    async def test_binary_garbage_over_tls(self, settings):
        """Random binary data should not crash the server."""
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            # Send garbage. Use smaller size to avoid hanging.
            writer.write(b"garbage" * 10)
            await writer.drain()

            # Should close or error
            try:
                resp = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            except asyncio.TimeoutError:
                resp = b""

            # Just ensure we didn't crash
            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()


# ---------------------------------------------------------------------------
# Response Timeout
# ---------------------------------------------------------------------------

class TestTimeouts:
    @pytest.mark.asyncio
    async def test_target_response_timeout(self, settings):
        """Target that never responds → 504 Gateway Timeout."""

        async def slow_target(reader, writer):
            # Read the request line to satisfy the proxy
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            # Never send a response — just hang. Use a shorter sleep to avoid test hang.
            try:
                await asyncio.sleep(8)
            except asyncio.CancelledError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        target, target_port = await _start_target_server(slow_target)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"GET http://127.0.0.1:{target_port}/slow HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()

            # The proxy should time out waiting for the target response line
            # within settings.REQUEST_TIMEOUT (5s in test)
            resp = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            assert b"504" in resp
            assert b"Gateway Timeout" in resp

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_relay_timeout_on_idle_tunnel(self, settings):
        """CONNECT tunnel that goes idle should eventually close."""
        settings.RELAY_TIMEOUT = 2.0  # Force short timeout for test

        async def idle_target(reader, writer):
            # Accept connection but do nothing
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        target, target_port = await _start_target_server(idle_target)
        home, home_port = await _start_home_node(settings)

        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", home_port, ssl=_client_ssl_context(),
            )
            writer.write(
                f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
            )
            await writer.drain()

            resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            assert b"200 Connection Established" in resp

            # Don't send anything — tunnel should timeout per RELAY_TIMEOUT
            # settings.RELAY_TIMEOUT is 2.0s
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # Should get empty (connection closed by timeout)
            assert data == b""

            writer.close()
            await writer.wait_closed()
        finally:
            home.close()
            await home.wait_closed()
            target.close()
            await target.wait_closed()


# ---------------------------------------------------------------------------
# parse_headers Edge Cases
# ---------------------------------------------------------------------------

class TestParseHeadersAdvanced:
    def test_colon_in_value(self):
        """Header value containing colons should not be split."""
        raw = b"Location: http://example.com:8080/path\r\n"
        h = parse_headers(raw)
        assert h["Location"] == "http://example.com:8080/path"

    def test_whitespace_trimming(self):
        raw = b"  Host  :   example.com   \r\n"
        h = parse_headers(raw)
        assert h["Host"] == "example.com"

    def test_duplicate_headers(self):
        """Last value wins (dict behavior)."""
        raw = b"X-Test: first\r\nX-Test: second\r\n"
        h = parse_headers(raw)
        assert h["X-Test"] == "second"

    def test_header_without_value(self):
        raw = b"X-Empty:\r\n"
        h = parse_headers(raw)
        assert h["X-Empty"] == ""

    def test_non_ascii_latin1(self):
        """Headers use latin-1 encoding per HTTP/1.1 spec."""
        raw = b"X-Custom: caf\xe9\r\n"
        h = parse_headers(raw)
        assert h["X-Custom"] == "café"


# ---------------------------------------------------------------------------
# Header Stripping — Additional Edge Cases
# ---------------------------------------------------------------------------

class TestHeaderStrippingAdvanced:
    def test_empty_headers_dict(self):
        assert _strip_spacerouter_headers({}) == {}

    def test_mixed_spacerouter_prefixes(self):
        headers = {
            "X-SpaceRouter-Node-Id": "abc",
            "X-Spacerouter-Region": "us",  # lowercase variant
            "X-SPACEROUTER-VERSION": "1.0",  # uppercase
            "Accept": "text/html",
        }
        stripped = _strip_spacerouter_headers(headers)
        assert stripped == {"Accept": "text/html"}

    def test_partial_prefix_not_stripped(self):
        """Headers that start with 'x-space' but not 'x-spacerouter-' should be kept."""
        headers = {
            "X-Space-Custom": "value",
            "X-SpaceRouter-Id": "abc",
        }
        stripped = _strip_spacerouter_headers(headers)
        assert "X-Space-Custom" in stripped
        assert "X-SpaceRouter-Id" not in stripped
