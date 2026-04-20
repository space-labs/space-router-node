"""Core proxy logic for the Provider.

The Provider is the server-side counterpart to the Gateway's
_connect_to_node -> handle_connect / handle_http_forward flow.  It receives
proxied traffic from the Gateway and forwards it to target servers
from its residential IP.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

from app.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

MAX_HEADER_SIZE = 64 * 1024          # 64 KB cap on total request headers
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB cap on request/response bodies
MAX_CHUNK_SIZE = 10 * 1024 * 1024     # 10 MB cap per chunked transfer chunk

# Private/reserved IP ranges — deny SSRF to internal networks
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("240.0.0.0/4"),      # reserved
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Ports that should never be proxied (internal services)
_BLOCKED_PORTS = {22, 23, 25, 135, 136, 137, 138, 139, 445, 3306, 5432, 6379, 11211, 27017}


def _is_private_ip(host: str) -> bool:
    """Return True if *host* (an IP literal) is in a private/reserved range."""
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        return False


def _is_private_target(host: str, port: int) -> bool:
    """Return True if the target host resolves to a private/reserved IP or a blocked port."""
    if port in _BLOCKED_PORTS:
        return True
    if _is_private_ip(host):
        return True
    # hostname, not IP — block obvious patterns
    try:
        ipaddress.ip_address(host)
    except ValueError:
        lower = host.lower()
        if lower in ("localhost", "localhost.localdomain") or lower.endswith(".local"):
            return True
    return False


import socket


class _DNSRebindingError(Exception):
    """Raised when DNS resolution yields a private/reserved IP."""


async def _resolve_and_connect(
    host: str,
    port: int,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Resolve *host* via DNS, check the resolved IP against the blocklist,
    then connect to the resolved IP directly (preventing DNS rebinding).

    Raises ``ConnectionRefusedError`` if the resolved IP is private/reserved.
    Raises ``OSError`` / ``asyncio.TimeoutError`` on network failure.
    """
    loop = asyncio.get_running_loop()

    # Resolve hostname → IP(s)
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=min(timeout, 10.0),
        )
    except (socket.gaierror, asyncio.TimeoutError) as exc:
        raise OSError(f"DNS resolution failed for {host}: {exc}") from exc

    if not infos:
        raise OSError(f"DNS resolution returned no results for {host}")

    # Check ALL resolved IPs — an attacker might have multiple A records
    # mixing public and private IPs.
    for family, _type, _proto, _canonname, sockaddr in infos:
        resolved_ip = sockaddr[0]
        if _is_private_ip(resolved_ip):
            raise _DNSRebindingError(
                f"DNS rebinding blocked: {host} resolved to private IP {resolved_ip}"
            )

    # Connect to the first resolved address
    # Using the resolved IP directly prevents TOCTOU race.
    family, _type, _proto, _canonname, sockaddr = infos[0]
    resolved_ip = sockaddr[0]

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(resolved_ip, port),
        timeout=timeout,
    )
    return reader, writer


# ---------------------------------------------------------------------------
# Shared protocol utilities (mirrored from proxy-gateway/app/proxy.py)
# ---------------------------------------------------------------------------

CHALLENGE_DOMAIN = "challenge.spacerouter.internal"

SPACEROUTER_HEADER_PREFIX = "x-spacerouter-"


def parse_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.split(b"\r\n"):
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
    return headers


async def _read_request_head(
    reader: asyncio.StreamReader,
    timeout: float = 30.0,
) -> tuple[bytes, str, str, str, dict[str, str]] | None:
    """Read and parse the HTTP request line + headers from *reader*.

    Returns (raw_head, method, target, version, headers) or None on error.
    """
    try:
        request_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionResetError):
        return None

    parts = request_line.decode("latin-1").strip().split(" ", 2)
    if len(parts) != 3:
        return None

    method, target, version = parts

    header_data = b""
    try:
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=10.0)
            if line == b"\r\n":
                break
            header_data += line
            if len(header_data) > MAX_HEADER_SIZE:
                return None  # Header too large — reject
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionResetError):
        return None

    headers = parse_headers(header_data)
    raw_head = request_line + header_data + b"\r\n"
    return raw_head, method, target, version, headers


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    counter: list[int],
    buffer_size: int,
) -> None:
    try:
        while True:
            data = await reader.read(buffer_size)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            counter[0] += len(data)
    except (ConnectionResetError, BrokenPipeError, OSError, asyncio.CancelledError):
        pass


