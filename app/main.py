"""Home Node Daemon — entry point.

Lifecycle:
  1. If UPnP enabled, try UPnP/NAT-PMP port mapping
  2. Detect public IP (or use configured value)
  3. Load/generate wallet key + derive address
  4. Start TLS server (must be running before registration for challenge probe)
  5. Register with Coordination API (triggers challenge probe)
  6. Upgrade to mTLS if enabled + start UPnP renewal
  7. Wait for SIGTERM / SIGINT
  8. Cancel UPnP renewal + remove port mapping
  9. Deregister node (best-effort)
  10. Shutdown
"""

import asyncio
import functools
import logging
import os
import signal
import sys

import httpx

from app.config import settings
from app.proxy_handler import handle_client
from app.registration import deregister_node, detect_public_ip, register_node, save_gateway_ca_cert
from app.tls import create_mtls_server_ssl_context, create_server_ssl_context, ensure_certificates
from app.version import __version__
from app.wallet import ensure_wallet_key, private_key_to_address

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run(settings_override=None) -> None:  # noqa: ANN001
    s = settings_override or settings
    stop_event = asyncio.Event()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    else:
        # Windows: loop.add_signal_handler() is not supported.
        # Use signal.signal() and schedule the event via call_soon_threadsafe
        # since signal handlers can interrupt the event loop.
        loop = asyncio.get_running_loop()

        def _handle_signal(signum, frame):  # noqa: ANN001
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    async with httpx.AsyncClient() as http_client:
        # 1. Try UPnP/NAT-PMP port mapping (if enabled)
        upnp_endpoint = None
        if s.UPNP_ENABLED:
            from app.upnp import setup_upnp_mapping

            upnp_endpoint = await setup_upnp_mapping(
                s.NODE_PORT, lease_duration=s.UPNP_LEASE_DURATION,
            )
            if upnp_endpoint:
                logger.info(
                    "UPnP mapping active: %s:%d",
                    upnp_endpoint[0], upnp_endpoint[1],
                )
            else:
                logger.warning(
                    "UPnP enabled but mapping failed — "
                    "falling back to direct public IP mode"
                )

        # 2. Detect public IP (always needed for metadata + challenge signing)
        if s.PUBLIC_IP:
            public_ip = s.PUBLIC_IP
            logger.info("Using configured public IP: %s", public_ip)
        else:
            try:
                public_ip = await detect_public_ip(http_client)
            except RuntimeError:
                logger.error("Cannot detect public IP — aborting")
                sys.exit(1)
        # Store back so proxy handler can access it for challenge signing
        s.PUBLIC_IP = public_ip

        # 3. Load/generate wallet key + derive address
        if s.WALLET_PRIVATE_KEY:
            logger.info("Using wallet key from SR_WALLET_PRIVATE_KEY env var")
        else:
            s.WALLET_PRIVATE_KEY = ensure_wallet_key(s.WALLET_KEY_PATH)
        wallet_address = private_key_to_address(s.WALLET_PRIVATE_KEY)
        logger.info("Wallet address: %s", wallet_address)

        # 4. Start TLS server (must be running before registration so the
        #    Coordination API challenge probe can reach us)
        ensure_certificates(s.TLS_CERT_PATH, s.TLS_KEY_PATH)
        ssl_ctx = create_server_ssl_context(s.TLS_CERT_PATH, s.TLS_KEY_PATH)

        handler = functools.partial(handle_client, settings=s)
        server = await asyncio.start_server(
            handler, host=s.BIND_ADDRESS, port=s.NODE_PORT, ssl=ssl_ctx,
        )
        logger.info("Home Node listening on port %d (pre-registration)", s.NODE_PORT)

        # 5. Register with Coordination API (triggers challenge probe)
        try:
            node_id, gateway_ca_cert = await register_node(
                http_client, s, public_ip,
                upnp_endpoint=upnp_endpoint,
                wallet_address=wallet_address,
            )
        except Exception:
            logger.exception("Failed to register with Coordination API — aborting")
            server.close()
            await server.wait_closed()
            sys.exit(1)

        # 5b. Save gateway CA cert if provided
        if gateway_ca_cert:
            save_gateway_ca_cert(gateway_ca_cert, s.GATEWAY_CA_CERT_PATH)

        # 6. Upgrade to mTLS if enabled (restart server with client-cert verification)
        if s.MTLS_ENABLED:
            if not os.path.isfile(s.GATEWAY_CA_CERT_PATH):
                logger.error(
                    "mTLS enabled but gateway CA cert not found at %s — aborting",
                    s.GATEWAY_CA_CERT_PATH,
                )
                server.close()
                await server.wait_closed()
                sys.exit(1)
            logger.info("Upgrading to mTLS…")
            server.close()
            await server.wait_closed()
            ssl_ctx = create_mtls_server_ssl_context(
                s.TLS_CERT_PATH, s.TLS_KEY_PATH, s.GATEWAY_CA_CERT_PATH,
            )
            server = await asyncio.start_server(
                handler, host=s.BIND_ADDRESS, port=s.NODE_PORT, ssl=ssl_ctx,
            )

        logger.info(
            "Home Node ready (node_id=%s, wallet=%s, upnp=%s)",
            node_id, wallet_address,
            f"{upnp_endpoint[0]}:{upnp_endpoint[1]}" if upnp_endpoint else "disabled",
        )

        # Start UPnP lease renewal if applicable
        renewal_task = None
        if upnp_endpoint and s.UPNP_LEASE_DURATION > 0:
            from app.upnp import renew_upnp_mapping

            async def _renew_loop() -> None:
                interval = max(s.UPNP_LEASE_DURATION // 2, 60)
                while True:
                    await asyncio.sleep(interval)
                    ok = await renew_upnp_mapping(
                        s.NODE_PORT, upnp_endpoint[1], s.UPNP_LEASE_DURATION,
                    )
                    if ok:
                        logger.debug("UPnP lease renewed")
                    else:
                        logger.warning("UPnP lease renewal failed")

            renewal_task = asyncio.create_task(_renew_loop())

        try:
            await stop_event.wait()
        finally:
            logger.info("Shutting down…")

            # 7. Stop accepting new connections
            server.close()
            await server.wait_closed()

            # 8. Cancel UPnP renewal + remove mapping
            if renewal_task is not None:
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass

            if upnp_endpoint:
                from app.upnp import remove_upnp_mapping
                await remove_upnp_mapping(upnp_endpoint[1])

            # 9. Deregister (best-effort)
            await deregister_node(http_client, s, node_id)

    logger.info("Home Node shut down cleanly")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"space-router-node {__version__}")
        sys.exit(0)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
