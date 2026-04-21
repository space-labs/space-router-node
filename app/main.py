"""Home Node Daemon — entry point.

Lifecycle phases:
  1. INITIALIZING — UPnP, IP detection, wallet validation, identity key, TLS certs
  2. BINDING — Start TLS server on configured port
  3. REGISTERING — Register with Coordination API (triggers challenge probe)
  4. RUNNING — Serve traffic, health checks, UPnP renewal
  5. STOPPING — Deregister, close server, remove UPnP mapping
"""

import argparse
import asyncio
import datetime
import functools
import getpass
import logging
import os
import signal
import socket
import sys

from dotenv import get_key, set_key

# Light imports only — heavy libraries (httpx, cryptography, web3, etc.)
# are deferred to first use inside _run() / _phase_*() to keep CLI startup fast.
from app.config import load_settings, _default_coordination_url
from app.identity import KeystorePassphraseRequired, load_or_create_identity, write_identity_key
from app.state import NodeState, NodeStateMachine
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

def _first_run_setup() -> bool:
    """Interactive first-time setup wizard with rich prompts.

    Creates the identity key file and writes settings to .env.
    Skips identity key steps when the key already exists.
    Returns True on success, False if user cancels (Ctrl+C).
    """
    from app.cli_ui import (
        wizard_banner, wizard_step, wizard_select, wizard_input,
        wizard_confirm, wizard_success, wizard_error, wizard_info, wizard_done,
    )

    s = load_settings()
    key_exists = os.path.isfile(s.IDENTITY_KEY_PATH)
    step = 1

    wizard_banner()

    try:
        identity_address = None
        passphrase = ""

        if key_exists:
            try:
                _, identity_address = load_or_create_identity(s.IDENTITY_KEY_PATH)
                wizard_success(f"Identity key found: {identity_address}")
            except KeystorePassphraseRequired:
                passphrase = wizard_input("Identity key is encrypted. Passphrase", password=True)
                _, identity_address = load_or_create_identity(s.IDENTITY_KEY_PATH, passphrase)
                wizard_success(f"Unlocked identity: {identity_address}")
        else:
            # --- Step 1: Identity Key ---
            wizard_step(step, "Identity Key")
            step += 1
            idx = wizard_select("", [
                ("Generate new key", "(recommended)"),
                ("Import existing key", "(paste private key hex)"),
            ], default=0)

            identity_key_hex = None
            if idx == 0:
                wizard_info("Identity key will be generated on first start")
            else:
                while True:
                    raw = wizard_input("Enter identity private key (hex)", password=True)
                    try:
                        from eth_account import Account
                        account = Account.from_key(raw)
                        identity_key_hex = account.key.hex()
                        identity_address = account.address.lower()
                        wizard_success(f"Identity address: {account.address}")
                        break
                    except Exception:
                        wizard_error("Invalid private key — expected 32-byte hex (with or without 0x prefix)")

            # --- Step 2: Identity Passphrase ---
            wizard_step(step, "Identity Passphrase (optional)")
            step += 1
            encrypt = wizard_confirm("Encrypt identity key with a passphrase?", default=False)

            if encrypt:
                while True:
                    p1 = wizard_input("Enter passphrase", password=True)
                    p2 = wizard_input("Confirm passphrase", password=True)
                    if p1 == p2:
                        passphrase = p1
                        break
                    wizard_error("Passphrases do not match — try again")

            # Write the identity key file now
            key_path = s.IDENTITY_KEY_PATH
            if identity_key_hex is not None:
                identity_address = write_identity_key(key_path, identity_key_hex, passphrase)
            else:
                _, identity_address = load_or_create_identity(key_path, passphrase)
                wizard_success(f"Generated identity address: {identity_address}")

        # --- Staking Address ---
        wizard_step(step, "Staking Address (optional)")
        step += 1
        wizard_info(f"Leave blank to use identity address ({identity_address})")
        while True:
            raw = wizard_input("Staking wallet address")
            if not raw:
                staking_address = ""
                break
            try:
                staking_address = validate_wallet_address(raw)
                break
            except ValueError as exc:
                wizard_error(f"Invalid address: {exc}")

        effective_staking = staking_address or identity_address

        # --- Collection Address ---
        wizard_step(step, "Collection Address (optional)")
        step += 1
        wizard_info(f"Leave blank to use staking address ({effective_staking})")
        while True:
            raw = wizard_input("Collection wallet address")
            if not raw:
                collection_address = ""
                break
            try:
                collection_address = validate_wallet_address(raw)
                break
            except ValueError as exc:
                wizard_error(f"Invalid address: {exc}")

        # --- Referral Code ---
        wizard_step(step, "Referral Code (optional)")
        step += 1
        existing_referral = get_key(_ENV_FILE, "SR_REFERRAL_CODE") or ""
        if existing_referral:
            wizard_success(f"Referral code already set: {existing_referral}")
            referral_code = existing_referral
        else:
            wizard_info("Partner referral code for acquisition tracking")
            while True:
                raw = wizard_input("Referral code")
                if not raw:
                    referral_code = ""
                    break
                raw = raw.strip()
                if len(raw) < 3 or len(raw) > 50:
                    wizard_error("Must be 3-50 characters")
                    continue
                import re
                if not re.match(r'^[a-zA-Z0-9_-]+$', raw):
                    wizard_error("Only letters, numbers, hyphens, and underscores allowed")
                    continue
                referral_code = raw
                break

        # --- Network Configuration ---
        wizard_step(step, "Network Configuration")
        step += 1
        choice = wizard_select("", [
            ("Automatic (UPnP)", "recommended for home routers"),
            ("Manual / Tunnel", "you provide public hostname and port"),
        ], default=0)

        upnp_enabled = True
        public_ip = ""
        public_port = ""

        if choice == 1:
            upnp_enabled = False
            while True:
                public_ip = wizard_input("Public hostname or IP").strip()
                if public_ip:
                    break
                wizard_error("Hostname is required for tunnel mode")
            public_port = wizard_input("Public port", default="9090")

        # --- Persist to .env ---
        if passphrase:
            set_key(_ENV_FILE, "SR_IDENTITY_PASSPHRASE", passphrase)
        if staking_address:
            set_key(_ENV_FILE, "SR_STAKING_ADDRESS", staking_address)
        if collection_address:
            set_key(_ENV_FILE, "SR_COLLECTION_ADDRESS", collection_address)
        if referral_code:
            set_key(_ENV_FILE, "SR_REFERRAL_CODE", referral_code)

        # Network mode
        set_key(_ENV_FILE, "SR_UPNP_ENABLED", str(upnp_enabled).lower())
        if public_ip:
            set_key(_ENV_FILE, "SR_PUBLIC_IP", public_ip)
        if public_port and public_port != "9090":
            set_key(_ENV_FILE, "SR_PUBLIC_PORT", public_port)

        wizard_done(_ENV_FILE)
        return True

    except (KeyboardInterrupt, EOFError):
        print("\n\nSetup cancelled.")
        return False


def _fetch_min_staking_amount() -> int:
    """Fetch minimum staking amount from coordination API /config endpoint."""
    try:
        import httpx
        s = load_settings()
        resp = httpx.get(f"{s.COORDINATION_API_URL}/config", timeout=5)
        resp.raise_for_status()
        return resp.json().get("minimumStakingAmount", 1)
    except Exception:
        return 1


def _show_staking_prompt() -> None:
    """Display a staking requirement notice before starting the node.

    Only shown when stdin is a TTY (interactive mode). In non-interactive
    mode (piped input, systemd), logs a warning instead.
    """
    min_amount = _fetch_min_staking_amount()

    if not sys.stdin.isatty():
        logger.warning(
            "Staking required for rewards: stake at least %s $SPACE at "
            "https://penguinbase.com/dapp/spacestaking",
            min_amount,
        )
        return

    from rich.panel import Panel
    from rich.console import Console

    console = Console()
    console.print()
    console.print(Panel(
        f"[bold white]To earn $SPACE rewards, you must stake at least\n"
        f"{min_amount} $SPACE before starting your node.[/bold white]\n\n"
        "[cyan]Stake here:[/cyan]    https://penguinbase.com/dapp/spacestaking\n"
        "[cyan]Staking guide:[/cyan] https://docs.spacecoin.org/usdspace-token/staking\n\n"
        "[dim]Press Enter to continue...[/dim]",
        title="[yellow]⚠ Staking Required for Rewards[/yellow]",
        border_style="yellow",
        padding=(1, 2),
    ))
    input()


