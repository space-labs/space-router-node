"""Home Node Daemon — entry point.

Lifecycle phases:
  1. INITIALIZING — UPnP, IP detection, wallet validation, identity key, TLS certs
  2. BINDING — Start TLS server on configured port
  3. REGISTERING — Register with Coordination API (triggers challenge probe)
  4. RUNNING — Serve traffic, health checks, UPnP renewal
  5. STOPPING — Deregister, close server, remove UPnP mapping
"""

import asyncio
import datetime
import functools
import getpass
import logging
import os
import signal
import socket
import sys

import httpx
from dotenv import set_key

from app.config import Settings, load_settings, _default_coordination_url
from app.errors import NodeError, NodeErrorCode, classify_error
from app.identity import KeystorePassphraseRequired, load_or_create_identity, write_identity_key
from app.proxy_handler import handle_client
from app.registration import (
    check_node_status,
    deregister_node,
    detect_public_ip,
    register_node,
    request_probe,
    save_gateway_ca_cert,
)
from app.state import NodeState, NodeStateMachine
from app.tls import (
    check_certificate_expiry,
    create_mtls_server_ssl_context,
    create_server_ssl_context,
    ensure_certificates,
)
from app.version import __version__
from app.wallet import validate_wallet_address

logger = logging.getLogger(__name__)

# Health check intervals
_HEARTBEAT_INTERVAL = 300  # 5 minutes
_CERT_CHECK_INTERVAL = 86400  # 24 hours
_PROBE_REQUEST_INTERVAL = 1800  # 30 minutes
_HEARTBEAT_FAIL_THRESHOLD = 3

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
    Skips identity key steps when the key already exists (e.g. after --reset --keep-identity).
    Returns True on success, False if user cancels (Ctrl+C).
    """
    s = load_settings()
    key_exists = os.path.isfile(s.IDENTITY_KEY_PATH)
    step = 1

    print()
    print("─" * 53)
    print("  SpaceRouter Node — Setup")
    print("─" * 53)

    try:
        identity_address = None
        passphrase = ""

        if key_exists:
            # Identity key already exists — load it and skip key/passphrase steps
            try:
                _, identity_address = load_or_create_identity(s.IDENTITY_KEY_PATH)
                print(f"\n   Identity key found: {identity_address}")
            except KeystorePassphraseRequired:
                passphrase = getpass.getpass("\n   Identity key is encrypted. Passphrase: ")
                _, identity_address = load_or_create_identity(s.IDENTITY_KEY_PATH, passphrase)
                print(f"   ✓ Unlocked identity: {identity_address}")
        else:
            # --- Step 1: Identity Key ---
            print()
            print(f"{step}. Identity Key")
            step += 1
            generate = _prompt("   Generate a new identity key? [Y/n]", default="Y").lower()

            identity_key_hex = None
            if generate in ("y", "yes", ""):
                print("   (Identity key will be generated on first start)")
            else:
                while True:
                    raw = getpass.getpass("   Enter identity private key (hex): ").strip()
                    try:
                        from eth_account import Account
                        account = Account.from_key(raw)
                        identity_key_hex = account.key.hex()
                        identity_address = account.address.lower()
                        print(f"   ✓ Identity address: {account.address}")
                        break
                    except Exception:
                        print("   Invalid private key — expected 32-byte hex (with or without 0x prefix).")

            # --- Step 2: Identity Passphrase ---
            print()
            print(f"{step}. Identity Passphrase (optional)")
            step += 1
            encrypt = _prompt("   Encrypt the identity key with a passphrase? [y/N]", default="N").lower()

            if encrypt in ("y", "yes"):
                while True:
                    p1 = getpass.getpass("   Enter passphrase: ")
                    p2 = getpass.getpass("   Confirm passphrase: ")
                    if p1 == p2:
                        passphrase = p1
                        break
                    print("   Passphrases do not match — try again.")

            # Write the identity key file now (so we can show the address for steps 3+)
            key_path = s.IDENTITY_KEY_PATH
            if identity_key_hex is not None:
                identity_address = write_identity_key(key_path, identity_key_hex, passphrase)
            else:
                _, identity_address = load_or_create_identity(key_path, passphrase)
                print(f"   ✓ Generated identity address: {identity_address}")

        # --- Staking Address ---
        print()
        print(f"{step}. Staking Address (optional)")
        step += 1
        print(f"   Leave blank to use identity address ({identity_address})")
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

        effective_staking = staking_address or identity_address

        # --- Collection Address ---
        print()
        print(f"{step}. Collection Address (optional)")
        step += 1
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


# ── Phase functions ──────────────────────────────────────────────────────────

class _NodeContext:
    """Mutable context passed between phases to accumulate state."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.s = settings
        self.http = http_client
        self.public_ip: str = ""
        self.upnp_endpoint: tuple[str, int] | None = None
        self.identity_key: str = ""
        self.identity_address: str = ""
        self.staking_address: str = ""
        self.collection_address: str = ""
        self.wallet_address: str = ""
        self.ssl_ctx = None
        self.server: asyncio.Server | None = None
        self.node_id: str = ""
        self.gateway_ca_cert: str | None = None


