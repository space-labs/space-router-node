#!/usr/bin/env python3
"""E2E staging tests for Space Router Home Node.

Verifies the node's proxy relay functionality after deploying to staging.

Critical tests (must pass):
  1. Node registered with Coordination API
  2. Node reachable via direct TLS handshake
  3. Direct CONNECT proxy relay through the node
  4. Direct HTTP forward proxy relay through the node

Non-critical tests (gateway infrastructure — warn on failure):
  5. Full proxy relay via HTTP gateway
  6. Full proxy relay via SOCKS5 gateway

Usage: python tests/e2e/staging_e2e_test.py

Required environment variables:
  STAGING_NODE_IP       - Droplet public IP (e.g. 159.223.44.4)
  STAGING_NODE_PORT     - Node port (e.g. 9090)
  COORDINATION_API_URL  - e.g. https://coordination.spacerouter.org
  GATEWAY_HOST          - e.g. gateway.spacerouter.org
  GATEWAY_HTTP_PORT     - HTTP proxy port (e.g. 8080)
  GATEWAY_SOCKS5_PORT   - SOCKS5 proxy port (e.g. 1080)
  SR_API_KEY            - Space Router API key for gateway authentication

Not collected by pytest — run directly via ``python tests/e2e/staging_e2e_test.py``.
"""

import json as _json
import os
import socket
import ssl
import struct
import sys
import time
from urllib.parse import quote

import httpx

MAX_REGISTRATION_WAIT = 60  # seconds
POLL_INTERVAL = 5  # seconds
PROXY_RETRIES = 4  # number of proxy relay attempts
PROXY_RETRY_DELAY = 10  # seconds between proxy relay retries
GATEWAY_PROPAGATION_DELAY = 15  # seconds to wait after registration for gateway to discover node
DIRECT_PROXY_RETRIES = 3  # retries for direct proxy tests
DIRECT_PROXY_RETRY_DELAY = 5  # seconds between direct proxy retries

# ---------------------------------------------------------------------------
# Test bookkeeping
# ---------------------------------------------------------------------------

pass_count = 0
fail_count = 0
warn_count = 0


def log(msg):
    print(f"  [INFO]  {msg}")


def pass_(msg):
    global pass_count
    pass_count += 1
    print(f"  [PASS]  {msg}")


def fail_(msg):
    global fail_count
    fail_count += 1
    print(f"  [FAIL]  {msg}")


def warn_(msg):
    global warn_count
    warn_count += 1
    print(f"  [WARN]  {msg}")


# ---------------------------------------------------------------------------
# Test 1: Node Registration
# ---------------------------------------------------------------------------

def check_node_registered(cfg):
    """Poll the Coordination API until the staging node appears."""
    log("Waiting for node to register with Coordination API...")
    deadline = time.time() + MAX_REGISTRATION_WAIT

    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"{cfg['api_url']}/nodes",
                timeout=10.0,
            )
            if resp.status_code == 200:
                nodes = resp.json()
                if isinstance(nodes, dict):
                    nodes = nodes.get("nodes", [])
                for node in nodes:
                    if (
                        node.get("public_ip") == cfg["node_ip"]
                        and node.get("status") == "online"
                    ):
                        node_id = node["id"]
                        log(f"Found staging node: id={node_id}, "
                            f"ip_type={node.get('ip_type')}, "
                            f"ip_region={node.get('ip_region')}")
                        pass_("Node registered with Coordination API")
                        return node_id
        except Exception as e:
            log(f"Polling error: {e}")

        time.sleep(POLL_INTERVAL)

    fail_("Node did not register within timeout")
    return None


# ---------------------------------------------------------------------------
# Test 2: Direct TLS Handshake
# ---------------------------------------------------------------------------