def _show_version_check() -> None:
    """Check for updates and display a banner if needed (CLI only).

    Performs a synchronous version check against the coordination API.
    Hard update: prints red banner and exits.  Soft update: prints
    yellow banner and continues after Enter.  Fail-safe: errors are
    logged and the node proceeds normally.
    """
    from app.updater import check_version_sync

    s = load_settings()
    result = check_version_sync(s.COORDINATION_API_URL)

    if result.status not in ("soft_update", "hard_update"):
        return

    from rich.panel import Panel
    from rich.console import Console

    console = Console()
    console.print()

    if result.status == "hard_update":
        console.print(Panel(
            f"[bold white]Your version ({result.current_version}) is below the\n"
            f"minimum required version ({result.min_version}).[/bold white]\n\n"
            f"[cyan]Download the latest release:[/cyan]\n{result.download_url}\n\n"
            "[bold red]The node cannot start until you update.[/bold red]",
            title="[red]Update Required[/red]",
            border_style="red",
            padding=(1, 2),
        ))
        sys.exit(1)

    # Soft update
    if sys.stdin.isatty():
        console.print(Panel(
            f"[bold white]A new version ({result.latest_version}) is available.\n"
            f"You are running {result.current_version}.[/bold white]\n\n"
            f"[cyan]Download:[/cyan] {result.download_url}\n\n"
            "[dim]Press Enter to continue...[/dim]",
            title="[yellow]Update Available[/yellow]",
            border_style="yellow",
            padding=(1, 2),
        ))
        input()
    else:
        logger.warning(
            "Update available: current %s, latest %s — download at %s",
            result.current_version, result.latest_version, result.download_url,
        )


# ── Phase functions ──────────────────────────────────────────────────────────

class _NodeContext:
    """Mutable context passed between phases to accumulate state."""

    def __init__(self, settings, http_client) -> None:  # noqa: ANN001
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
        self.version_check = None  # VersionCheckResult | None
        self.receipt_poller = None  # ReceiptPoller | None
        self.claim_reaper = None  # ClaimReaper | None


async def _phase_init(ctx: _NodeContext) -> None:
    """INITIALIZING: UPnP, IP detection, wallet validation, identity, TLS."""
    from app.errors import NodeError, NodeErrorCode
    from app.registration import detect_public_ip
    from app.tls import ensure_certificates, create_server_ssl_context

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
        s.STAKING_ADDRESS = ctx.identity_address   # sync for proxy_handler challenge response
        logger.info("Staking address (identity fallback): %s", ctx.staking_address)

    # 5. TLS certificates
    try:
        ensure_certificates(s.TLS_CERT_PATH, s.TLS_KEY_PATH)
        ctx.ssl_ctx = create_server_ssl_context(s.TLS_CERT_PATH, s.TLS_KEY_PATH)
    except Exception as exc:
        raise NodeError(NodeErrorCode.TLS_CERT_ERROR, str(exc))


async def _phase_bind(ctx: _NodeContext) -> None:
    """BINDING: Start the TLS server."""
    from app.proxy_handler import handle_client

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
    from app.registration import register_node, save_gateway_ca_cert

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

    # Initialise the Leg 2 receipt submitter. Needs node_id + identity key +
    # gateway's payer address (fetched from coord API /config). We do this
    # after registration since node_id isn't known before.
    if ctx.s.PAYMENT_ENABLED and ctx.s.NODE_RATE_PER_GB > 0:
        await _init_receipt_submitter(ctx)

    # Upgrade to mTLS if enabled
    _upgrade_mtls(ctx)


async def _init_receipt_submitter(ctx: _NodeContext) -> None:
    from app.payment.receipt_submitter import (
        ReceiptPoller, ReceiptSubmitter, set_submitter,
    )
    try:
        resp = await ctx.http.get(f"{ctx.s.COORDINATION_API_URL}/config", timeout=10.0)
        resp.raise_for_status()
        gateway_payer = resp.json().get("gatewayPayerAddress") or ""
    except Exception:
        logger.warning("Failed to fetch /config for Leg 2 payer address — Leg 2 disabled", exc_info=True)
        return
    if not gateway_payer:
        logger.info("Coord API reports no gatewayPayerAddress — Leg 2 disabled")
        return

    # Single source of truth: COLLECTION_ADDRESS is what the contract pays,
    # and it's what coord API stores as the node's wallet. Falling back to
    # STAKING_ADDRESS for legacy configs where COLLECTION_ADDRESS wasn't set.
    # NODE_IDENTITY_ADDRESS is ignored for receipts — if operator configured
    # it distinct from the collection wallet, we warn.
    node_wallet = ctx.s.COLLECTION_ADDRESS or ctx.s.STAKING_ADDRESS
    if not node_wallet:
        logger.info("No provider wallet address configured — Leg 2 disabled")
        return

    nia = (ctx.s.NODE_IDENTITY_ADDRESS or "").strip()
    if nia and nia.lower() != node_wallet.lower():
        logger.warning(
            "SR_NODE_IDENTITY_ADDRESS=%s is set but differs from COLLECTION_ADDRESS=%s; "
            "Leg 2 receipts pay COLLECTION_ADDRESS. Remove NODE_IDENTITY_ADDRESS or match it.",
            nia, node_wallet,
        )

    submitter = ReceiptSubmitter(
        settings=ctx.s,
        node_id=ctx.node_id,
        identity_key=ctx.identity_key,
        identity_address=ctx.identity_address,
        gateway_payer_address=gateway_payer,
        node_wallet_address=node_wallet,
    )
    set_submitter(submitter)

    poller = ReceiptPoller(
        settings=ctx.s,
        node_id=ctx.node_id,
        identity_key=ctx.identity_key,
        node_wallet_address=node_wallet,
    )
    await poller.start()
    ctx.receipt_poller = poller

    # Reaper resolves stuck CLAIM_TX_TIMEOUT rows by re-querying the chain.
    # Only runs when escrow RPC + contract are configured — safe on dev
    # setups that don't have on-chain settlement enabled.
    from app.payment.reaper import ClaimReaper
    reaper = ClaimReaper(settings=ctx.s)
    if reaper.enabled:
        await reaper.start()
        ctx.claim_reaper = reaper

    logger.info(
        "Leg 2 submitter ready — payer=%s node_wallet=%s rate=%d/GB "
        "(poller every 10s, reaper enabled=%s)",
        gateway_payer, node_wallet[:12] + "...",
        ctx.s.NODE_RATE_PER_GB, reaper.enabled,
    )

    # Sanity checks for Leg 2 config — ERROR-log only, never fail
    # startup. The node is still useful for routing even if Leg 2 is
    # misconfigured; we want to surface the root cause instead of
    # accumulating silent failures in the receipt store.
    await _verify_escrow_config(ctx.s, node_wallet)