async def _phase_init(ctx: _NodeContext) -> None:
    """INITIALIZING: UPnP, IP detection, wallet validation, identity, TLS."""
    s = ctx.s

    # 1. UPnP port mapping
    if s.UPNP_ENABLED:
        from app.upnp import setup_upnp_mapping

        ctx.upnp_endpoint = await setup_upnp_mapping(
            s.NODE_PORT, lease_duration=s.UPNP_LEASE_DURATION,
        )
        if ctx.upnp_endpoint:
            logger.info("UPnP mapping active: %s:%d", ctx.upnp_endpoint[0], ctx.upnp_endpoint[1])
        else:
            logger.warning("UPnP enabled but mapping failed — falling back to direct public IP mode")

    # 2. Public IP detection
    try:
        real_ip = await detect_public_ip(ctx.http)
    except RuntimeError:
        real_ip = None

    if s.PUBLIC_IP:
        ctx.public_ip = s.PUBLIC_IP
        logger.info("Using configured public IP: %s", ctx.public_ip)
        if real_ip and real_ip != ctx.public_ip:
            logger.info("Detected exit IP: %s (tunnel mode)", real_ip)
    else:
        if not real_ip:
            raise NodeError(NodeErrorCode.NETWORK_UNREACHABLE, "Cannot detect public IP")
        ctx.public_ip = real_ip
    s.PUBLIC_IP = ctx.public_ip
    s._REAL_EXIT_IP = real_ip

    # 3. Wallet validation
    staking = s.STAKING_ADDRESS.strip()
    collection = s.COLLECTION_ADDRESS.strip()

    if staking:
        try:
            staking = validate_wallet_address(staking)
        except ValueError as exc:
            raise NodeError(NodeErrorCode.INVALID_WALLET, f"Invalid staking address: {exc}")
        if collection:
            try:
                collection = validate_wallet_address(collection)
            except ValueError as exc:
                raise NodeError(NodeErrorCode.INVALID_WALLET, f"Invalid collection address: {exc}")
        else:
            collection = staking
        ctx.staking_address = staking
        ctx.collection_address = collection
        ctx.wallet_address = staking
        logger.info("Staking address: %s (v0.2.0)", staking)
        logger.info("Collection address: %s", collection)
    else:
        # No staking address configured — identity address will be used as fallback
        logger.info("No staking address configured — will use identity address as fallback")

    # 4. Identity keypair (with passphrase support)
    try:
        ctx.identity_key, ctx.identity_address = load_or_create_identity(
            s.IDENTITY_KEY_PATH, s.IDENTITY_PASSPHRASE,
        )
    except KeystorePassphraseRequired:
        raise  # Let caller (NodeManager or CLI) handle passphrase prompt
    except Exception as exc:
        raise NodeError(NodeErrorCode.IDENTITY_KEY_ERROR, str(exc))
    logger.info("Node identity: %s", ctx.identity_address)

    # Staking address falls back to identity address if not configured
    if not ctx.staking_address:
        ctx.staking_address = ctx.identity_address
        ctx.wallet_address = ctx.identity_address
        logger.info("Staking address (identity fallback): %s", ctx.staking_address)

    # 5. TLS certificates
    try:
        ensure_certificates(s.TLS_CERT_PATH, s.TLS_KEY_PATH)
        ctx.ssl_ctx = create_server_ssl_context(s.TLS_CERT_PATH, s.TLS_KEY_PATH)
    except Exception as exc:
        raise NodeError(NodeErrorCode.TLS_CERT_ERROR, str(exc))