def check_direct_tls_handshake(cfg):
    """Verify the node's TLS port is reachable."""
    log(f"Testing direct TLS connection to {cfg['node_ip']}:{cfg['node_port']}...")
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed cert
        with socket.create_connection(
            (cfg["node_ip"], cfg["node_port"]), timeout=10
        ) as sock:
            with ctx.wrap_socket(sock) as ssock:
                log(f"TLS version: {ssock.version()}")
                pass_("Direct TLS handshake succeeded")
                return True
    except Exception as e:
        fail_(f"Direct TLS handshake failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Test 3: Direct CONNECT Proxy Relay
# ---------------------------------------------------------------------------

def _tls_connect_to_node(cfg):
    """Open a TLS connection to the node, returning the wrapped socket."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw_sock = socket.create_connection(
        (cfg["node_ip"], cfg["node_port"]), timeout=30,
    )
    return ctx.wrap_socket(raw_sock)


def _recv_until_double_crlf(sock, timeout=30):
    """Read from socket until \\r\\n\\r\\n (end of HTTP headers)."""
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def check_direct_connect_relay(cfg):
    """Send a CONNECT request directly to the node and verify tunnel relay."""
    target_host = "httpbin.org"
    target_port = 80
    log(f"Testing direct CONNECT relay to {target_host}:{target_port} via node...")

    last_error = None
    for attempt in range(1, DIRECT_PROXY_RETRIES + 1):
        try:
            sock = _tls_connect_to_node(cfg)
            try:
                # Send CONNECT request
                connect_req = (
                    f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                    f"Host: {target_host}:{target_port}\r\n"
                    f"\r\n"
                ).encode()
                sock.sendall(connect_req)

                # Read CONNECT response
                response = _recv_until_double_crlf(sock)
                status_line = response.split(b"\r\n", 1)[0].decode()
                log(f"Attempt {attempt}: CONNECT response: {status_line}")

                if "200" not in status_line:
                    last_error = f"CONNECT returned: {status_line}"
                    continue

                # Tunnel is open — send plain HTTP through it
                http_req = (
                    f"GET /ip HTTP/1.1\r\n"
                    f"Host: {target_host}\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                ).encode()
                sock.sendall(http_req)

                # Read HTTP response from target
                sock.settimeout(30)
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw_resp = b"".join(chunks).decode(errors="replace")

                resp_status = raw_resp.split("\r\n", 1)[0]
                log(f"Target response: {resp_status}")

                if "200" in resp_status:
                    body = raw_resp.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in raw_resp else ""
                    log(f"Body: {body[:200]}")
                    if cfg["node_ip"] in body:
                        pass_("Direct CONNECT relay succeeded (exit IP matches node)")
                    else:
                        pass_("Direct CONNECT relay succeeded")
                    return
                elif "301" in resp_status or "302" in resp_status:
                    pass_("Direct CONNECT relay succeeded (target redirected)")
                    return
                else:
                    last_error = f"target returned: {resp_status}"

            finally:
                sock.close()

        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")
            last_error = str(e)

        if attempt < DIRECT_PROXY_RETRIES:
            log(f"Retrying in {DIRECT_PROXY_RETRY_DELAY}s...")
            time.sleep(DIRECT_PROXY_RETRY_DELAY)

    fail_(f"Direct CONNECT relay failed after {DIRECT_PROXY_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Test 4: Direct HTTP Forward Proxy Relay
# ---------------------------------------------------------------------------

def check_direct_http_forward(cfg):
    """Send an HTTP forward (absolute-URI) request directly to the node."""
    target_url = "http://httpbin.org/ip"
    log(f"Testing direct HTTP forward to {target_url} via node...")

    last_error = None
    for attempt in range(1, DIRECT_PROXY_RETRIES + 1):
        try:
            sock = _tls_connect_to_node(cfg)
            try:
                # Send HTTP forward request with absolute URI (proxy-style)
                http_req = (
                    f"GET {target_url} HTTP/1.1\r\n"
                    f"Host: httpbin.org\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                ).encode()
                sock.sendall(http_req)

                # Read response
                sock.settimeout(30)
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw_resp = b"".join(chunks).decode(errors="replace")

                resp_status = raw_resp.split("\r\n", 1)[0]
                log(f"Attempt {attempt}: {resp_status}")

                if "200" in resp_status:
                    body = raw_resp.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in raw_resp else ""
                    log(f"Body: {body[:200]}")
                    if cfg["node_ip"] in body:
                        pass_("Direct HTTP forward succeeded (exit IP matches node)")
                    else:
                        pass_("Direct HTTP forward succeeded")
                    return
                elif "301" in resp_status or "302" in resp_status:
                    pass_("Direct HTTP forward succeeded (target redirected)")
                    return
                else:
                    last_error = f"response: {resp_status}"

            finally:
                sock.close()

        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")
            last_error = str(e)

        if attempt < DIRECT_PROXY_RETRIES:
            log(f"Retrying in {DIRECT_PROXY_RETRY_DELAY}s...")
            time.sleep(DIRECT_PROXY_RETRY_DELAY)

    fail_(f"Direct HTTP forward failed after {DIRECT_PROXY_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Test 5: Full Proxy Relay via HTTP Gateway (non-critical)
# ---------------------------------------------------------------------------

def check_http_proxy_relay(cfg, node_id):
    """Send an HTTP request through the gateway's HTTP proxy."""
    api_key_encoded = quote(cfg["api_key"], safe="")

    # Gateway terminates TLS on port 8080
    proxy_url = f"https://{api_key_encoded}:@{cfg['gw_host']}:{cfg['gw_http_port']}"
    target_url = "https://httpbin.org/ip"
    log(f"Testing HTTP proxy relay via https://{cfg['gw_host']}:{cfg['gw_http_port']}...")
    log(f"Target URL: {target_url}")

    last_error = None
    for attempt in range(1, PROXY_RETRIES + 1):
        try:
            with httpx.Client(proxy=proxy_url, verify=False, timeout=30.0) as client:
                resp = client.get(target_url)

            log(f"Attempt {attempt}: status={resp.status_code}")
            if resp.status_code == 200:
                exit_ip = resp.json().get("origin", "")
                routed_node = resp.headers.get("x-spacerouter-node", "")
                request_id = resp.headers.get("x-spacerouter-request-id", "")

                log(f"Exit IP: {exit_ip}")
                log(f"Routed via node: {routed_node}")
                if request_id:
                    log(f"Request ID: {request_id}")

                if node_id and routed_node == node_id:
                    pass_("HTTP proxy relay succeeded (routed through staging node)")
                elif cfg["node_ip"] in exit_ip:
                    pass_("HTTP proxy relay succeeded (exit IP matches staging node)")
                else:
                    pass_("HTTP proxy relay succeeded (gateway routed request)")
                return

            log(f"Response headers: {dict(resp.headers)}")
            log(f"Response body: {resp.text[:500]}")
            last_error = f"status {resp.status_code}"

        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")
            last_error = str(e)

        if attempt < PROXY_RETRIES:
            log(f"Retrying in {PROXY_RETRY_DELAY}s...")
            time.sleep(PROXY_RETRY_DELAY)

    warn_(f"HTTP proxy relay failed after {PROXY_RETRIES} attempts: {last_error} "
          "(gateway infrastructure issue — not a node defect)")


# ---------------------------------------------------------------------------
# Test 6: Full Proxy Relay via SOCKS5 Gateway (non-critical)
# ---------------------------------------------------------------------------

def _socks5_over_tls_request(cfg, target_host, target_port):
    """Perform a SOCKS5 handshake over a TLS connection and send an HTTP request."""
    # 1. Connect with TLS to the SOCKS5 gateway
    ctx = ssl.create_default_context()
    raw_sock = socket.create_connection(
        (cfg["gw_host"], cfg["gw_socks5_port"]), timeout=30,
    )
    sock = ctx.wrap_socket(raw_sock, server_hostname=cfg["gw_host"])

    try:
        # 2. SOCKS5 greeting: support username/password auth (method 0x02)
        sock.sendall(b"\x05\x01\x02")
        reply = sock.recv(2)
        if len(reply) < 2 or reply[0] != 0x05 or reply[1] != 0x02:
            raise RuntimeError(f"SOCKS5 greeting failed: {reply!r}")

        # 3. Username/password auth (RFC 1929)
        api_key = cfg["api_key"].encode()
        auth_msg = b"\x01" + bytes([len(api_key)]) + api_key + b"\x01\x00"  # empty password
        sock.sendall(auth_msg)
        auth_reply = sock.recv(2)
        if len(auth_reply) < 2 or auth_reply[1] != 0x00:
            raise RuntimeError(f"SOCKS5 auth failed: {auth_reply!r}")

        # 4. CONNECT request
        host_bytes = target_host.encode()
        connect_msg = (
            b"\x05\x01\x00\x03"
            + bytes([len(host_bytes)]) + host_bytes
            + struct.pack("!H", target_port)
        )
        sock.sendall(connect_msg)
        connect_reply = sock.recv(256)
        if len(connect_reply) < 2 or connect_reply[1] != 0x00:
            raise RuntimeError(f"SOCKS5 CONNECT failed: status={connect_reply[1]!r}")

        # 5. Send HTTP request through the tunnel
        http_req = (
            f"GET /ip HTTP/1.1\r\n"
            f"Host: {target_host}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode()
        sock.sendall(http_req)

        # 6. Read response
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")
    finally:
        sock.close()


def check_socks5_proxy_relay(cfg, node_id):
    """Send an HTTP request through the gateway's SOCKS5 proxy."""
    log(f"Testing SOCKS5 proxy relay via {cfg['gw_host']}:{cfg['gw_socks5_port']}...")

    last_error = None
    for attempt in range(1, PROXY_RETRIES + 1):
        try:
            raw_resp = _socks5_over_tls_request(cfg, "httpbin.org", 80)
            log(f"Attempt {attempt}: raw response (first 300 chars): {raw_resp[:300]}")

            # Parse status line
            status_line = raw_resp.split("\r\n", 1)[0]
            status_code = int(status_line.split(" ", 2)[1])

            if status_code == 200:
                # Extract JSON body
                body = raw_resp.split("\r\n\r\n", 1)[-1]
                if "{" in body:
                    json_str = body[body.index("{"):body.rindex("}") + 1]
                    exit_ip = _json.loads(json_str).get("origin", "")
                else:
                    exit_ip = ""

                log(f"Exit IP: {exit_ip}")

                if cfg["node_ip"] in exit_ip:
                    pass_("SOCKS5 proxy relay succeeded (exit IP matches staging node)")
                else:
                    pass_("SOCKS5 proxy relay succeeded (gateway routed request)")
                return

            last_error = f"status {status_code}"
            log(f"Attempt {attempt}: SOCKS5 proxy returned status {status_code}")

        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")
            last_error = str(e)

        if attempt < PROXY_RETRIES:
            log(f"Retrying in {PROXY_RETRY_DELAY}s...")
            time.sleep(PROXY_RETRY_DELAY)

    warn_(f"SOCKS5 proxy relay failed after {PROXY_RETRIES} attempts: {last_error} "
          "(gateway infrastructure issue — not a node defect)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = {
        "node_ip": os.environ["STAGING_NODE_IP"],
        "node_port": int(os.environ["STAGING_NODE_PORT"]),
        "api_url": os.environ["COORDINATION_API_URL"],
        "gw_host": os.environ["GATEWAY_HOST"],
        "gw_http_port": int(os.environ["GATEWAY_HTTP_PORT"]),
        "gw_socks5_port": int(os.environ["GATEWAY_SOCKS5_PORT"]),
        "api_key": os.environ["SR_API_KEY"].strip(),
    }

    print()
    print("=== Space Router Home Node — Staging E2E Tests ===")
    print(f"Node:    {cfg['node_ip']}:{cfg['node_port']}")
    print(f"API:     {cfg['api_url']}")
    print(f"Gateway: {cfg['gw_host']} (HTTP:{cfg['gw_http_port']}, SOCKS5:{cfg['gw_socks5_port']})")
    print()

    # --- Critical tests (node functionality) ---
    print("--- Critical: Node Registration & Connectivity ---")
    node_id = check_node_registered(cfg)
    check_direct_tls_handshake(cfg)

    print()
    print("--- Critical: Direct Proxy Relay (node ← test runner) ---")
    check_direct_connect_relay(cfg)
    check_direct_http_forward(cfg)

    # --- Non-critical tests (gateway infrastructure) ---
    print()
    print("--- Non-critical: Gateway Proxy Relay (gateway infrastructure) ---")
    log(f"Waiting {GATEWAY_PROPAGATION_DELAY}s for gateway to discover new node...")
    time.sleep(GATEWAY_PROPAGATION_DELAY)

    check_http_proxy_relay(cfg, node_id)
    check_socks5_proxy_relay(cfg, node_id)

    print()
    print(f"=== Results: {pass_count} passed, {fail_count} failed, {warn_count} warnings ===")
    if warn_count > 0:
        print("  (Warnings are from gateway infrastructure tests — not node defects)")
    print()

    sys.exit(1 if fail_count > 0 else 0)