async def _verify_escrow_config(settings, node_wallet: str) -> None:
    """Run cheap sanity checks against the configured escrow chain.

    Three checks (each logs ERROR + continues):

    - **S8**: Does the RPC actually point at the chain_id we expect?
      Misconfigured prod-vs-test RPCs silently broadcast claim txs to
      the wrong chain otherwise.
    - **P1**: Is the node wallet registered via ``registerNode()``?
      Without registration, every Leg 2 claim silently skips on-chain.
    - **P2**: Does ``SR_COLLECTION_ADDRESS`` match the ``node_address``
      in existing unclaimed signed receipts? Changing the config after
      receipts accumulate orphans them.
    - **P9**: Warn if ``NODE_RATE_PER_GB`` is zero.
    """
    if settings.NODE_RATE_PER_GB <= 0:
        logger.warning(
            "SR_NODE_RATE_PER_GB=%d — all Leg 2 receipts will be zero-value "
            "and skipped. Set a non-zero rate to earn payouts.",
            settings.NODE_RATE_PER_GB,
        )

    if not settings.ESCROW_CHAIN_RPC or not settings.ESCROW_CONTRACT_ADDRESS:
        return  # Escrow disabled; nothing to verify.

    def _sync_check() -> dict:
        out: dict = {"chain_id": None, "registered": None, "error": None}
        try:
            from web3 import Web3
            from eth_utils import to_bytes, to_checksum_address
            import json as _json
            from pathlib import Path as _Path

            w3 = Web3(Web3.HTTPProvider(
                settings.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 10},
            ))
            if not w3.is_connected():
                out["error"] = f"RPC unreachable: {settings.ESCROW_CHAIN_RPC}"
                return out

            out["chain_id"] = int(w3.eth.chain_id)

            abi_path = _Path(__file__).parent / "payment" / "escrow_abi.json"
            with open(abi_path) as f:
                abi_data = _json.load(f)
            abi = abi_data["escrow"] if isinstance(abi_data, dict) else abi_data

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(settings.ESCROW_CONTRACT_ADDRESS),
                abi=abi,
            )
            node_b32 = to_bytes(hexstr="0x" + node_wallet.lower().removeprefix("0x").zfill(64))
            try:
                mapped = contract.functions.getNodeWallet(node_b32).call()
                out["registered"] = (
                    mapped != "0x0000000000000000000000000000000000000000"
                )
            except Exception as e:
                # Older contract revisions may not have getNodeWallet;
                # don't fail the check. Default to "unknown".
                out["registered"] = None
                logger.debug("getNodeWallet call failed: %s", e)
        except Exception as e:
            out["error"] = str(e)
        return out

    info = await asyncio.to_thread(_sync_check)

    if info.get("error"):
        logger.error(
            "Escrow config check: RPC/ABI error — %s. Leg 2 claims will "
            "likely fail until this is fixed.",
            info["error"],
        )
        return

    # S8: chain_id mismatch guard
    expected_chain = getattr(settings, "ESCROW_CHAIN_ID", 0)
    actual_chain = info.get("chain_id")
    if expected_chain and actual_chain and expected_chain != actual_chain:
        logger.error(
            "ESCROW CHAIN ID MISMATCH: SR_ESCROW_CHAIN_ID=%d but "
            "SR_ESCROW_CHAIN_RPC reports chain_id=%d. Your claim "
            "transactions will be rejected or go to the wrong chain. "
            "Fix the config before running --claim.",
            expected_chain, actual_chain,
        )

    # P1: registerNode guard
    if info.get("registered") is False:
        logger.error(
            "NODE NOT REGISTERED in escrow contract: node_wallet=%s on "
            "chain_id=%s. Payouts will silently fail until engineering "
            "calls registerNode(). Receipts will still sign but "
            "--claim will not transfer tokens.",
            node_wallet, actual_chain,
        )

    # P2: collection-address-changed-mid-lifetime guard
    try:
        from app.payment.receipt_store import get_store
        store = get_store(settings.RECEIPT_STORE_PATH)
        await store.initialize()
        # Query directly via a helper that returns distinct node_address
        # values across unclaimed rows.
        import sqlite3 as _sqlite3
        def _do():
            with _sqlite3.connect(store.path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT node_address FROM signed_receipts "
                    "WHERE claimed_at IS NULL AND locked = 0"
                ).fetchall()
            return [r[0] for r in rows]
        existing_addrs = await asyncio.to_thread(_do)
        expected_b32 = "0x" + node_wallet.lower().removeprefix("0x").zfill(64)
        orphans = [a for a in existing_addrs if a.lower() != expected_b32]
        if orphans:
            logger.warning(
                "COLLECTION ADDRESS CHANGED: %d unclaimed receipt(s) "
                "reference a different node_address than the current "
                "SR_COLLECTION_ADDRESS=%s. They will pay out to the "
                "previous collection wallet if that wallet is still "
                "registered. Run --receipts --json to inspect.",
                len(orphans), node_wallet,
            )
    except Exception:
        logger.debug("Collection-address drift check failed", exc_info=True)


def _upgrade_mtls(ctx: _NodeContext) -> None:
    """Attempt mTLS upgrade (non-fatal on failure)."""
    from app.tls import create_mtls_server_ssl_context

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
    from app.proxy_handler import handle_client

    s = ctx.s
    if ctx.server:
        ctx.server.close()
        await ctx.server.wait_closed()
    handler = functools.partial(handle_client, settings=s)
    ctx.server = await asyncio.start_server(
        handler, host=s.BIND_ADDRESS, port=s.NODE_PORT, ssl=ctx.ssl_ctx,
        reuse_address=True,
    )


async def _version_check_loop(
    ctx: _NodeContext,
    stop_event: asyncio.Event,
    on_version_check=None,  # noqa: ANN001
) -> None:
    """Periodic version check every 6 hours (fail-safe).

    Updates ``ctx.version_check`` so the GUI can poll the result.
    Never raises — all errors are swallowed and logged at debug level.
    """
    from app.updater import check_version, VERSION_CHECK_INTERVAL

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=VERSION_CHECK_INTERVAL,
            )
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed — run the check

        try:
            result = await check_version(ctx.http, ctx.s.COORDINATION_API_URL)
            ctx.version_check = result
            if on_version_check:
                on_version_check(result)
            if result.status == "hard_update":
                logger.warning(
                    "Scheduled version check: update required (current=%s, min=%s)",
                    result.current_version, result.min_version,
                )
            elif result.status == "soft_update":
                logger.info(
                    "Scheduled version check: update available (current=%s, latest=%s)",
                    result.current_version, result.latest_version,
                )
            else:
                logger.debug("Scheduled version check: %s", result.status)
        except Exception:
            logger.debug("Scheduled version check failed", exc_info=True)


async def _health_loop(
    ctx: _NodeContext,
    sm: NodeStateMachine,
    stop_event: asyncio.Event,
) -> None:
    """Periodic health checks while RUNNING."""
    from app.node_logging import activity  # noqa: E402
    from app.registration import check_node_status, request_probe
    from app.tls import (
        check_certificate_expiry, ensure_certificates, create_server_ssl_context,
    )

    consecutive_failures = 0
    last_cert_check = 0.0

    import time
    # Start at current time so the first 30-min probe waits a full interval
    # (registration already requested a probe during _phase_register).
    last_probe_request = time.time()
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
            node_data = await check_node_status(
                ctx.http, ctx.s, ctx.node_id, identity_key=ctx.identity_key,
            )
            status = node_data.get("status", "unknown")
            activity.record_health_check(status)
            if status in ("online", "active"):
                consecutive_failures = 0
                logger.debug("Health check OK: status=%s", status)
            else:
                logger.warning("Health check: node status is '%s'", status)
                consecutive_failures += 1
        except Exception as exc:
            consecutive_failures += 1
            activity.record_health_check("error")
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

        # Periodic probe request (every 30 min, non-critical).
        # Skip if _self_probe_loop recently requested one.
        now = time.time()
        last_global = getattr(ctx, "_last_probe_request_time", 0)
        if (now - last_probe_request >= _PROBE_REQUEST_INTERVAL
                and now - last_global >= _SELF_PROBE_REQUEST_COOLDOWN):
            last_probe_request = now
            try:
                accepted = await request_probe(
                    ctx.http, ctx.s, ctx.node_id,
                    identity_key=ctx.identity_key,
                )
                if accepted:
                    ctx._last_probe_request_time = now
            except Exception:
                pass  # non-critical


async def _status_summary_loop(
    ctx: "_NodeContext",
    stop_event: asyncio.Event,
    interval: float,
) -> None:
    """Periodically log a node status summary (non-dashboard mode)."""
    from app.node_logging import activity  # noqa: E402

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass

        logger.info(
            "--- Status [%s]: uptime=%s | connections=%d (active=%d) | "
            "health_checks=%d (failures=%d) | reconnects=%d ---",
            ctx.node_id[:12] if ctx.node_id else "unregistered",
            activity.uptime_str,
            activity.connections_served,
            activity.connections_active,
            activity.health_check_count,
            activity.health_check_failures,
            activity.reconnect_count,
        )