async def _phase_bind(ctx: _NodeContext) -> None:
    """BINDING: Start the TLS server."""
    s = ctx.s
    handler = functools.partial(handle_client, settings=s)

    # Use SO_REUSEADDR to avoid "address already in use" after restart
    server = await asyncio.start_server(
        handler,
        host=s.BIND_ADDRESS,
        port=s.NODE_PORT,
        ssl=ctx.ssl_ctx,
        reuse_address=True,
    )
    ctx.server = server
    logger.info("Home Node listening on port %d", s.NODE_PORT)


async def _phase_register(ctx: _NodeContext) -> None:
    """REGISTERING: Register with the Coordination API."""
    node_id, gateway_ca_cert = await register_node(
        ctx.http, ctx.s, ctx.public_ip,
        identity_key=ctx.identity_key,
        upnp_endpoint=ctx.upnp_endpoint,
        wallet_address=ctx.wallet_address,
        staking_address=ctx.staking_address,
        collection_address=ctx.collection_address,
    )
    ctx.node_id = node_id
    ctx.gateway_ca_cert = gateway_ca_cert

    # Save gateway CA cert if provided
    if gateway_ca_cert:
        save_gateway_ca_cert(gateway_ca_cert, ctx.s.GATEWAY_CA_CERT_PATH)

    # Upgrade to mTLS if enabled
    _upgrade_mtls(ctx)


def _upgrade_mtls(ctx: _NodeContext) -> None:
    """Attempt mTLS upgrade (non-fatal on failure)."""
    s = ctx.s
    if not s.MTLS_ENABLED:
        return
    if not os.path.isfile(s.GATEWAY_CA_CERT_PATH):
        logger.warning("mTLS enabled but gateway CA cert not found — using standard TLS")
        return
    try:
        logger.info("Upgrading to mTLS…")
        ctx.ssl_ctx = create_mtls_server_ssl_context(
            s.TLS_CERT_PATH, s.TLS_KEY_PATH, s.GATEWAY_CA_CERT_PATH,
        )
        logger.info("mTLS context ready — server will rebind on next cycle")
    except Exception:
        logger.warning("mTLS upgrade failed — continuing with standard TLS", exc_info=True)


async def _rebind_server_mtls(ctx: _NodeContext) -> None:
    """Close and rebind server with the (possibly upgraded) SSL context."""
    s = ctx.s
    if ctx.server:
        ctx.server.close()
        await ctx.server.wait_closed()
    handler = functools.partial(handle_client, settings=s)
    ctx.server = await asyncio.start_server(
        handler, host=s.BIND_ADDRESS, port=s.NODE_PORT, ssl=ctx.ssl_ctx,
        reuse_address=True,
    )


