"""Home Node Daemon — entry point.

Lifecycle:
  1. If UPnP enabled, try UPnP/NAT-PMP port mapping
  2. Detect public IP (or use configured value)
  3. Load/generate identity keypair + derive addresses
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
import getpass
import logging
import os
import signal
import sys

import httpx
from dotenv import set_key

from app.config import settings
from app.identity import KeystorePassphraseRequired, load_or_create_identity, write_identity_key
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

_ENV_FILE = ".env"


# ---------------------------------------------------------------------------
# First-run interactive setup (CLI only)
# ---------------------------------------------------------------------------

def _prompt(prompt_text: str, default: str = "") -> str:
    """Prompt the user for input with an optional default."""
    if default:
        display = f"{prompt_text} [{default}]: "
    else:
        display = f"{prompt_text}: "
    value = input(display).strip()
    return value or default


def _first_run_setup() -> bool:
    """Interactive first-time setup wizard.

    Creates the identity key file and writes settings to .env.
    Returns True on success, False if user cancels (Ctrl+C).
    """
    print()
    print("─" * 53)
    print("  SpaceRouter Node — First-Time Setup")
    print("─" * 53)

    try:
        # --- Step 1: Identity Key ---
        print()
        print("1. Identity Key")
        generate = _prompt("   Generate a new identity key? [Y/n]", default="Y").lower()

        if generate in ("y", "yes", ""):
            # Auto-generate — key is created by load_or_create_identity during node start.
            # We create it now so we can show the address.
            identity_key_hex = None
            print("   (Identity key will be generated on first start)")
            node_address = None
        else:
            while True:
                raw = getpass.getpass("   Enter identity private key (hex): ").strip()
                try:
                    from eth_account import Account
                    account = Account.from_key(raw)
                    identity_key_hex = account.key.hex()
                    node_address = account.address.lower()
                    print(f"   ✓ Identity address: {account.address}")
                    break
                except Exception:
                    print("   Invalid private key — expected 32-byte hex (with or without 0x prefix).")

        # --- Step 2: Identity Passphrase ---
        print()
        print("2. Identity Passphrase (optional)")
        encrypt = _prompt("   Encrypt the identity key with a passphrase? [y/N]", default="N").lower()

        passphrase = ""
        if encrypt in ("y", "yes"):
            while True:
                p1 = getpass.getpass("   Enter passphrase: ")
                p2 = getpass.getpass("   Confirm passphrase: ")
                if p1 == p2:
                    passphrase = p1
                    break
                print("   Passphrases do not match — try again.")

        # Write the identity key file now (so we can show the address for steps 3+)
        key_path = settings.IDENTITY_KEY_PATH
        if identity_key_hex is not None:
            node_address = write_identity_key(key_path, identity_key_hex, passphrase)
        else:
            # Auto-generate now so we can show the address
            _, node_address = load_or_create_identity(key_path, passphrase)
            print(f"   ✓ Generated identity address: {node_address}")

        # --- Step 3: Staking Address ---
        print()
        print("3. Staking Address (optional)")
        print(f"   Leave blank to use identity address ({node_address})")
        while True:
            raw = _prompt("   Enter staking wallet address", default="")
            if not raw:
                staking_address = ""
                break
            try:
                staking_address = validate_wallet_address(raw)
                break
            except ValueError as exc:
                print(f"   Invalid address: {exc}")

        effective_staking = staking_address or node_address

        # --- Step 4: Collection Address ---
        print()
        print("4. Collection Address (optional)")
        print(f"   Leave blank to use staking address ({effective_staking})")
        while True:
            raw = _prompt("   Enter collection wallet address", default="")
            if not raw:
                collection_address = ""
                break
            try:
                collection_address = validate_wallet_address(raw)
                break
            except ValueError as exc:
                print(f"   Invalid address: {exc}")

        # --- Persist to .env ---
        if passphrase:
            set_key(_ENV_FILE, "SR_IDENTITY_PASSPHRASE", passphrase)
        if staking_address:
            set_key(_ENV_FILE, "SR_STAKING_ADDRESS", staking_address)
        if collection_address:
            set_key(_ENV_FILE, "SR_COLLECTION_ADDRESS", collection_address)

        print()
        print("─" * 53)
        print(f"  Configuration saved to {_ENV_FILE}")
        print("  Starting node...")
        print("─" * 53)
        print()
        return True

    except (KeyboardInterrupt, EOFError):
        print("\n\nSetup cancelled.")
        return False


# ---------------------------------------------------------------------------
# Main async run loop
# ---------------------------------------------------------------------------

async def _run(settings_override=None, stop_event=None) -> None:  # noqa: ANN001
    s = settings_override or settings
    stop_event_arg = stop_event
    if stop_event is None:
        stop_event = asyncio.Event()

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
        if s.PUBLIC_IP:
            public_ip = s.PUBLIC_IP
            logger.info("Using configured public IP: %s", public_ip)
        else:
            try:
                public_ip = await detect_public_ip(http_client)
            except RuntimeError:
                logger.error("Cannot detect public IP — aborting")
                sys.exit(1)
        s.PUBLIC_IP = public_ip

        # 3. Load or create node identity keypair
        try:
            identity_key, node_address = load_or_create_identity(
                s.IDENTITY_KEY_PATH, s.IDENTITY_PASSPHRASE,
            )
        except KeystorePassphraseRequired:
            if stop_event_arg is not None:
                raise  # GUI path — NodeManager surfaces this to the frontend
            # CLI path: prompt interactively
            passphrase = getpass.getpass("Identity keystore passphrase: ")
            identity_key, node_address = load_or_create_identity(
                s.IDENTITY_KEY_PATH, passphrase,
            )

        logger.info("Node identity: %s", node_address)

        # Staking address falls back to identity address if not configured
        staking_address = s.STAKING_ADDRESS or node_address
        logger.info("Staking address: %s", staking_address)

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
        max_retries = int(os.environ.get("SR_REGISTER_MAX_RETRIES", "5"))
        backoff = 5  # seconds, doubles each retry
        for attempt in range(1, max_retries + 1):
            try:
                node_id, gateway_ca_cert = await register_node(
                    http_client, s, public_ip,
                    identity_key=identity_key,
                    node_address=node_address,
                    upnp_endpoint=upnp_endpoint,
                    wallet_address=staking_address,
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

        logger.info(
            "Home Node ready (node_id=%s, staking=%s, upnp=%s)",
            node_id, staking_address,
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

    # First-run wizard: trigger when identity key file doesn't exist yet,
    # but only in interactive (TTY) sessions — skip silently in CI/piped mode.
    if not os.path.isfile(settings.IDENTITY_KEY_PATH) and sys.stdin.isatty():
        if not _first_run_setup():
            sys.exit(0)
        # Reload settings so _run() picks up values written to .env
        from importlib import reload
        import app.config as config_mod
        reload(config_mod)
        from app.config import settings as reloaded_settings
        try:
            asyncio.run(_run(settings_override=reloaded_settings))
        finally:
            if sys.platform == "win32":
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return

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