# Self-probe interval — more frequent than health checks to catch bore disconnects fast
_SELF_PROBE_INTERVAL = 60  # 1 minute
_SELF_PROBE_REQUEST_COOLDOWN = 300  # 5 min — matches server rate limit
_SELF_PROBE_BACKOFF_CAP = 1800  # 30 min max backoff on consecutive 429s


async def _self_probe_loop(
    ctx: "_NodeContext",
    sm: NodeStateMachine,
    stop_event: asyncio.Event,
    dashboard=None,  # noqa: ANN001
) -> None:
    """Periodically check node status from coordination's perspective.

    Runs every 60s (vs 5min for health checks) to catch bore tunnel
    disconnects and other reachability issues quickly.  Also feeds
    staking_status, health_score, and probe results to the dashboard.
    """
    import time as _time

    from app.registration import check_node_status, request_probe

    # Run first check almost immediately (5s delay for registration to settle)
    first_run = True
    # Start at current time so first cooldown respects the registration probe
    # (which already fired during _phase_register).
    last_probe_request_time = _time.time()
    current_cooldown = _SELF_PROBE_REQUEST_COOLDOWN
    while not stop_event.is_set():
        delay = 5 if first_run else _SELF_PROBE_INTERVAL
        first_run = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            pass

        if not ctx.node_id:
            continue

        try:
            node_data = await check_node_status(
                ctx.http, ctx.s, ctx.node_id, identity_key=ctx.identity_key,
            )
            status = node_data.get("status", "unknown")
            health_score = node_data.get("health_score", 0)
            staking_status = node_data.get("staking_status", "—")

            probe_result = status
            if status not in ("online", "active"):
                logger.warning(
                    "Self-probe: coordination reports status='%s' health_score=%.1f — requesting probe",
                    status, health_score,
                )
                now = _time.time()
                if now - last_probe_request_time >= current_cooldown:
                    try:
                        accepted = await request_probe(
                            ctx.http, ctx.s, ctx.node_id,
                            identity_key=ctx.identity_key,
                        )
                        if accepted:
                            last_probe_request_time = now
                            probe_result = "probe_requested"
                            current_cooldown = _SELF_PROBE_REQUEST_COOLDOWN
                            ctx._last_probe_request_time = now
                        else:
                            # 429 or server error — exponential backoff
                            probe_result = "rate_limited"
                            current_cooldown = min(
                                current_cooldown * 2,
                                _SELF_PROBE_BACKOFF_CAP,
                            )
                            logger.info(
                                "Probe request not accepted — backing off to %ds",
                                current_cooldown,
                            )
                    except Exception:
                        probe_result = "probe_failed"
                else:
                    probe_result = "cooldown"

            # Update state machine so GUI can read staking_status
            sm.status.staking_status = staking_status

            if dashboard:
                dashboard.update(
                    last_probe_result=probe_result,
                    last_probe_time=_time.time(),
                    health_status=status,
                    health_score=str(health_score),
                    staking_status=staking_status,
                )
        except Exception as exc:
            logger.debug("Self-probe check failed: %s", exc)
            if dashboard:
                dashboard.update(
                    last_probe_result="error",
                    last_probe_time=_time.time(),
                )


async def _dashboard_loop(
    ctx: "_NodeContext",
    sm: NodeStateMachine,
    stop_event: asyncio.Event,
    dashboard,  # noqa: ANN001
) -> None:
    """Update the live CLI dashboard every second."""
    from app.node_logging import activity

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            break
        except asyncio.TimeoutError:
            pass

        dashboard.update(
            state=sm.state.value,
            node_id=ctx.node_id,
            connections_served=activity.connections_served,
            connections_active=activity.connections_active,
            last_health_check=activity.last_health_check or 0,
            health_status=activity.last_health_status or "—",
        )


# ── Orchestrator ─────────────────────────────────────────────────────────────

def _check_disk_space(settings) -> None:
    """Warn / ERROR when the receipt-store filesystem is filling up.

    SQLite writes silently fail at ENOSPC. Daemon continues running but
    drops every receipt it tries to persist. Surfacing this at startup
    (and after each tick would be nice but is too noisy) lets operators
    act before data is lost.
    """
    import shutil
    from pathlib import Path

    store_path = Path(settings.RECEIPT_STORE_PATH).expanduser()
    target = store_path.parent if store_path.parent.exists() else Path.home()
    try:
        stat = shutil.disk_usage(target)
    except OSError as exc:
        logger.debug("disk_usage(%s) failed: %s", target, exc)
        return

    free_mb = stat.free / (1024 * 1024)
    total_mb = stat.total / (1024 * 1024)
    pct_free = (stat.free / stat.total * 100) if stat.total else 0.0

    if free_mb < 50:
        logger.error(
            "Receipt-store disk almost full: %.1f MB free of %.0f MB "
            "(%.1f%%) on %s. SQLite writes will start failing — clear "
            "space immediately or receipts will be lost silently.",
            free_mb, total_mb, pct_free, target,
        )
    elif free_mb < 500 or pct_free < 5:
        logger.warning(
            "Receipt-store disk low: %.1f MB free (%.1f%%) on %s",
            free_mb, pct_free, target,
        )


# Module-level references to the open lock files. Keeping the objects
# alive for the whole process lifetime is what keeps the flock held —
# the moment Python GCs the file object, the fd is closed and the
# kernel drops the lock. Keyed by lock path so repeated calls in tests
# can inspect state.
_daemon_lock_handles: dict[str, "object"] = {}