async def _health_loop(
    ctx: _NodeContext,
    sm: NodeStateMachine,
    stop_event: asyncio.Event,
) -> None:
    """Periodic health checks while RUNNING."""
    consecutive_failures = 0
    last_cert_check = 0.0

    import time
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_HEARTBEAT_INTERVAL,
            )
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed, run checks

        # Heartbeat: check if node is still registered
        try:
            status = await check_node_status(
                ctx.http, ctx.s, ctx.node_id, identity_key=ctx.identity_key,
            )
            if status in ("online", "active"):
                consecutive_failures = 0
            else:
                logger.warning("Health check: node status is '%s'", status)
                consecutive_failures += 1
        except Exception as exc:
            consecutive_failures += 1
            logger.warning("Health check failed (%d/%d): %s",
                           consecutive_failures, _HEARTBEAT_FAIL_THRESHOLD, exc)

        if consecutive_failures >= _HEARTBEAT_FAIL_THRESHOLD:
            logger.warning("Health check threshold reached — triggering reconnection")
            sm.transition(NodeState.RECONNECTING, "Lost connection to coordination server")
            return  # exit health loop; orchestrator handles reconnection

        # Certificate expiry check
        now = time.time()
        if now - last_cert_check > _CERT_CHECK_INTERVAL:
            last_cert_check = now
            expiry = check_certificate_expiry(ctx.s.TLS_CERT_PATH)
            if expiry:
                days_left = (expiry - datetime.datetime.now(datetime.timezone.utc)).days
                if days_left < 30:
                    sm.set_cert_warning(True)
                    logger.warning("TLS certificate expires in %d days", days_left)
                    if days_left < 7:
                        logger.info("Auto-renewing TLS certificate…")
                        try:
                            os.remove(ctx.s.TLS_CERT_PATH)
                            os.remove(ctx.s.TLS_KEY_PATH)
                            ensure_certificates(ctx.s.TLS_CERT_PATH, ctx.s.TLS_KEY_PATH)
                            ctx.ssl_ctx = create_server_ssl_context(ctx.s.TLS_CERT_PATH, ctx.s.TLS_KEY_PATH)
                            await _rebind_server_mtls(ctx)
                            sm.set_cert_warning(False)
                            logger.info("TLS certificate renewed")
                        except Exception:
                            logger.warning("Certificate renewal failed", exc_info=True)
                else:
                    sm.set_cert_warning(False)

        # Periodic probe request (every 30 min, non-critical)
        try:
            await request_probe(ctx.http, ctx.s, ctx.node_id, identity_key=ctx.identity_key)
        except Exception:
            pass  # non-critical


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def _run(
    settings_override=None,  # noqa: ANN001
    stop_event: asyncio.Event | None = None,
    on_phase=None,  # noqa: ANN001
    state_machine: NodeStateMachine | None = None,
) -> None:
    """Main orchestrator loop. Drives phases and handles retries."""
    s = settings_override or load_settings()

    # Configure logging from settings
    log_level = getattr(logging, s.LOG_LEVEL.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level)

    own_stop_event = stop_event is None
    if stop_event is None:
        stop_event = asyncio.Event()

    sm = state_machine or NodeStateMachine()

    def _report(state: NodeState, detail: str = "") -> None:
        sm.transition(state, detail)
        if on_phase:
            on_phase(state.value)

    # Signal handlers (standalone mode only)
    if own_stop_event:
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop_event.set)
        else:
            loop = asyncio.get_running_loop()

            def _handle_signal(signum, frame):  # noqa: ANN001
                loop.call_soon_threadsafe(stop_event.set)

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

    async with httpx.AsyncClient() as http_client:
        ctx = _NodeContext(s, http_client)
        renewal_task = None
        health_task = None

        try:
            # ── Phase: INITIALIZING ──
            _report(NodeState.INITIALIZING, "Loading identity and certificates")
            try:
                await _phase_init(ctx)
            except KeystorePassphraseRequired:
                if state_machine:
                    state_machine.transition(
                        NodeState.PASSPHRASE_REQUIRED,
                        "Identity key is encrypted — passphrase required",
                    )
                raise
            except NodeError:
                raise
            except Exception as exc:
                raise classify_error(exc)

            if stop_event.is_set():
                return

            # ── Phase: BINDING ──
            _report(NodeState.BINDING, f"Binding to port {s.NODE_PORT}")
            try:
                await _phase_bind(ctx)
            except NodeError:
                raise
            except Exception as exc:
                raise classify_error(exc)

            if stop_event.is_set():
                return

            # ── Phase: REGISTERING ──
            _report(NodeState.REGISTERING, "Registering with coordination server")
            try:
                await _phase_register(ctx)
            except NodeError:
                raise
            except Exception as exc:
                raise classify_error(exc)

            # mTLS rebind if upgrade happened
            if ctx.s.MTLS_ENABLED and os.path.isfile(ctx.s.GATEWAY_CA_CERT_PATH):
                try:
                    await _rebind_server_mtls(ctx)
                except Exception:
                    logger.warning("mTLS server rebind failed", exc_info=True)

            sm.set_node_id(ctx.node_id)

            # ── Phase: RUNNING ──
            _report(NodeState.RUNNING, f"Node ID: {ctx.node_id[:12]}...")

            display_wallet = ctx.staking_address or ctx.wallet_address
            logger.info(
                "Home Node ready (node_id=%s, wallet=%s, upnp=%s)",
                ctx.node_id, display_wallet,
                f"{ctx.upnp_endpoint[0]}:{ctx.upnp_endpoint[1]}" if ctx.upnp_endpoint else "disabled",
            )

            # Start UPnP renewal
            if ctx.upnp_endpoint and s.UPNP_LEASE_DURATION > 0:
                from app.upnp import renew_upnp_mapping

                async def _renew_loop() -> None:
                    interval = max(s.UPNP_LEASE_DURATION // 2, 60)
                    while True:
                        await asyncio.sleep(interval)
                        ok = await renew_upnp_mapping(
                            s.NODE_PORT, ctx.upnp_endpoint[1], s.UPNP_LEASE_DURATION,
                        )
                        if ok:
                            logger.debug("UPnP lease renewed")
                        else:
                            logger.warning("UPnP lease renewal failed")

                renewal_task = asyncio.create_task(_renew_loop())

            # Start health monitoring
            health_task = asyncio.create_task(_health_loop(ctx, sm, stop_event))

            # Wait for stop or health loop exit (reconnection trigger)
            done, pending = await asyncio.wait(
                [asyncio.create_task(stop_event.wait()), health_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            # If health loop exited (RECONNECTING), handle reconnection
            if sm.state == NodeState.RECONNECTING:
                # Cancel health task if still running
                if health_task and not health_task.done():
                    health_task.cancel()

                # Retry registration while server stays up
                while not stop_event.is_set() and sm.state == NodeState.RECONNECTING:
                    try:
                        await _phase_register(ctx)
                        sm.set_node_id(ctx.node_id)
                        # Rebind server with mTLS if applicable
                        if ctx.s.MTLS_ENABLED and os.path.isfile(ctx.s.GATEWAY_CA_CERT_PATH):
                            try:
                                await _rebind_server_mtls(ctx)
                            except Exception:
                                logger.warning("mTLS server rebind failed", exc_info=True)
                        _report(NodeState.RUNNING, f"Reconnected (Node ID: {ctx.node_id[:12]}...)")
                        logger.info("Reconnected successfully")
                        # Restart health loop
                        health_task = asyncio.create_task(_health_loop(ctx, sm, stop_event))
                        done, pending = await asyncio.wait(
                            [asyncio.create_task(stop_event.wait()), health_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                    except Exception as exc:
                        error = classify_error(exc) if not isinstance(exc, NodeError) else exc
                        delay = sm.handle_error(error, NodeState.RECONNECTING)
                        if on_phase:
                            on_phase(sm.state.value)
                        if delay is None:
                            break  # permanent error
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=delay)
                            break  # stop requested during wait
                        except asyncio.TimeoutError:
                            sm.transition(NodeState.RECONNECTING, "Retrying registration")
                            if on_phase:
                                on_phase(sm.state.value)

        except NodeError as exc:
            # Let the caller (NodeManager) handle the error
            raise
        except Exception as exc:
            raise classify_error(exc)
        finally:
            logger.info("Shutting down…")

            # Stop accepting new connections
            if ctx.server:
                ctx.server.close()
                await ctx.server.wait_closed()

            # Cancel background tasks
            for task in (renewal_task, health_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Remove UPnP mapping
            if ctx.upnp_endpoint:
                from app.upnp import remove_upnp_mapping
                await remove_upnp_mapping(ctx.upnp_endpoint[1])

            # Deregister (best-effort)
            if ctx.node_id:
                await deregister_node(ctx.http, s, ctx.node_id, identity_key=ctx.identity_key)

    logger.info("Home Node shut down cleanly")


def _do_reset() -> None:
    """Handle --reset: delete config and optionally identity key."""
    from app.paths import config_dir

    keep_identity = "--keep-identity" in sys.argv
    s = load_settings()

    # Check both well-known config dir and CWD for config files
    cfg_dir = config_dir()
    wellknown_env = cfg_dir / "spacerouter.env"
    cwd_env = os.path.abspath(".env")

    env_file = str(wellknown_env) if wellknown_env.is_file() else cwd_env
    certs_dir = os.path.dirname(os.path.abspath(s.IDENTITY_KEY_PATH)) or "certs"
    identity_path = os.path.abspath(s.IDENTITY_KEY_PATH)

    if not keep_identity and sys.stdin.isatty():
        print("WARNING: This will delete your identity key and all configuration.")
        confirm = input("Type YES to confirm: ").strip()
        if confirm != "YES":
            print("Reset cancelled.")
            sys.exit(0)

    # Delete .env
    if os.path.isfile(env_file):
        os.remove(env_file)
        print(f"Removed {env_file}")

    # Delete certs (except identity key if --keep-identity)
    if os.path.isdir(certs_dir):
        import shutil
        if keep_identity:
            for f in os.listdir(certs_dir):
                fp = os.path.join(certs_dir, f)
                if os.path.abspath(fp) != identity_path and os.path.isfile(fp):
                    os.remove(fp)
                    print(f"Removed {fp}")
            print(f"Kept identity key: {identity_path}")
        else:
            shutil.rmtree(certs_dir)
            print(f"Removed {certs_dir}/")

    print("Reset complete." + (" Identity key preserved." if keep_identity else ""))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"space-router-node {__version__}")
        sys.exit(0)

    if "--reset" in sys.argv:
        _do_reset()
        sys.exit(0)

    # --password-file: read passphrase from file for automation/systemd
    if "--password-file" in sys.argv:
        idx = sys.argv.index("--password-file")
        if idx + 1 >= len(sys.argv):
            print("Error: --password-file requires a file path argument", file=sys.stderr)
            sys.exit(1)
        pw_path = sys.argv[idx + 1]
        try:
            with open(pw_path) as f:
                os.environ["SR_IDENTITY_PASSPHRASE"] = f.readline().rstrip("\n")
        except (OSError, IOError) as exc:
            print(f"Error reading password file: {exc}", file=sys.stderr)
            sys.exit(1)

    # Setup wizard: trigger when identity key is missing, when --setup is
    # passed explicitly, or when config looks unconfigured (no staking address
    # and default coordination URL). Only in interactive TTY.
    s = load_settings()
    explicit_setup = "--setup" in sys.argv
    needs_setup = (
        explicit_setup
        or not os.path.isfile(s.IDENTITY_KEY_PATH)
        or (not s.STAKING_ADDRESS and s.COORDINATION_API_URL == _default_coordination_url())
    )
    if needs_setup and sys.stdin.isatty():
        if not _first_run_setup():
            sys.exit(0)
        # Reload settings so _run() picks up values written to .env
        reloaded_settings = load_settings()
        try:
            asyncio.run(_run(settings_override=reloaded_settings))
        finally:
            if sys.platform == "win32":
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
        return

    try:
        asyncio.run(_run())
    except KeystorePassphraseRequired:
        if sys.stdin.isatty():
            # Prompt for passphrase and retry
            try:
                passphrase = getpass.getpass("Identity key passphrase: ")
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(1)
            os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase
            try:
                asyncio.run(_run(settings_override=load_settings()))
            except NodeError as exc:
                logger.error("Node failed: %s", exc.user_message)
                sys.exit(1)
            finally:
                if sys.platform == "win32":
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
        else:
            print(
                "Identity key is encrypted. Set SR_IDENTITY_PASSPHRASE "
                "environment variable or run interactively.",
                file=sys.stderr,
            )
            sys.exit(1)
    except NodeError as exc:
        logger.error("Node failed: %s", exc.user_message)
        sys.exit(1)
    finally:
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


if __name__ == "__main__":
    main()
