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
from app.identity import load_or_create_identity
from app.proxy_handler import handle_client
from app.registration import deregister_node, detect_public_ip, register_node, save_gateway_ca_cert
from app.tls import create_mtls_server_ssl_context, create_server_ssl_context, ensure_certificates
from app.version import __version__
from app.wallet import validate_wallet_address

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run(settings_override=None, stop_event=None, on_phase=None) -> None:  # noqa: ANN001
    s = settings_override or settings
    stop_event_arg = stop_event
    if stop_event is None:
        stop_event = asyncio.Event()

    def _report(phase: str) -> None:
        if on_phase:
            on_phase(phase)

    # Only install signal handlers when running as the main daemon (not from
    # the GUI, which passes its own stop_event and runs _run() in a background
    # thread where signal APIs are unavailable).
    if stop_event_arg is None:
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

        # 2. Detect public IP (needed for registration endpoint_url)
        #    Always detect the real exit IP for classification, even when
        #    SR_PUBLIC_IP is set (it may be a tunnel hostname like bore.pub).
        try:
            real_ip = await detect_public_ip(http_client)
        except RuntimeError:
            real_ip = None

        if s.PUBLIC_IP:
            public_ip = s.PUBLIC_IP
            logger.info("Using configured public IP: %s", public_ip)
            if real_ip and real_ip != public_ip:
                logger.info("Detected exit IP: %s (differs from configured — tunnel mode)", real_ip)
            elif real_ip is None:
                logger.warning(
                    "Could not detect real exit IP — tunnel mode classification "
                    "may use hostname instead of IP: %s", public_ip,
                )
        else:
            if not real_ip:
                logger.error("Cannot detect public IP — aborting")
                sys.exit(1)
            public_ip = real_ip
            # detect_public_ip() already logged the IP and source service
        s.PUBLIC_IP = public_ip
        s._REAL_EXIT_IP = real_ip  # actual IP seen by external services

        # 3. Validate wallet addresses
        #    v0.2.0: SR_STAKING_ADDRESS + SR_COLLECTION_ADDRESS (preferred)
        #    v0.1.2: SR_WALLET_ADDRESS only (backward compat)
        staking_address = s.STAKING_ADDRESS.strip()
        collection_address = s.COLLECTION_ADDRESS.strip()

        if staking_address:
            try:
                staking_address = validate_wallet_address(staking_address)
            except ValueError as exc:
                logger.error("Invalid staking address: %s — aborting", exc)
                sys.exit(1)
            if collection_address:
                try:
                    collection_address = validate_wallet_address(collection_address)
                except ValueError as exc:
                    logger.error("Invalid collection address: %s — aborting", exc)
                    sys.exit(1)
            else:
                collection_address = staking_address
            # Also set WALLET_ADDRESS for any code that reads it
            wallet_address = staking_address
            s.WALLET_ADDRESS = staking_address
            logger.info("Staking address: %s (v0.2.0)", staking_address)
            logger.info("Collection address: %s", collection_address)
        else:
            # v0.1.2 fallback — single wallet address
            if not s.WALLET_ADDRESS:
                logger.error("SR_STAKING_ADDRESS or SR_WALLET_ADDRESS is required — aborting")
                sys.exit(1)
            try:
                s.WALLET_ADDRESS = validate_wallet_address(s.WALLET_ADDRESS)
            except ValueError as exc:
                logger.error("Invalid wallet address: %s — aborting", exc)
                sys.exit(1)
            wallet_address = s.WALLET_ADDRESS
            logger.info("Wallet address: %s (v0.1.2 compat)", wallet_address)

        # 3b. Load or create node identity keypair
        identity_key, node_address = load_or_create_identity(s.IDENTITY_KEY_PATH)
        logger.info("Node identity: %s", node_address)

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
        #    Retry with exponential back-off so transient failures
        #    (e.g. Coordination API rollout) don't kill the node.
        _report("registering")
        max_retries = int(os.environ.get("SR_REGISTER_MAX_RETRIES", "5"))
        backoff = 5  # seconds, doubles each retry
        for attempt in range(1, max_retries + 1):
            try:
                node_id, gateway_ca_cert = await register_node(
                    http_client, s, public_ip,
                    identity_key=identity_key,
                    upnp_endpoint=upnp_endpoint,
                    wallet_address=wallet_address,
                    staking_address=staking_address,
                    collection_address=collection_address,
                )
                break  # success
            except Exception:
                if attempt == max_retries:
                    logger.exception(
                        "Failed to register after %d attempts — aborting",
                        max_retries,
                    )
                    server.close()
                    await server.wait_closed()
                    sys.exit(1)
                logger.warning(
                    "Registration attempt %d/%d failed, retrying in %ds…",
                    attempt, max_retries, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        # 5b. Save gateway CA cert if provided
        if gateway_ca_cert:
            save_gateway_ca_cert(gateway_ca_cert, s.GATEWAY_CA_CERT_PATH)

        # 6. Upgrade to mTLS if enabled and CA cert is available
        if s.MTLS_ENABLED:
            if not os.path.isfile(s.GATEWAY_CA_CERT_PATH):
                logger.warning(
                    "mTLS enabled but gateway CA cert not found at %s "
                    "— falling back to standard TLS (gateway may not have CA configured yet)",
                    s.GATEWAY_CA_CERT_PATH,
                )
            else:
                try:
                    logger.info("Upgrading to mTLS…")
                    ssl_ctx = create_mtls_server_ssl_context(
                        s.TLS_CERT_PATH, s.TLS_KEY_PATH, s.GATEWAY_CA_CERT_PATH,
                    )
                    # Close and immediately rebind to minimise the port-unavailable
                    # window.  We cannot use reuse_port because the initial server
                    # was created without it (Linux requires all sockets sharing a
                    # port to set SO_REUSEPORT), and Windows lacks SO_REUSEPORT.
                    server.close()
                    await server.wait_closed()
                    server = await asyncio.start_server(
                        handler, host=s.BIND_ADDRESS, port=s.NODE_PORT, ssl=ssl_ctx,
                    )
                except Exception:
                    logger.warning(
                        "mTLS upgrade failed — continuing with standard TLS",
                        exc_info=True,
                    )

        display_wallet = staking_address or wallet_address
        _report("running")
        logger.info(
            "Home Node ready (node_id=%s, wallet=%s, upnp=%s)",
            node_id, display_wallet,
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

            # 9. Deregister (best-effort, signed)
            await deregister_node(http_client, s, node_id, identity_key=identity_key)

    logger.info("Home Node shut down cleanly")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"space-router-node {__version__}")
        sys.exit(0)
    try:
        asyncio.run(_run())
    finally:
        # Restore default signal handlers on Windows to avoid calling
        # loop.call_soon_threadsafe() on the now-closed event loop if a
        # late signal arrives between asyncio.run() returning and process exit.
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


if __name__ == "__main__":
    main()