# Maximum absolute wall-clock timeout for a single relay (safety cap).
MAX_RELAY_DURATION = 3600.0  # 1 hour


async def relay_streams(
    reader_a: asyncio.StreamReader,
    writer_a: asyncio.StreamWriter,
    reader_b: asyncio.StreamReader,
    writer_b: asyncio.StreamWriter,
    buffer_size: int,
    timeout: float = 300.0,
) -> tuple[int, int]:
    """Bidirectional byte relay with idle timeout.

    Waits for EITHER pipe to finish (EOF from one side), then cancels
    the other.  A hard safety cap (``MAX_RELAY_DURATION``) ensures no
    relay lives forever even if there is continuous low-rate traffic.
    """
    bytes_a_to_b = [0]
    bytes_b_to_a = [0]

    task_a = asyncio.create_task(
        _pipe(reader_a, writer_b, bytes_a_to_b, buffer_size)
    )
    task_b = asyncio.create_task(
        _pipe(reader_b, writer_a, bytes_b_to_a, buffer_size)
    )

    try:
        # Wait for EITHER pipe to finish (EOF from one side), then
        # cancel the other.  Previously used asyncio.gather which
        # waited for BOTH — causing 300s hangs when one side closed
        # the connection first (e.g. health probes, short requests).
        done, pending = await asyncio.wait_for(
            asyncio.wait({task_a, task_b}, return_when=asyncio.FIRST_COMPLETED),
            timeout=min(timeout, MAX_RELAY_DURATION),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.TimeoutError:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    return bytes_a_to_b[0], bytes_b_to_a[0]


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

def _error_response(
    status: int,
    reason: str,
    body: str,
    request_id: str | None = None,
) -> bytes:
    payload = body.encode()
    extra_headers = ""
    if request_id:
        extra_headers = f"X-SpaceRouter-Request-Id: {request_id}\r\n"
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + payload


def _bad_request(detail: str = "Bad Request", request_id: str | None = None) -> bytes:
    return _error_response(400, "Bad Request", detail, request_id)


def _forbidden(detail: str = "Forbidden", request_id: str | None = None) -> bytes:
    return _error_response(403, "Forbidden", detail, request_id)


def _bad_gateway(detail: str = "Bad Gateway", request_id: str | None = None) -> bytes:
    return _error_response(502, "Bad Gateway", detail, request_id)


def _gateway_timeout(detail: str = "Gateway Timeout", request_id: str | None = None) -> bytes:
    return _error_response(504, "Gateway Timeout", detail, request_id)


# ---------------------------------------------------------------------------
# Strip internal headers before forwarding to the target
# ---------------------------------------------------------------------------

def _strip_spacerouter_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove X-SpaceRouter-*, Proxy-Authorization, and other sensitive headers."""
    # Standard headers to remove for anonymity and security
    stripped_headers = {
        "x-forwarded-for",
        "x-real-ip",
        "via",
        "forwarded",
        "proxy-authorization",
        "proxy-connection",
    }
    return {
        k: v
        for k, v in headers.items()
        if not k.lower().startswith(SPACEROUTER_HEADER_PREFIX)
        and k.lower() not in stripped_headers
    }


# ---------------------------------------------------------------------------
# CONNECT handler — tunnel to target
# ---------------------------------------------------------------------------

async def _handle_challenge_probe(
    client_writer: asyncio.StreamWriter,
    settings: Settings,
    request_id: str | None = None,
) -> None:
    """Respond to the Coordination API challenge probe with the node's
    wallet address in the ``X-SpaceRouter-Address`` header.
    """
    extra = ""
    if request_id:
        extra = f"X-SpaceRouter-Request-Id: {request_id}\r\n"

    response = (
        f"HTTP/1.1 200 Connection Established\r\n"
        f"X-SpaceRouter-Address: {settings.STAKING_ADDRESS}\r\n"
        f"{extra}"
        f"Content-Length: 0\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()
    client_writer.write(response)
    await client_writer.drain()

    rid_tag = f" [request_id={request_id}]" if request_id else ""
    logger.info("Challenge probe answered with wallet address%s", rid_tag)


async def handle_connect(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    settings: Settings,
    request_id: str | None = None,
    gateway_address: str = "",
) -> None:
    """Open a TCP connection to *target_host:target_port*, reply 200, then
    relay bytes bidirectionally between the client (Proxy Gateway) and the
    target server.
    """
    rid_tag = f" [request_id={request_id}]" if request_id else ""

    # Challenge probe interception — sign the node's IP as proof of ownership
    if target_host == CHALLENGE_DOMAIN:
        await _handle_challenge_probe(client_writer, settings, request_id)
        return

    # SSRF protection — block private/reserved targets (static check)
    if _is_private_target(target_host, target_port):
        logger.warning("CONNECT blocked — private target %s:%s%s", target_host, target_port, rid_tag)
        client_writer.write(_forbidden("Target not allowed", request_id))
        await client_writer.drain()
        return

    # DNS-resolved SSRF check — prevent DNS rebinding attacks
    try:
        target_reader, target_writer = await _resolve_and_connect(
            target_host, target_port, settings.REQUEST_TIMEOUT,
        )
    except _DNSRebindingError as exc:
        logger.warning("CONNECT blocked (DNS rebinding) — %s%s", exc, rid_tag)
        client_writer.write(_forbidden("Target not allowed", request_id))
        await client_writer.drain()
        return
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("CONNECT failed to %s:%s — %s%s", target_host, target_port, exc, rid_tag)
        client_writer.write(_bad_gateway("Cannot connect to target", request_id))
        await client_writer.drain()
        return

    # Tell the Proxy Gateway the tunnel is established
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_writer.drain()

    try:
        bytes_sent, bytes_received = await relay_streams(
            client_reader,
            client_writer,
            target_reader,
            target_writer,
            settings.BUFFER_SIZE,
            settings.RELAY_TIMEOUT,
        )

        # Leg 2 receipt exchange: send receipt to gateway, get signature
        if settings.PAYMENT_ENABLED and settings.NODE_RATE_PER_GB > 0:
            await _do_receipt_exchange(
                client_reader, client_writer, settings,
                bytes_sent + bytes_received, request_id,
                gateway_address=gateway_address,
            )
    finally:
        try:
            target_writer.close()
            await target_writer.wait_closed()
        except Exception:
            pass


async def _do_receipt_exchange(
    gateway_reader: asyncio.StreamReader,
    gateway_writer: asyncio.StreamWriter,
    settings: Settings,
    data_amount: int,
    request_id: str | None,
    gateway_address: str = "",
) -> None:
    """Build a receipt, send to Gateway, store the signed response.

    The Gateway's EVM address is received via the
    ``X-SpaceRouter-Gateway-Address`` header in the CONNECT request.
    """
    from app.payment.receipt_exchange import build_receipt, exchange_receipt_with_gateway
    from app.payment.eip712 import address_to_bytes32

    if not gateway_address:
        logger.debug("No gateway address — skipping receipt exchange")
        return

    node_identity = settings.NODE_IDENTITY_ADDRESS or settings.COLLECTION_ADDRESS or settings.STAKING_ADDRESS
    if not node_identity:
        logger.debug("No provider identity address — skipping receipt exchange")
        return

    node_b32 = address_to_bytes32(node_identity)
    receipt = build_receipt(
        gateway_address=gateway_address,
        node_address_bytes32=node_b32,
        data_amount=data_amount,
        rate_per_gb=settings.NODE_RATE_PER_GB,
    )

    result = await exchange_receipt_with_gateway(
        gateway_reader, gateway_writer, receipt,
    )

    if not result:
        return

    signature, signed_receipt = result
    try:
        from app.payment.receipt_store import get_store
        store = get_store(settings.RECEIPT_STORE_PATH)
        await store.initialize()
        await store.store(signed_receipt, signature)
    except Exception as e:
        logger.warning(
            "Leg 2 receipt received but failed to persist locally "
            "(uuid=%s): %s — signature logged below, recover manually if needed: %s",
            signed_receipt.request_uuid, e, signature,
        )
        return

    logger.info(
        "Leg 2 receipt signed and stored: uuid=%s amount=%d price=%d",
        signed_receipt.request_uuid,
        signed_receipt.data_amount,
        signed_receipt.total_price,
    )


# ---------------------------------------------------------------------------
# HTTP forward handler — plain-text HTTP proxy
# ---------------------------------------------------------------------------

async def handle_http_forward(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    method: str,
    target: str,
    version: str,
    headers: dict[str, str],
    settings: Settings,
    request_id: str | None = None,
) -> None:
    """Forward an HTTP request to the target server and stream the response
    back to the client (Proxy Gateway).

    The *target* is an absolute URI (``http://example.com/path``).  We parse
    it, connect to the origin, rewrite the request line to a relative path,
    and relay request + response.
    """
    rid_tag = f" [request_id={request_id}]" if request_id else ""

    parsed = urlparse(target)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    if not host:
        client_writer.write(_bad_request("Missing host in target URL", request_id))
        await client_writer.drain()
        return

    # SSRF protection — block private/reserved targets (static check)
    if _is_private_target(host, port):
        logger.warning("HTTP forward blocked — private target %s:%s%s", host, port, rid_tag)
        client_writer.write(_forbidden("Target not allowed", request_id))
        await client_writer.drain()
        return

    # DNS-resolved SSRF check — prevent DNS rebinding attacks
    try:
        target_reader, target_writer = await _resolve_and_connect(
            host, port, settings.REQUEST_TIMEOUT,
        )
    except _DNSRebindingError as exc:
        logger.warning("HTTP forward blocked (DNS rebinding) — %s%s", exc, rid_tag)
        client_writer.write(_forbidden("Target not allowed", request_id))
        await client_writer.drain()
        return
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("HTTP forward failed to %s:%s — %s%s", host, port, exc, rid_tag)
        client_writer.write(_bad_gateway("Cannot connect to target", request_id))
        await client_writer.drain()
        return

    try:
        # Build the forwarded request — relative path, strip internal headers
        forward_headers = _strip_spacerouter_headers(headers)
        # Ensure Host header is correct
        forward_headers["Host"] = f"{host}:{port}" if port not in (80, 443) else host

        header_str = "".join(f"{k}: {v}\r\n" for k, v in forward_headers.items())
        request_head = f"{method} {path} {version}\r\n{header_str}\r\n".encode()
        target_writer.write(request_head)
        await target_writer.drain()

        # Forward request body if present (with size cap)
        content_length = int(headers.get("Content-Length", headers.get("content-length", "0")))
        if content_length > MAX_CONTENT_LENGTH:
            client_writer.write(_bad_request("Request body too large", request_id))
            await client_writer.drain()
            return
        if content_length > 0:
            remaining = content_length
            while remaining > 0:
                chunk = await client_reader.read(min(remaining, settings.BUFFER_SIZE))
                if not chunk:
                    break
                target_writer.write(chunk)
                await target_writer.drain()
                remaining -= len(chunk)

        # Read response from target
        try:
            response_line = await asyncio.wait_for(
                target_reader.readuntil(b"\r\n"),
                timeout=settings.REQUEST_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            client_writer.write(_gateway_timeout("Target server timed out", request_id))
            await client_writer.drain()
            return

        # Read response headers
        resp_header_data = b""
        while True:
            try:
                line = await asyncio.wait_for(target_reader.readuntil(b"\r\n"), timeout=10.0)
                if line == b"\r\n":
                    break
                resp_header_data += line
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break

        resp_headers = parse_headers(resp_header_data)

        # Forward response status + headers to the Proxy Gateway
        client_writer.write(response_line)
        client_writer.write(resp_header_data)
        client_writer.write(b"\r\n")
        await client_writer.drain()

        # Stream response body
        resp_content_length = resp_headers.get(
            "Content-Length", resp_headers.get("content-length")
        )
        transfer_encoding = resp_headers.get(
            "Transfer-Encoding", resp_headers.get("transfer-encoding", "")
        )

        if resp_content_length:
            remaining = int(resp_content_length)
            while remaining > 0:
                chunk = await target_reader.read(min(remaining, settings.BUFFER_SIZE))
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()
                remaining -= len(chunk)
        elif "chunked" in transfer_encoding.lower():
            while True:
                try:
                    size_line = await asyncio.wait_for(
                        target_reader.readuntil(b"\r\n"),
                        timeout=settings.REQUEST_TIMEOUT,
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                client_writer.write(size_line)
                await client_writer.drain()

                chunk_size = int(size_line.strip(), 16)
                if chunk_size > MAX_CHUNK_SIZE:
                    break  # Reject oversized chunks
                if chunk_size == 0:
                    trailer = await target_reader.readuntil(b"\r\n")
                    client_writer.write(trailer)
                    await client_writer.drain()
                    break

                chunk_data = await target_reader.readexactly(chunk_size + 2)
                client_writer.write(chunk_data)
                await client_writer.drain()
        else:
            # No content-length or chunked: read until connection close
            while True:
                chunk = await target_reader.read(settings.BUFFER_SIZE)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()

    finally:
        try:
            target_writer.close()
            await target_writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Client dispatch
# ---------------------------------------------------------------------------

# Global connection semaphore — initialized lazily on first use.
# The limit comes from Settings.MAX_CONNECTIONS.
_connection_semaphore: asyncio.Semaphore | None = None
_active_connections: int = 0


def _get_semaphore(max_connections: int) -> asyncio.Semaphore:
    """Return (or create) the global connection semaphore."""
    global _connection_semaphore
    if _connection_semaphore is None:
        _connection_semaphore = asyncio.Semaphore(max_connections)
    return _connection_semaphore


def _service_unavailable() -> bytes:
    body = b"503 Service Unavailable - connection limit reached"
    return (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def _is_probe_target(target_host: str, settings: Settings) -> bool:
    """Return True if the CONNECT target is a coordination API probe."""
    if target_host == CHALLENGE_DOMAIN:
        return True
    # Liveness probes CONNECT to the coordination API's own hostname.
    try:
        api_host = urlparse(settings.COORDINATION_API_URL).hostname
        if api_host and target_host == api_host:
            return True
    except Exception:
        pass
    return False


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    settings: Settings,
) -> None:
    """Entry point for each inbound connection from the Proxy Gateway."""
    global _active_connections
    peer = writer.get_extra_info("peername")

    sem = _get_semaphore(settings.MAX_CONNECTIONS)
    if not sem._value:  # noqa: SLF001 — fast check without awaiting
        logger.warning(
            "Connection limit reached (%d) — rejecting %s",
            settings.MAX_CONNECTIONS, peer,
        )
        try:
            writer.write(_service_unavailable())
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        return

    async with sem:
        _active_connections += 1
        from app.node_logging import activity as _activity  # noqa: E402
        counted = False
        try:
            result = await _read_request_head(reader, settings.REQUEST_TIMEOUT)
            if result is None:
                writer.write(_bad_request("Malformed request"))
                await writer.drain()
                return

            _raw_head, method, target, version, headers = result

            # Extract X-SpaceRouter-Request-Id for log correlation
            request_id: str | None = (
                headers.get("X-SpaceRouter-Request-Id")
                or headers.get("x-spacerouter-request-id")
                or None
            )

            if method.upper() == "CONNECT":
                host_port = target.split(":")
                target_host = host_port[0]
                target_port = int(host_port[1]) if len(host_port) > 1 else 443

                is_probe = _is_probe_target(target_host, settings)

                if not is_probe:
                    counted = True
                    total = _activity.record_connection()
                    logger.info(
                        "Connection #%d CONNECT %s:%s (active=%d)%s",
                        total, target_host, target_port, _active_connections,
                        f" [request_id={request_id}]" if request_id else "",
                    )
                else:
                    logger.info(
                        "Probe CONNECT %s:%s%s",
                        target_host, target_port,
                        f" [request_id={request_id}]" if request_id else "",
                    )

                # Extract gateway payment address for Leg 2 receipt exchange
                gw_addr = (
                    headers.get("X-SpaceRouter-Gateway-Address")
                    or headers.get("x-spacerouter-gateway-address")
                    or ""
                )
                await handle_connect(
                    reader, writer, target_host, target_port, settings,
                    request_id, gateway_address=gw_addr,
                )
            else:
                counted = True
                total = _activity.record_connection()
                logger.info(
                    "Connection #%d %s %s (active=%d)%s",
                    total, method, target, _active_connections,
                    f" [request_id={request_id}]" if request_id else "",
                )
                await handle_http_forward(reader, writer, method, target, version, headers, settings, request_id)

        except Exception:
            logger.exception("Unhandled error in client handler")
        finally:
            _active_connections -= 1
            if counted:
                _activity.record_connection_closed()
            logger.debug("Connection closed from %s (active=%d)", peer, _active_connections)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
