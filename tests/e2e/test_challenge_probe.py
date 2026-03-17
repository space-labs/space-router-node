#!/usr/bin/env python3
"""E2E test: register with the production Coordination API and verify the
endpoint challenge probe succeeds.

The Coordination API sends ``CONNECT challenge.spacerouter.internal:443``
back to our ``endpoint_url`` during ``POST /nodes``.  A successful 201
response proves the challenge was satisfied.

Usage:
    SR_COORDINATION_API_URL=https://coordination.spacerouter.org \
    SR_NODE_PORT=9090 \
    python3 tests/e2e/test_challenge_probe.py

Environment variables (all optional — sensible defaults provided):
    SR_COORDINATION_API_URL  — Coordination API base URL
    SR_NODE_PORT             — port the proxy binds to (default: 9090)
    SR_PUBLIC_IP             — override auto-detection
    SR_UPNP_ENABLED         — "true" (default) or "false"
"""

import asyncio
import functools
import logging
import sys
import os

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import httpx

from app.config import Settings
from app.proxy_handler import handle_client
from app.registration import deregister_node, detect_public_ip, register_node
from app.tls import create_server_ssl_context, ensure_certificates
from app.wallet import validate_wallet_address

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("e2e.challenge_probe")


async def run_e2e() -> bool:
    settings = Settings()

    logger.info("Coordination API: %s", settings.COORDINATION_API_URL)
    logger.info("Node port: %d", settings.NODE_PORT)

    # 1. Generate TLS certificates and validate wallet address
    ensure_certificates(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)
    ssl_ctx = create_server_ssl_context(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)

    if not settings.WALLET_ADDRESS:
        logger.error("SR_WALLET_ADDRESS is required")
        return False
    settings.WALLET_ADDRESS = validate_wallet_address(settings.WALLET_ADDRESS)
    wallet_address = settings.WALLET_ADDRESS
    logger.info("Wallet address: %s", wallet_address)

    # 2. Start the proxy server
    handler = functools.partial(handle_client, settings=settings)
    server = await asyncio.start_server(
        handler, host=settings.BIND_ADDRESS, port=settings.NODE_PORT, ssl=ssl_ctx,
    )
    logger.info("Proxy server listening on %s:%d", settings.BIND_ADDRESS, settings.NODE_PORT)

    node_id = None
    async with httpx.AsyncClient() as http_client:
        try:
            # 3. Try UPnP port mapping if enabled
            upnp_endpoint = None
            if settings.UPNP_ENABLED:
                try:
                    from app.upnp import setup_upnp_mapping
                    upnp_endpoint = await setup_upnp_mapping(
                        settings.NODE_PORT, lease_duration=settings.UPNP_LEASE_DURATION,
                    )
                    if upnp_endpoint:
                        logger.info("UPnP mapping: %s:%d", upnp_endpoint[0], upnp_endpoint[1])
                    else:
                        logger.warning("UPnP mapping failed — using direct mode")
                except Exception as exc:
                    logger.warning("UPnP unavailable: %s", exc)

            # 4. Detect public IP
            if settings.PUBLIC_IP:
                public_ip = settings.PUBLIC_IP
                logger.info("Using configured public IP: %s", public_ip)
            else:
                public_ip = await detect_public_ip(http_client)
                settings.PUBLIC_IP = public_ip

            # 5. Register with Coordination API (this triggers the challenge probe)
            logger.info("Registering with Coordination API (challenge probe will be sent)...")
            node_id, gateway_ca_cert = await register_node(
                http_client, settings, public_ip, upnp_endpoint=upnp_endpoint,
                wallet_address=wallet_address,
            )

            logger.info("Registration SUCCEEDED — node_id=%s", node_id)
            logger.info("Challenge probe was satisfied (Coordination API accepted our registration)")
            return True

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Registration FAILED with HTTP %d: %s",
                exc.response.status_code,
                exc.response.text,
            )
            return False

        except Exception as exc:
            logger.error("Registration FAILED: %s", exc)
            return False

        finally:
            # 6. Clean up: deregister and stop server
            if node_id:
                try:
                    await deregister_node(http_client, settings, node_id)
                    logger.info("Deregistered node %s", node_id)
                except Exception as exc:
                    logger.warning("Deregistration failed: %s", exc)

            if upnp_endpoint:
                try:
                    from app.upnp import remove_upnp_mapping
                    await remove_upnp_mapping(upnp_endpoint[1])
                    logger.info("UPnP mapping removed")
                except Exception:
                    pass

            server.close()
            await server.wait_closed()
            logger.info("Proxy server stopped")


def main() -> None:
    success = asyncio.run(run_e2e())
    if success:
        print("\n[PASS] E2E challenge probe test passed")
        sys.exit(0)
    else:
        print("\n[FAIL] E2E challenge probe test failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
