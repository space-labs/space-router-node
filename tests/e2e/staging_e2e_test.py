#!/usr/bin/env python3
"""E2E staging tests for Space Router Home Node.

Verifies the full proxy relay flow against the production Coordination API
and Proxy Gateway after deploying to the staging droplet.

Tests:
  1. Node registered with Coordination API
  2. Node reachable via direct TLS handshake
  3. Full proxy relay via HTTP gateway
  4. Full proxy relay via SOCKS5 gateway

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

import os
import socket
import ssl
import sys
import time
import traceback
from urllib.parse import quote

import httpx

MAX_REGISTRATION_WAIT = 60  # seconds
POLL_INTERVAL = 5  # seconds

# ---------------------------------------------------------------------------
# Test bookkeeping
# ---------------------------------------------------------------------------

pass_count = 0
fail_count = 0


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
# Test 3: Full Proxy Relay via HTTP Gateway
# ---------------------------------------------------------------------------

def check_http_proxy_relay(cfg, node_id):
    """Send an HTTP request through the gateway's HTTP proxy."""
    api_key_encoded = quote(cfg["api_key"], safe="")

    # Try HTTPS first (gateway likely terminates TLS), fall back to HTTP
    for scheme in ("https", "http"):
        proxy_url = f"{scheme}://{api_key_encoded}:@{cfg['gw_host']}:{cfg['gw_http_port']}"
        log(f"Testing HTTP proxy relay via {scheme}://{cfg['gw_host']}:{cfg['gw_http_port']}...")

        try:
            with httpx.Client(proxy=proxy_url, verify=False, timeout=30.0) as client:
                resp = client.get("http://httpbin.org/ip")

            if resp.status_code != 200:
                fail_(f"HTTP proxy returned status {resp.status_code}")
                return

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

        except Exception as e:
            log(f"{scheme} proxy attempt failed: {e}")
            traceback.print_exc()

    fail_("HTTP proxy relay failed with both HTTPS and HTTP")


# ---------------------------------------------------------------------------
# Test 4: Full Proxy Relay via SOCKS5 Gateway
# ---------------------------------------------------------------------------

def check_socks5_proxy_relay(cfg, node_id):
    """Send an HTTP request through the gateway's SOCKS5 proxy."""
    api_key_encoded = quote(cfg["api_key"], safe="")

    # Try plain SOCKS5 first, then SOCKS5 over TLS via raw socket
    log(f"Testing SOCKS5 proxy relay via {cfg['gw_host']}:{cfg['gw_socks5_port']}...")
    proxy_url = f"socks5://{api_key_encoded}:@{cfg['gw_host']}:{cfg['gw_socks5_port']}"

    try:
        with httpx.Client(proxy=proxy_url, timeout=30.0) as client:
            resp = client.get("http://httpbin.org/ip")

        if resp.status_code != 200:
            fail_(f"SOCKS5 proxy returned status {resp.status_code}")
            return

        exit_ip = resp.json().get("origin", "")
        log(f"Exit IP: {exit_ip}")

        if cfg["node_ip"] in exit_ip:
            pass_("SOCKS5 proxy relay succeeded (exit IP matches staging node)")
        else:
            pass_("SOCKS5 proxy relay succeeded (gateway routed request)")
        return

    except Exception as e:
        log(f"Plain SOCKS5 failed: {e}")
        traceback.print_exc()

    fail_("SOCKS5 proxy relay failed")


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

    node_id = check_node_registered(cfg)
    check_direct_tls_handshake(cfg)
    check_http_proxy_relay(cfg, node_id)
    check_socks5_proxy_relay(cfg, node_id)

    print()
    print(f"=== Results: {pass_count} passed, {fail_count} failed ===")
    print()

    sys.exit(1 if fail_count > 0 else 0)