def _acquire_daemon_lock(settings) -> int:
    """Acquire an exclusive lock on the daemon lock file.

    Cross-platform:
    - POSIX (Linux / macOS): ``fcntl.flock`` — kernel-backed advisory lock
      that releases on process exit regardless of how the process dies.
    - Windows: ``msvcrt.locking`` on the first byte of the file. Same
      process-lifetime semantics; the kernel releases the lock when the
      handle is closed.

    Keyed on the receipts-store directory so dev setups with separate
    DBs can run multiple daemons, but a double-start on the same store
    refuses rather than silently corrupting the receipt lifecycle.

    On conflict, exits with a clear message. The lock file handle is
    stashed in a module-level dict so the OS keeps the lock held for
    the process's lifetime (see PR #50 post-mortem — early impl lost
    the fd to GC and the lock evaporated).
    """
    from pathlib import Path

    store_path = Path(settings.RECEIPT_STORE_PATH).expanduser()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.parent / "daemon.lock"
    key = str(lock_path)

    if key in _daemon_lock_handles:
        return _daemon_lock_handles[key].fileno()

    is_windows = sys.platform == "win32"

    try:
        # Windows needs the file to exist before msvcrt.locking can set
        # a lock range on it, and "w" truncates — use "a+" to ensure the
        # file exists without nuking an existing pid line.
        fd = open(lock_path, "a+" if is_windows else "w")
    except OSError as exc:
        logger.error(
            "Cannot open daemon lock file %s: %s — continuing without "
            "single-instance protection.",
            lock_path, exc,
        )
        return -1

    # Short retry loop: on Windows, when a prior daemon was hard-killed
    # (Stop-Process -Force / TerminateProcess) the OS takes a moment to
    # fully release the msvcrt lock on the file handle. Without retries,
    # a quick restart after a crash hits "another instance" even though
    # nothing is running. The real "two daemons running" case still gets
    # caught — that lock stays held indefinitely.
    import time as _time
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            if is_windows:
                import msvcrt
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            last_err = None
            break
        except (BlockingIOError, OSError) as exc:
            last_err = exc
            if attempt < 3:
                _time.sleep(0.5)

    if last_err is not None:
        fd.close()
        print(
            f"Another space-router-node daemon is already running against "
            f"{store_path}. Refusing to start to avoid receipt corruption. "
            f"Lock: {lock_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Write our PID for diagnostic purposes. Lock itself is the source
    # of truth, but `ps`-side tooling benefits from having a PID in the
    # file. On Windows we reserved the first byte with msvcrt.locking;
    # append the PID after that so it doesn't overwrite the locked byte.
    try:
        if is_windows:
            fd.seek(0, 2)  # end-of-file
            fd.write(f"\n{os.getpid()}\n")
        else:
            fd.seek(0)
            fd.truncate()
            fd.write(f"{os.getpid()}\n")
        fd.flush()
    except Exception:
        pass

    _daemon_lock_handles[key] = fd
    logger.info("Acquired daemon lock at %s", lock_path)
    return fd.fileno()


async def _run(
    settings_override=None,  # noqa: ANN001
    stop_event: asyncio.Event | None = None,
    on_phase=None,  # noqa: ANN001
    state_machine: NodeStateMachine | None = None,
    on_version_check=None,  # noqa: ANN001  # callback(VersionCheckResult)
) -> None:
    """Main orchestrator loop. Drives phases and handles retries."""
    # Deferred heavy imports — keep CLI startup fast
    import httpx  # noqa: E402
    from app.errors import NodeError, NodeErrorCode, classify_error  # noqa: E402
    from app.node_logging import activity, setup_cli_logging  # noqa: E402
    from app.node_logging import _STATUS_INTERVAL  # noqa: E402
    from app.proxy_handler import handle_client  # noqa: E402
    from app.registration import (  # noqa: E402
        check_node_status, deregister_node, detect_public_ip,
        register_node, request_probe, save_gateway_ca_cert,
    )
    from app.tls import (  # noqa: E402
        check_certificate_expiry, create_mtls_server_ssl_context,
        create_server_ssl_context, ensure_certificates,
    )

    s = settings_override or load_settings()

    # Configure logging from settings (updates both logger and handler levels)
    setup_cli_logging(s.LOG_LEVEL)

    # Single-instance daemon lock — keyed on the receipts DB path so two
    # daemons pointing at different stores can run (dev use case), but a
    # double-start on the same store refuses immediately instead of
    # corrupting the receipt state. Released on process exit (OS-level).
    _daemon_lock_fd = _acquire_daemon_lock(s)

    # Pre-flight: warn if the receipt-store filesystem is almost full.
    # SQLite writes silently fail at ENOSPC; better to flag it at startup
    # than accumulate receipt loss over days.
    _check_disk_space(s)

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
        status_task = None
        probe_task = None
        dashboard = None

        version_check_task = None

        try:
            # ── Pre-flight: Version check ──
            from app.updater import check_version

            version_result = await check_version(http_client, s.COORDINATION_API_URL)
            ctx.version_check = version_result
            if on_version_check:
                on_version_check(version_result)

            if version_result.status == "hard_update":
                logger.warning(
                    "Version check: update required — current %s below minimum %s",
                    version_result.current_version,
                    version_result.min_version,
                )
                # In standalone CLI mode, abort immediately.
                # In GUI mode (state_machine provided), store result and let
                # registration's HTTP 426 act as the enforcement backstop.
                if not state_machine:
                    raise NodeError(
                        NodeErrorCode.VERSION_TOO_OLD,
                        f"Current version {version_result.current_version} is below "
                        f"minimum required {version_result.min_version}. "
                        f"Download the latest release: {version_result.download_url}",
                    )
            elif version_result.status == "soft_update":
                logger.info(
                    "Version check: update available — current %s, latest %s",
                    version_result.current_version,
                    version_result.latest_version,
                )
            else:
                logger.debug("Version check: %s", version_result.status)

            if stop_event.is_set():
                return

            # ── Phase: INITIALIZING ──
            _report(NodeState.INITIALIZING, "Loading identity and certificates")
            logger.info("Initializing node (version %s)...", __version__)
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

            # Export identity info for GUI error reporting (read-only env vars)
            os.environ["_SR_IDENTITY_KEY"] = ctx.identity_key
            os.environ["_SR_IDENTITY_ADDRESS"] = ctx.identity_address

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
            logger.info("Registering with %s ...", s.COORDINATION_API_URL)
            try:
                await _phase_register(ctx)
            except NodeError:
                raise
            except Exception as exc:
                raise classify_error(exc)

            logger.info("Registration successful  node_id=%s", ctx.node_id[:16])
            activity.last_registration_time = asyncio.get_event_loop().time()

            # mTLS rebind if upgrade happened
            if ctx.s.MTLS_ENABLED and os.path.isfile(ctx.s.GATEWAY_CA_CERT_PATH):
                try:
                    await _rebind_server_mtls(ctx)
                    logger.info("mTLS active -- gateway authentication enabled")
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

            # Live dashboard for CLI standalone mode
            dashboard = None
            dashboard_task = None
            probe_task = None
            if own_stop_event and sys.stdin.isatty():
                try:
                    from app.cli_ui import StatusDashboard
                    dashboard = StatusDashboard()
                    dashboard.update(
                        node_id=ctx.node_id,
                        state="running",
                        staking_address=ctx.staking_address,
                        public_ip=ctx.public_ip,
                        port=s.PUBLIC_PORT or s.NODE_PORT,
                        upnp=bool(ctx.upnp_endpoint),
                        version=__version__,
                    )
                    dashboard.start()
                except Exception:
                    dashboard = None
                    logger.info(
                        "--- Node is RUNNING --- "
                        "Listening on port %d | IP %s | Ctrl+C to stop",
                        s.NODE_PORT, ctx.public_ip,
                    )
            else:
                logger.info(
                    "--- Node is RUNNING --- "
                    "Listening on port %d | IP %s | Ctrl+C to stop",
                    s.NODE_PORT, ctx.public_ip,
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

            # Start periodic version check (every 6 hours)
            version_check_task = asyncio.create_task(
                _version_check_loop(ctx, stop_event, on_version_check)
            )

            # Start periodic status summary (text mode) or dashboard (rich mode)
            if dashboard:
                status_task = asyncio.create_task(
                    _dashboard_loop(ctx, sm, stop_event, dashboard)
                )
            else:
                status_task = asyncio.create_task(
                    _status_summary_loop(ctx, stop_event, _STATUS_INTERVAL)
                )

            # Self-probe loop — checks reachability from coordination's perspective
            probe_task = asyncio.create_task(
                _self_probe_loop(ctx, sm, stop_event, dashboard)
            )

            # Wait for stop or health loop exit (reconnection trigger)
            done, pending = await asyncio.wait(
                [asyncio.create_task(stop_event.wait()), health_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            # If health loop exited (RECONNECTING), handle reconnection
            if sm.state == NodeState.RECONNECTING:
                logger.warning("Connection lost -- attempting reconnection...")
                activity.record_reconnect()

                # Cancel ALL background tasks during reconnection
                for _tname, _task in [
                    ("health", health_task), ("probe", probe_task),
                    ("status", status_task), ("renewal", renewal_task),
                    ("version_check", version_check_task),
                ]:
                    if _task is not None and not _task.done():
                        _task.cancel()
                        try:
                            await _task
                        except asyncio.CancelledError:
                            pass

                from app.registration import check_node_status

                # Retry registration while server stays up
                while not stop_event.is_set() and sm.state == NodeState.RECONNECTING:
                    try:
                        # Check if the coordination API already considers
                        # us healthy (e.g. transient network blip resolved).
                        skip_registration = False
                        if ctx.node_id:
                            try:
                                node_data = await check_node_status(
                                    ctx.http, ctx.s, ctx.node_id,
                                    identity_key=ctx.identity_key,
                                )
                                api_status = node_data.get("status", "unknown")
                                api_health = node_data.get("health_score", 0)
                                if api_status in ("online", "active") and api_health >= 0.5:
                                    logger.info(
                                        "Node already healthy on coordination API "
                                        "(status=%s, health=%.1f) — skipping re-registration",
                                        api_status, api_health,
                                    )
                                    skip_registration = True
                            except Exception:
                                pass  # fall through to re-registration

                        if not skip_registration:
                            # Retry UPnP if it failed at startup
                            if ctx.s.UPNP_ENABLED and ctx.upnp_endpoint is None:
                                from app.upnp import setup_upnp_mapping
                                upnp_result = await setup_upnp_mapping(
                                    ctx.s.NODE_PORT,
                                    lease_duration=ctx.s.UPNP_LEASE_DURATION,
                                )
                                if upnp_result:
                                    ctx.upnp_endpoint = upnp_result
                                    logger.info(
                                        "UPnP mapping recovered: %s:%d",
                                        upnp_result[0], upnp_result[1],
                                    )

                            await _phase_register(ctx)
                            sm.set_node_id(ctx.node_id)
                            if ctx.s.MTLS_ENABLED and os.path.isfile(ctx.s.GATEWAY_CA_CERT_PATH):
                                try:
                                    await _rebind_server_mtls(ctx)
                                except Exception:
                                    logger.warning("mTLS server rebind failed", exc_info=True)

                        _report(NodeState.RUNNING, f"Reconnected (Node ID: {ctx.node_id[:12]}...)")
                        logger.info("Reconnected successfully")

                        # Recreate background tasks
                        if ctx.upnp_endpoint and s.UPNP_LEASE_DURATION > 0:
                            from app.upnp import renew_upnp_mapping

                            async def _renew_loop() -> None:
                                interval = max(s.UPNP_LEASE_DURATION // 2, 60)
                                while True:
                                    await asyncio.sleep(interval)
                                    ok = await renew_upnp_mapping(
                                        s.NODE_PORT, ctx.upnp_endpoint[1],
                                        s.UPNP_LEASE_DURATION,
                                    )
                                    if ok:
                                        logger.debug("UPnP lease renewed")
                                    else:
                                        logger.warning("UPnP lease renewal failed")

                            renewal_task = asyncio.create_task(_renew_loop())
                        else:
                            renewal_task = None

                        if dashboard:
                            status_task = asyncio.create_task(
                                _dashboard_loop(ctx, sm, stop_event, dashboard)
                            )
                        else:
                            status_task = asyncio.create_task(
                                _status_summary_loop(ctx, stop_event, _STATUS_INTERVAL)
                            )
                        probe_task = asyncio.create_task(
                            _self_probe_loop(ctx, sm, stop_event, dashboard)
                        )
                        version_check_task = asyncio.create_task(
                            _version_check_loop(ctx, stop_event, on_version_check)
                        )

                        # Restart health loop
                        health_task = asyncio.create_task(_health_loop(ctx, sm, stop_event))
                        done, pending = await asyncio.wait(
                            [asyncio.create_task(stop_event.wait()), health_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()

                        # If health loop exits again, cancel tasks for next
                        # reconnection attempt.
                        if sm.state == NodeState.RECONNECTING:
                            for _tname, _task in [
                                ("probe", probe_task), ("status", status_task),
                                ("renewal", renewal_task),
                                ("version_check", version_check_task),
                            ]:
                                if _task is not None and not _task.done():
                                    _task.cancel()
                                    try:
                                        await _task
                                    except asyncio.CancelledError:
                                        pass

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
            # Stop dashboard first so shutdown logs are visible
            if dashboard:
                dashboard.stop()

            logger.info("Shutting down…")

            # Stop accepting new connections
            if ctx.server:
                ctx.server.close()
                await ctx.server.wait_closed()

            # Cancel background tasks
            for task in (renewal_task, health_task, status_task, probe_task, version_check_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Stop Leg 2 receipt poller
            if ctx.receipt_poller is not None:
                try:
                    await ctx.receipt_poller.stop()
                except Exception:
                    logger.debug("Receipt poller stop errored", exc_info=True)

            # Stop claim reaper
            if ctx.claim_reaper is not None:
                try:
                    await ctx.claim_reaper.stop()
                except Exception:
                    logger.debug("Claim reaper stop errored", exc_info=True)

            # Remove UPnP mapping
            if ctx.upnp_endpoint:
                from app.upnp import remove_upnp_mapping
                await remove_upnp_mapping(ctx.upnp_endpoint[1])

            # Deregister (best-effort)
            if ctx.node_id:
                await deregister_node(ctx.http, s, ctx.node_id, identity_key=ctx.identity_key)

    logger.info("Home Node shut down cleanly")


def _do_reset() -> bool:
    """Delete all config, identity key, and certificates.

    Returns True if reset was performed, False if cancelled.
    """
    from app.paths import config_dir

    s = load_settings()

    # Check both well-known config dir and CWD for config files
    cfg_dir = config_dir()
    wellknown_env = cfg_dir / "spacerouter.env"
    cwd_env = os.path.abspath(".env")

    env_file = str(wellknown_env) if wellknown_env.is_file() else cwd_env
    certs_dir = os.path.dirname(os.path.abspath(s.IDENTITY_KEY_PATH)) or "certs"

    if sys.stdin.isatty():
        print("WARNING: This will delete your identity key and all configuration.")
        confirm = input("Type YES to confirm: ").strip()
        if confirm != "YES":
            print("Reset cancelled.")
            return False

    # Delete .env
    if os.path.isfile(env_file):
        os.remove(env_file)
        print(f"Removed {env_file}")

    # Delete certs directory (identity key + all certificates)
    if os.path.isdir(certs_dir):
        import shutil
        shutil.rmtree(certs_dir)
        print(f"Removed {certs_dir}/")

    print("Reset complete.\n")
    return True


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="space-router-node",
        description="SpaceRouter Home Node — proxy node daemon",
    )
    parser.add_argument(
        "--version", "-V", action="version",
        version=f"space-router-node {__version__}",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Clear all config and re-run onboarding wizard",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="Re-run onboarding wizard (without clearing)",
    )

    # Network settings
    net = parser.add_argument_group("network")
    net.add_argument(
        "--port", "-p", type=int, metavar="PORT",
        help="Node listen port (default: 9090)",
    )
    net.add_argument(
        "--public-url", metavar="HOST",
        help="Public hostname or IP (tunnel mode)",
    )
    net.add_argument(
        "--public-port", type=int, metavar="PORT",
        help="Advertised public port (tunnel mode)",
    )
    net.add_argument(
        "--no-upnp", action="store_true",
        help="Disable UPnP automatic port forwarding",
    )

    # Identity / wallet settings
    wallet = parser.add_argument_group("wallet")
    wallet.add_argument(
        "--staking-address", metavar="ADDR",
        help="Staking wallet address",
    )
    wallet.add_argument(
        "--collection-address", metavar="ADDR",
        help="Collection wallet address",
    )
    wallet.add_argument(
        "--password-file", metavar="PATH",
        help="Read identity passphrase from file",
    )

    # Misc
    parser.add_argument(
        "--log-level", metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--label", metavar="NAME",
        help="Human-readable node label",
    )

    # Leg 2 settlement commands — run instead of starting the node.
    claim_group = parser.add_argument_group("payment settlement")
    claim_group.add_argument(
        "--receipts", action="store_true",
        help="List outstanding Leg 2 receipts and exit. Adds --failed to "
             "show failed/retryable/locked rows with their reason; --json "
             "for machine-readable output; --reap to run the claim reaper.",
    )
    claim_group.add_argument(
        "--failed", action="store_true",
        help="With --receipts: show only rows in failed_retryable or "
             "failed_terminal state, including the full error reason.",
    )
    claim_group.add_argument(
        "--json", action="store_true", dest="output_json",
        help="With --receipts: emit a stable JSON payload instead of the "
             "rich table. Schema documented in docs/cli-receipts.md.",
    )
    claim_group.add_argument(
        "--reap", action="store_true",
        help="With --receipts: run one claim-reaper tick before the "
             "listing so stuck CLAIM_TX_TIMEOUT rows are resolved.",
    )
    claim_group.add_argument(
        "--claim", action="store_true",
        help="Submit all claimable Leg 2 receipts on-chain via claimBatch() "
             "and exit. Combine with --include-retryable to also settle "
             "rows that previously reverted but are still under the "
             "attempt cap, or --uuid to settle a single receipt.",
    )
    claim_group.add_argument(
        "--include-retryable", action="store_true",
        help="With --claim: also submit rows in failed_retryable state. "
             "Default off so scheduled cron runs don't snowball into "
             "retry storms on terminally broken receipts.",
    )
    claim_group.add_argument(
        "--uuid", metavar="UUID",
        help="With --claim: settle only the receipt with this UUID. "
             "Refuses if the row is locked (failed_terminal).",
    )

    return parser


def _apply_cli_args(args: argparse.Namespace) -> None:
    """Override environment variables from CLI arguments.

    CLI args take precedence over .env values. We set os.environ so that
    pydantic-settings picks them up when load_settings() is called.
    """
    if args.port is not None:
        os.environ["SR_NODE_PORT"] = str(args.port)
    if args.public_url is not None:
        os.environ["SR_PUBLIC_IP"] = args.public_url
    if args.public_port is not None:
        os.environ["SR_PUBLIC_PORT"] = str(args.public_port)
    if args.no_upnp:
        os.environ["SR_UPNP_ENABLED"] = "false"
    if args.staking_address is not None:
        os.environ["SR_STAKING_ADDRESS"] = args.staking_address
    if args.collection_address is not None:
        os.environ["SR_COLLECTION_ADDRESS"] = args.collection_address
    if args.log_level is not None:
        os.environ["SR_LOG_LEVEL"] = args.log_level
    if args.label is not None:
        os.environ["SR_NODE_LABEL"] = args.label
    if args.password_file is not None:
        try:
            with open(args.password_file) as f:
                os.environ["SR_IDENTITY_PASSPHRASE"] = f.readline().rstrip("\n")
        except (OSError, IOError) as exc:
            print(f"Error reading password file: {exc}", file=sys.stderr)
            sys.exit(1)


def _prompt_error_report(error, settings_override=None) -> None:  # noqa: ANN001
    """Prompt the user to send an opt-in error report (CLI only)."""
    from app.error_report import is_reportable, build_error_report, send_error_report_sync

    if not is_reportable(error.code.value):
        return
    if not sys.stdin.isatty():
        return

    try:
        answer = input("\nSend error report to help us investigate? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer not in ("", "y", "yes"):
        return

    # Best-effort: load identity + settings to sign and build the report
    try:
        s = settings_override or load_settings()
        identity_key = ""
        identity_address = ""
        try:
            identity_key, identity_address = load_or_create_identity(
                s.IDENTITY_KEY_PATH, s.IDENTITY_PASSPHRASE,
            )
        except Exception:
            pass

        report = build_error_report(
            error,
            node_id=None,
            identity_address=identity_address or None,
            staking_address=s.STAKING_ADDRESS or None,
            collection_address=s.COLLECTION_ADDRESS or None,
            settings=s,
            app_type="cli",
            state_snapshot=None,
        )

        if identity_key and identity_address:
            ok = send_error_report_sync(
                report, identity_key, identity_address, s.COORDINATION_API_URL,
            )
            if ok:
                print("  Report sent. Thank you!")
            else:
                print("  Failed to send report.")
        else:
            print("  Cannot send report — identity key unavailable.")
    except Exception:
        print("  Failed to send report.")


def _run_node(settings_override=None) -> None:  # noqa: ANN001
    """Run the node with proper error handling and signal cleanup."""
    from app.errors import NodeError

    try:
        asyncio.run(_run(settings_override=settings_override))
    except KeystorePassphraseRequired:
        if sys.stdin.isatty():
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
                _prompt_error_report(exc, settings_override=load_settings())
                sys.exit(1)
        else:
            print(
                "Identity key is encrypted. Set SR_IDENTITY_PASSPHRASE "
                "environment variable or run interactively.",
                file=sys.stderr,
            )
            sys.exit(1)
    except NodeError as exc:
        logger.error("Node failed: %s", exc.user_message)
        _prompt_error_report(exc, settings_override=settings_override)
        sys.exit(1)
    finally:
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


async def _cmd_receipts(
    failed_only: bool = False,
    as_json: bool = False,
    run_reaper: bool = False,
) -> None:
    """List Leg 2 receipts from the local store.

    Default output preserves the pre-v1.5 one-line summary + table for
    the common case (no failures, no flags). Failure columns only appear
    once there's at least one non-zero ``attempts`` or a ``last_error_code``
    somewhere, so the happy path looks identical to what operators know.
    """
    import json as json_mod
    from app.payment.receipt_store import get_store
    from app.payment import reasons

    s = load_settings()

    if run_reaper:
        from app.payment.reaper import ClaimReaper
        reaper = ClaimReaper(settings=s)
        if reaper.enabled:
            summary = await reaper.tick()
            if not as_json:
                print(
                    f"Reaper: checked={summary['checked']} "
                    f"reconciled={summary['reconciled']} "
                    f"cleared={summary['cleared']}"
                )

    store = get_store(s.RECEIPT_STORE_PATH)
    await store.initialize()
    summary = await store.summary()

    if failed_only:
        rows = await store.list_by_view("failed_retryable", limit=500)
        rows += await store.list_by_view("failed_terminal", limit=500)
    else:
        # Claimable first (most actionable), then retryable, then pending.
        rows = await store.list_by_view("claimable", limit=500)
        rows += await store.list_by_view("failed_retryable", limit=500)
        rows += await store.list_by_view("pending_sign", limit=500)
        # Locked rows at the end so they don't dominate the top of the
        # list when the interesting data is further down.
        rows += await store.list_by_view("failed_terminal", limit=500)

    if as_json:
        print(json_mod.dumps({
            "store_path": str(s.RECEIPT_STORE_PATH),
            "summary": summary,
            "receipts": [_receipt_to_json(sr) for sr in rows],
        }, indent=2))
        return

    print(f"Receipt store: {s.RECEIPT_STORE_PATH}")
    print(
        f"Claimable: {summary['claimable']} receipt(s), "
        f"total = {summary['claimable_total_price']} wei "
        f"({summary['claimable_total_price'] / 10**18:.6f} tokens)"
    )
    if summary["failed_retryable"] or summary["failed_terminal"]:
        print(
            f"Needs attention: {summary['failed_retryable']} retryable, "
            f"{summary['failed_terminal']} locked"
        )
    if summary["pending_sign"]:
        print(f"Pending signing: {summary['pending_sign']}")

    if not rows:
        return

    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        use_rich = True
    except Exception:
        use_rich = False

    if use_rich:
        table = Table(show_header=True, header_style="bold")
        table.add_column("UUID", style="dim", no_wrap=True)
        table.add_column("Bytes", justify="right")
        table.add_column("Price (wei)", justify="right")
        table.add_column("Age", justify="right")
        table.add_column("Try")
        table.add_column("Status")
        now = int(time.time())
        for sr in rows[:100]:
            age = now - sr.created_at
            tries = _tries_cell(sr)
            status, style = _status_cell(sr)
            uuid_display = sr.receipt.request_uuid
            if sr.view == "failed_terminal":
                uuid_display = f"[strike]{uuid_display}[/]"
            table.add_row(
                uuid_display,
                f"{sr.receipt.data_amount:,}",
                f"{sr.receipt.total_price:,}",
                _humanize_age(age),
                tries,
                f"[{style}]{status}[/]",
            )
        console.print(table)
        if len(rows) > 100:
            console.print(f"[dim]... ({len(rows) - 100} more — use --json for full set)[/]")
    else:
        # Plain-text fallback for environments without rich.
        print()
        print(f"  {'UUID':<38} {'bytes':>12} {'price (wei)':>22} {'age':>8} {'try':>5}  status")
        now = int(time.time())
        for sr in rows[:50]:
            age = now - sr.created_at
            print(
                f"  {sr.receipt.request_uuid:<38} "
                f"{sr.receipt.data_amount:>12d} "
                f"{sr.receipt.total_price:>22d} "
                f"{_humanize_age(age):>8} "
                f"{_tries_cell(sr):>5}  {_status_cell(sr)[0]}"
            )
            if sr.last_error_code:
                msg = reasons.message_for(sr.last_error_code)
                print(f"      {sr.last_error_code}: {msg}")


def _humanize_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _tries_cell(sr) -> str:
    """Show sign vs claim attempts only when non-zero — the default
    happy-path output stays clean."""
    from app.payment import reasons
    if sr.claim_attempts:
        return f"{sr.claim_attempts}/{reasons.MAX_CLAIM_ATTEMPTS}"
    if sr.sign_attempts:
        return f"{sr.sign_attempts}/{reasons.MAX_SIGN_ATTEMPTS}"
    return "—"


def _status_cell(sr) -> tuple[str, str]:
    from app.payment import reasons
    view = sr.view
    if view == "claimable":
        return ("ready to claim", "cyan")
    if view == "pending_sign":
        return ("pending signing", "dim")
    if view == "failed_retryable":
        msg = reasons.message_for(sr.last_error_code) or "retryable"
        return (f"retry: {msg}", "yellow")
    if view == "failed_terminal":
        msg = reasons.message_for(sr.last_error_code) or "locked"
        return (f"locked: {msg}", "red dim")
    if view == "claimed":
        return ("claimed", "green dim")
    return (view, "")


def _receipt_to_json(sr) -> dict:
    from app.payment import reasons as reasons_mod
    return {
        "request_uuid": sr.receipt.request_uuid,
        "tunnel_request_id": sr.tunnel_request_id,
        "client_address": sr.receipt.client_address,
        "node_address": sr.receipt.node_address,
        "data_amount": int(sr.receipt.data_amount),
        "total_price": int(sr.receipt.total_price),
        "view": sr.view,
        "signature_present": bool(sr.signature),
        "created_at": sr.created_at,
        "claimed_at": sr.claimed_at,
        "claim_tx_hash": sr.claim_tx_hash,
        "sign_attempts": sr.sign_attempts,
        "claim_attempts": sr.claim_attempts,
        "max_sign_attempts": reasons_mod.MAX_SIGN_ATTEMPTS,
        "max_claim_attempts": reasons_mod.MAX_CLAIM_ATTEMPTS,
        "last_error_code": sr.last_error_code,
        "last_error_detail": sr.last_error_detail,
        "last_error_message": reasons_mod.message_for(sr.last_error_code),
        "last_attempt_at": sr.last_attempt_at,
        "locked": sr.locked,
    }


async def _cmd_claim(
    include_retryable: bool = False, only_uuid: str | None = None,
) -> None:
    """Submit claimable Leg 2 receipts on-chain.

    Default scope is ``claimable`` only, matching pre-v1.5 behaviour.
    ``include_retryable=True`` picks up ``failed_retryable`` rows for
    explicit retry. ``only_uuid`` restricts the run to a single row and
    refuses if that row is locked.
    """
    from app.payment.settlement import claim_all
    from app.payment.receipt_store import get_store

    s = load_settings()

    if only_uuid:
        store = get_store(s.RECEIPT_STORE_PATH)
        await store.initialize()
        existing = await store.get_by_uuid(only_uuid)
        if existing is None:
            print(f"No receipt found with uuid {only_uuid}", file=sys.stderr)
            sys.exit(1)
        if existing.locked:
            print(
                f"Receipt {only_uuid} is locked (failed_terminal) — refusing "
                f"to claim. Use --unlock to reset if you're sure.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Use identity key by default — operator can override with SR_SETTLEMENT_KEY if they want
    # a separate settlement wallet. Both paths require the key file on disk.
    settlement_key_hex = os.environ.get("SR_SETTLEMENT_KEY", "")
    override = bool(settlement_key_hex)
    if not settlement_key_hex:
        try:
            identity_key, identity_address = load_or_create_identity(
                s.IDENTITY_KEY_PATH, s.IDENTITY_PASSPHRASE,
            )
            settlement_key_hex = identity_key if identity_key.startswith("0x") else "0x" + identity_key
            print(f"Submitting as identity {identity_address}")
        except KeystorePassphraseRequired:
            print("Identity key is encrypted. Set SR_IDENTITY_PASSPHRASE or use --password-file.",
                  file=sys.stderr)
            sys.exit(1)

    # Gas pre-check — the chain tx will revert with cryptic "insufficient
    # funds" if the settlement wallet has 0 native tokens. Fail early with
    # guidance instead.
    if s.ESCROW_CHAIN_RPC:
        from web3 import Web3
        from eth_account import Account
        try:
            w3 = Web3(Web3.HTTPProvider(s.ESCROW_CHAIN_RPC, request_kwargs={"timeout": 10}))
            addr = Account.from_key(settlement_key_hex).address
            balance = w3.eth.get_balance(addr)
        except Exception as e:
            print(f"Could not check gas balance ({e}); proceeding.", file=sys.stderr)
            balance = None
        if balance is not None and balance == 0:
            print(
                f"Settlement wallet {addr} has 0 native tokens for gas.\n"
                f"{'(This is your identity key.) ' if not override else ''}"
                f"Fund it with a small amount of the chain's native token, "
                f"or set SR_SETTLEMENT_KEY=<hex> to a funded wallet and retry.",
                file=sys.stderr,
            )
            sys.exit(0)

    try:
        results = await claim_all(
            s, settlement_key_hex,
            include_retryable=include_retryable,
            only_uuids=[only_uuid] if only_uuid else None,
        )
    except ValueError as e:
        print(f"Cannot claim: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No receipts to submit.")
        return

    total_submitted = sum(r.submitted for r in results)
    total_failed = sum(1 for r in results if r.error)
    total_reconciled = sum(r.skipped_as_already_claimed for r in results)
    total_locked = sum(r.locked_after_failure for r in results)

    print(
        f"Submitted {len(results)} batch(es), {total_submitted} receipt(s) total."
    )
    if total_reconciled:
        print(
            f"Reconciled {total_reconciled} receipt(s) as already-claimed on-chain."
        )
    for i, r in enumerate(results, 1):
        if r.skipped_as_already_claimed and not r.submitted:
            # The reconciliation pseudo-batch — already surfaced above.
            continue
        if r.error:
            tail = f"tx={r.tx_hash}" if r.tx_hash else ""
            print(
                f"  Batch {i}: FAILED ({r.submitted} receipts, "
                f"reason={r.reason_code}) {tail}"
            )
        else:
            print(
                f"  Batch {i}: OK ({r.submitted} receipts) "
                f"tx={r.tx_hash} gas={r.gas_used}"
            )
    if total_locked:
        print(
            f"{total_locked} receipt(s) hit the retry cap and are now "
            f"locked — run --receipts --failed to inspect."
        )
    if total_failed:
        sys.exit(1)


# ``time`` is used inside _cmd_receipts for age display.
import time  # noqa: E402


def main() -> None:
    from app.node_logging import setup_cli_logging, reset_activity  # noqa: E402

    setup_cli_logging()
    reset_activity()

    parser = _build_arg_parser()
    args = parser.parse_args()

    # Apply CLI args as env var overrides before loading settings
    _apply_cli_args(args)

    # Settlement commands — read outstanding receipts or submit them on-chain, then exit.
    if args.receipts:
        asyncio.run(_cmd_receipts(
            failed_only=args.failed,
            as_json=args.output_json,
            run_reaper=args.reap,
        ))
        return
    if args.claim:
        asyncio.run(_cmd_claim(
            include_retryable=args.include_retryable,
            only_uuid=args.uuid,
        ))
        return

    # --reset: clear everything, then re-run wizard and start
    if args.reset:
        if not _do_reset():
            sys.exit(0)
        # Fall through to onboarding wizard
        if sys.stdin.isatty():
            if not _first_run_setup():
                sys.exit(0)
            _show_version_check()
            _show_staking_prompt()
            _run_node(settings_override=load_settings())
        else:
            print("Reset complete. Run again to reconfigure.", file=sys.stderr)
        return

    # Setup wizard: trigger when --setup is passed, identity key is missing,
    # or config looks unconfigured. Only in interactive TTY.
    s = load_settings()
    needs_setup = (
        args.setup
        or not os.path.isfile(s.IDENTITY_KEY_PATH)
        or (not s.STAKING_ADDRESS and s.COORDINATION_API_URL == _default_coordination_url())
    )
    if needs_setup and sys.stdin.isatty():
        if not _first_run_setup():
            sys.exit(0)
        _show_version_check()
        _show_staking_prompt()
        _run_node(settings_override=load_settings())
        return

    _show_version_check()
    _show_staking_prompt()
    _run_node()


if __name__ == "__main__":
    main()
