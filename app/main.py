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
from app import constants
from app.config import load_settings, _default_coordination_url
from app.identity import (
    KeystorePassphraseRequired,
    KeystoreWrongPassphrase,
    load_or_create_identity,
    write_identity_key,
)
from app.state import NodeState, NodeStateMachine
from app.version import __version__
from app.wallet import validate_wallet_address

logger = logging.getLogger(__name__)

# Health check intervals
_HEARTBEAT_INTERVAL = 300  # 5 minutes
_CERT_CHECK_INTERVAL = 86400  # 24 hours
_PROBE_REQUEST_INTERVAL = 1800  # 30 minutes
_HEARTBEAT_FAIL_THRESHOLD = 3

def _wizard_env_file() -> str:
    """Canonical env file the first-run wizard persists into.

    Pre-rc.3 the wizard wrote to ``./.env`` (relative to the cwd), which
    the daemon's settings_loader never reads — so wallet addresses,
    network mode, referral code, and the passphrase boolean were
    silently lost on the first daemon start. Now we write to the
    canonical ``~/.spacerouter/spacerouter.env`` location so
    :py:func:`app.settings_loader.load_provider_settings` migrates it
    into ``settings.json`` on the next read.
    """
    from app.settings_loader import env_path
    p = env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


# ---------------------------------------------------------------------------
# First-run interactive setup (CLI only)
# ---------------------------------------------------------------------------

def _persist_wizard_results(
    *,
    staking_address: str,
    collection_address: str,
    referral_code: str,
    upnp_enabled: bool,
    public_ip: str,
    public_port: str,
    passphrase_set: bool,
) -> None:
    """Write wizard answers directly into ``~/.spacerouter/settings.json``.

    Pre-rc.5 the wizard only wrote to ``spacerouter.env`` and relied on
    ``settings_loader`` to migrate the env file on the next daemon read.
    But ``load_settings()`` is invoked BEFORE the wizard (to compute
    ``needs_setup``), and that call's cold-start branch persists a
    defaults-only ``settings.json``. From then on the env-file migration
    is a no-op (target exists), so the wizard's values never reach
    settings.json. This helper plugs that gap by writing settings.json
    directly using the same ``Settings.from_env_mapping`` shape.

    The passphrase is INTENTIONALLY not persisted — only the
    ``identity_passphrase_set`` boolean is, and only when the user
    actually picked a passphrase. The plaintext passphrase stays in
    ``os.environ['SR_IDENTITY_PASSPHRASE']`` for the immediate daemon
    start that follows.
    """
    from app.settings_loader import settings_path
    from app.settings_v2 import Settings as _SettingsV2
    from app.variant import BUILD_VARIANT as _BUILD_VARIANT

    env_dict: dict[str, str] = {"SR_BUILD_VARIANT": _BUILD_VARIANT}
    if staking_address:
        env_dict["SR_STAKING_ADDRESS"] = staking_address
    if collection_address:
        env_dict["SR_COLLECTION_ADDRESS"] = collection_address
    if referral_code:
        env_dict["SR_REFERRAL_CODE"] = referral_code
    env_dict["SR_UPNP_ENABLED"] = "true" if upnp_enabled else "false"
    if public_ip:
        env_dict["SR_PUBLIC_IP"] = public_ip
    if public_port and str(public_port) != "9090":
        env_dict["SR_PUBLIC_PORT"] = str(public_port)
    if passphrase_set:
        # ``from_env_mapping`` reads the presence of SR_IDENTITY_PASSPHRASE
        # to flip the boolean — value is never stored, only the boolean flag.
        env_dict["SR_IDENTITY_PASSPHRASE"] = "set"

    sp = settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)

    # Merge: if a defaults-only settings.json already exists (cold-start
    # path), preserve any operator-set fields we didn't overwrite. Build
    # a fresh Settings from the wizard mapping, then copy it over the
    # existing one section-by-section using model_dump merge.
    merged = _SettingsV2.from_env_mapping(env_dict)
    if sp.exists():
        try:
            existing = _SettingsV2.load(sp)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            # Wizard answers win for the fields the user just chose; for
            # everything else (escrow defaults, coord URL, ports the
            # wizard didn't touch) keep the existing value.
            if not staking_address:
                merged.wallet.staking_address = existing.wallet.staking_address
            if not collection_address:
                merged.wallet.collection_address = existing.wallet.collection_address
            if not referral_code:
                merged.node.referral_code = existing.node.referral_code
            if not public_ip:
                merged.node.public_ip = existing.node.public_ip
            if not public_port or str(public_port) == "9090":
                merged.node.public_port = existing.node.public_port
            # Preserve previously-configured escrow + coord URL.
            merged.escrow = existing.escrow
            merged.coordination = existing.coordination
            # Carry over identity_passphrase_set when the wizard didn't
            # touch it (existing key, no fresh passphrase set).
            if not passphrase_set:
                merged.wallet.identity_passphrase_set = (
                    existing.wallet.identity_passphrase_set
                )

    merged.save(sp)


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
        env_file = _wizard_env_file()
        existing_referral = get_key(env_file, "SR_REFERRAL_CODE") or ""
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

        # --- Persist to ~/.spacerouter/spacerouter.env ---
        # settings_loader migrates this into settings.json on the next
        # daemon read. The passphrase is intentionally NOT migrated to
        # settings.json — only the boolean flag is — so the wizard ALSO
        # exports it to os.environ for the immediate daemon start that
        # follows. Subsequent restarts re-prompt via the unlock dialog
        # (GUI) or require SR_IDENTITY_PASSPHRASE to be set externally
        # (systemd EnvironmentFile, etc.).
        if passphrase:
            set_key(env_file, "SR_IDENTITY_PASSPHRASE", passphrase)
            os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase
        if staking_address:
            set_key(env_file, "SR_STAKING_ADDRESS", staking_address)
        if collection_address:
            set_key(env_file, "SR_COLLECTION_ADDRESS", collection_address)
        if referral_code:
            set_key(env_file, "SR_REFERRAL_CODE", referral_code)

        # Network mode
        set_key(env_file, "SR_UPNP_ENABLED", str(upnp_enabled).lower())
        if public_ip:
            set_key(env_file, "SR_PUBLIC_IP", public_ip)
        if public_port and public_port != "9090":
            set_key(env_file, "SR_PUBLIC_PORT", public_port)

        # ALSO write directly to settings.json. Pre-rc.5 we relied on
        # settings_loader picking up the env file on the next daemon
        # start, but a settings.json had already been created by the
        # earlier ``load_settings()`` call (the cold-start path persists
        # defaults). With settings.json present the env-file migration is
        # skipped and the wizard's values are silently dropped.
        _persist_wizard_results(
            staking_address=staking_address,
            collection_address=collection_address,
            referral_code=referral_code,
            upnp_enabled=upnp_enabled,
            public_ip=public_ip,
            public_port=public_port,
            passphrase_set=bool(passphrase),
        )

        wizard_done(env_file)
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
        self.auto_claim_monitor = None  # AutoClaimMonitor | None


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
            s.NODE_PORT, lease_duration=constants.UPNP_LEASE_DURATION,
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
        host=constants.BIND_ADDRESS,
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
    else:
        _log_leg2_gated_off(ctx.s)

    # Upgrade to mTLS if enabled
    _upgrade_mtls(ctx)


def _log_leg2_gated_off(s) -> None:
    """Surface why Leg 2 is disabled. test.95 silently skipped this path
    when escrow.enabled=false slipped through Fresh Restart, leaving the
    user wondering why no receipts ever materialised. Make the gate
    decision explicit in the log so the next misconfiguration shows up
    immediately instead of via a quiet Earnings-card spam.
    """
    from app.variant import BUILD_VARIANT

    if not s.PAYMENT_ENABLED:
        msg = (
            "Leg 2 disabled — settings.escrow.enabled=false. "
            "Receipts will not be signed or claimed. "
            "Set escrow.enabled=true in ~/.spacerouter/settings.json "
            "(or SR_PAYMENT_ENABLED=true) to participate in escrow payments."
        )
    else:
        msg = (
            "Leg 2 disabled — settings.escrow.leg2_rate_per_gb=%d (must be > 0). "
            "Set a non-zero rate (the coord TOFU sync will overwrite it on first /config call)."
        ) % int(getattr(s, "NODE_RATE_PER_GB", 0) or 0)

    if BUILD_VARIANT == "test":
        # On test builds, escrow OFF is almost always misconfiguration —
        # escalate to WARNING so it shows up in the GUI status panel
        # alongside other startup warnings.
        logger.warning(msg)
    else:
        logger.info(msg)


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

    # P3/L5 — one-shot reconciliation of any tx that was broadcast
    # but didn't reach mark_claimed before the previous run crashed.
    # Runs before the reaper starts so the recurring reaper tick
    # doesn't redo work the reconciler already handled. Best-effort
    # — failure here just logs; the reaper picks up anything left.
    try:
        from app.payment.inflight_reconciler import reconcile_inflight
        recon = await reconcile_inflight(ctx.s)
        if recon["checked"]:
            logger.info(
                "Startup reconcile: %d in-flight row(s) — settled %d, "
                "cleared %d.",
                recon["checked"], recon["marked_claimed"], recon["cleared"],
            )
    except Exception:
        logger.exception("Startup in-flight reconcile failed; continuing")

    # Reaper resolves stuck CLAIM_TX_TIMEOUT rows by re-querying the chain.
    # Only runs when escrow RPC + contract are configured — safe on dev
    # setups that don't have on-chain settlement enabled.
    from app.payment.reaper import ClaimReaper
    reaper = ClaimReaper(settings=ctx.s)
    if reaper.enabled:
        await reaper.start()
        ctx.claim_reaper = reaper

    # P10 — optional auto-claim monitor. Default OFF; only spins up when
    # the operator opted in via settings.json. Same lifecycle as the
    # reaper: start now, stop on daemon shutdown. Reuses the daemon's
    # already-resolved identity key as the settlement key (matching the
    # CLI ``--claim`` default), so the monitor never has to re-prompt
    # for a passphrase from a background task.
    from app.payment.auto_claim import AutoClaimMonitor
    settlement_key_hex = os.environ.get("SR_SETTLEMENT_KEY", "") or (
        ctx.identity_key if ctx.identity_key.startswith("0x")
        else ("0x" + ctx.identity_key if ctx.identity_key else "")
    )
    auto_claim = AutoClaimMonitor(
        settings=ctx.s, settlement_key_hex=settlement_key_hex or None,
    )
    if auto_claim.enabled:
        await auto_claim.start()
        ctx.auto_claim_monitor = auto_claim

    logger.info(
        "Leg 2 submitter ready — payer=%s node_wallet=%s rate=%d/GB "
        "(poller every 10s, reaper enabled=%s, auto-claim enabled=%s)",
        gateway_payer, node_wallet[:12] + "...",
        ctx.s.NODE_RATE_PER_GB, reaper.enabled, auto_claim.enabled,
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
        handler, host=constants.BIND_ADDRESS, port=s.NODE_PORT, ssl=ctx.ssl_ctx,
        reuse_address=True,
    )


async def _upnp_renewal_loop(
    renew_fn,  # noqa: ANN001 — async callable returning bool
    long_interval: int,
    short_interval: int = 60,
    escalate_after: int = 3,
) -> None:
    """Keep the UPnP port mapping alive across its lease lifetime.

    ``renew_fn`` is a full re-discovery + re-add, so on success the
    mapping is good for another ``LEASE_DURATION`` seconds. We wake
    every ``long_interval`` seconds for the normal refresh, but on
    failure we shrink to ``short_interval`` so transient router /
    network blips don't leave the mapping expired for up to a full
    half-lease gap — that was the "ENDPOINT_UNREACHABLE at ~1h15m"
    symptom QA saw on a default 3600s lease.

    Factored out of ``_run`` so it can be tested without standing up
    the whole node.
    """
    consecutive_failures = 0
    while True:
        interval = short_interval if consecutive_failures > 0 else long_interval
        await asyncio.sleep(interval)
        ok = await renew_fn()
        if ok:
            if consecutive_failures:
                logger.info(
                    "UPnP lease recovered after %d failed attempt(s)",
                    consecutive_failures,
                )
            consecutive_failures = 0
            logger.debug("UPnP lease renewed")
        else:
            consecutive_failures += 1
            # Escalate tone once we've burned through the natural grace
            # window (short_interval retries cover a few minutes; past
            # that the original mapping is probably already expired).
            if consecutive_failures >= escalate_after:
                logger.error(
                    "UPnP lease renewal has failed %d consecutive "
                    "attempts — node may be ENDPOINT_UNREACHABLE until "
                    "the router accepts a re-mapping.",
                    consecutive_failures,
                )
            else:
                logger.warning(
                    "UPnP lease renewal failed (attempt %d); retrying in %ds",
                    consecutive_failures, short_interval,
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
            # rc.6 BLK-3: the self-probe loop (_self_probe_loop) can also
            # trigger this transition. If it raced ahead of us, the state
            # is already RECONNECTING and a second transition would raise
            # ValueError (RECONNECTING→RECONNECTING is not allowed) —
            # killing this task. Guard at the call site; the state table
            # itself stays correct.
            if sm.state == NodeState.RECONNECTING:
                return
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
                result = await request_probe(
                    ctx.http, ctx.s, ctx.node_id,
                    identity_key=ctx.identity_key,
                )
                if result.outcome == "ok":
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
_SELF_PROBE_BACKOFF_CAP = 600  # 10 min max backoff on persistent failures
# After this many consecutive offline polls (≈6 min at 60s each), give up on
# self-probe recovery and force RECONNECTING — that path retries UPnP and
# re-registers, which is the only thing that can override offline status
# without a consecutive-success threshold.
_SELF_PROBE_OFFLINE_ESCALATION_THRESHOLD = 6


async def _self_probe_loop(
    ctx: "_NodeContext",
    sm: NodeStateMachine,
    stop_event: asyncio.Event,
    dashboard=None,  # noqa: ANN001
) -> None:
    """Periodically check node status from coordination's perspective.

    Runs every 60s (vs 5min for health checks) to catch bore tunnel
    disconnects and other reachability issues quickly.  Also feeds
    staking_status, health_score, and probe results to the dashboard
    and to ``sm.status`` for GUI consumption.

    Recovery flow (the bug this exists to fix):
      - On the first online→offline transition, we fire a probe request
        immediately, ignoring the client-side cooldown (the server's
        own rate limit still applies).
      - On 429 we honour the server's ``Try again in {N}s`` hint exactly
        instead of blindly doubling the cooldown.
      - On other failures we double the cooldown up to
        ``_SELF_PROBE_BACKOFF_CAP``.
      - After ``_SELF_PROBE_OFFLINE_ESCALATION_THRESHOLD`` consecutive
        offline polls we transition to RECONNECTING, since persistent
        offline status almost always means the tunnel/UPnP lease is
        dead and only re-registration can recover it.
    """
    import time as _time

    from app.registration import check_node_status, request_probe

    # Run first check almost immediately (5s delay for registration to settle)
    first_run = True
    # Start at 0.0 so the first attempt can fire immediately if the loop is
    # created into an already-offline state — the server's own rate limit
    # is still respected via the 429 path.
    last_probe_request_time = 0.0
    current_cooldown = _SELF_PROBE_REQUEST_COOLDOWN
    previous_status: str | None = None
    consecutive_offline = 0
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
        except Exception as exc:
            # Promoted DEBUG → INFO: a swallowed status check is the kind of
            # thing operators need to see when the node is silently stuck.
            logger.info("Self-probe check failed: %s", exc)
            sm.status.last_probe_outcome = "failed"
            if dashboard:
                dashboard.update(
                    last_probe_result="error",
                    last_probe_time=_time.time(),
                )
            continue

        status = node_data.get("status", "unknown")
        health_score = float(node_data.get("health_score", 0.0))
        staking_status = node_data.get("staking_status", "—")

        # Plumb coord-side observations through to GUI / dashboard.
        sm.status.coord_status = status
        sm.status.coord_health_score = health_score
        sm.status.staking_status = staking_status

        if status in ("online", "active"):
            consecutive_offline = 0
            previous_status = status
            sm.status.next_probe_attempt_at = None
            if dashboard:
                dashboard.update(
                    last_probe_result=status,
                    last_probe_time=_time.time(),
                    health_status=status,
                    health_score=str(health_score),
                    staking_status=staking_status,
                )
            continue

        # ---- Offline branch ------------------------------------------------
        consecutive_offline += 1
        # Online→offline transition: force an immediate probe request
        # (subject only to the server's rate limit, not the client cooldown).
        is_first_transition = (
            previous_status in ("online", "active") and consecutive_offline == 1
        )
        previous_status = status

        logger.warning(
            "Self-probe: coord reports status=%s health_score=%.1f (consecutive_offline=%d)",
            status, health_score, consecutive_offline,
        )

        now = _time.time()
        cooldown_ok = (now - last_probe_request_time) >= current_cooldown
        probe_result = "cooldown"

        if is_first_transition or cooldown_ok:
            # Always advance last_probe_request_time so the client-side gate
            # is honest about when we actually reached out.
            last_probe_request_time = now
            sm.status.last_probe_attempt_at = now
            ctx._last_probe_request_time = now

            result = await request_probe(
                ctx.http, ctx.s, ctx.node_id,
                identity_key=ctx.identity_key,
            )
            if result.outcome == "ok":
                current_cooldown = _SELF_PROBE_REQUEST_COOLDOWN
                sm.status.last_probe_outcome = "ok"
                sm.status.next_probe_attempt_at = now + current_cooldown
                probe_result = "probe_requested"
                logger.info(
                    "Probe requested for node %s — next attempt in %ds",
                    ctx.node_id, current_cooldown,
                )
            elif result.outcome == "rate_limited":
                # Honour the server's retry hint exactly — no exponential
                # doubling. +5s jitter buffer to avoid racing the rate limit.
                wait = (result.retry_after_seconds or _SELF_PROBE_REQUEST_COOLDOWN) + 5
                current_cooldown = wait
                sm.status.last_probe_outcome = "rate_limited"
                sm.status.next_probe_attempt_at = now + wait
                probe_result = "rate_limited"
                logger.info("Probe rate-limited by coord (retry in %ds)", wait)
            else:  # failed
                current_cooldown = min(current_cooldown * 2, _SELF_PROBE_BACKOFF_CAP)
                sm.status.last_probe_outcome = "failed"
                sm.status.next_probe_attempt_at = now + current_cooldown
                probe_result = "probe_failed"
                logger.info(
                    "Probe request failed — backing off to %ds", current_cooldown,
                )
        else:
            # Cooldown active — don't hit the server, but surface when we
            # plan to try next so the GUI can render a countdown.
            sm.status.last_probe_outcome = "cooldown"
            sm.status.next_probe_attempt_at = last_probe_request_time + current_cooldown

        if dashboard:
            dashboard.update(
                last_probe_result=probe_result,
                last_probe_time=_time.time(),
                health_status=status,
                health_score=str(health_score),
                staking_status=staking_status,
            )

        # Escalation: persistent offline despite probe attempts → force
        # RECONNECTING.  The orchestrator's RECONNECTING path retries UPnP
        # and re-registers; re-registration enqueues a REGISTRATION-priority
        # probe which is the only class that can override offline status
        # without a consecutive-success threshold on the coord side.
        if consecutive_offline >= _SELF_PROBE_OFFLINE_ESCALATION_THRESHOLD:
            logger.warning(
                "Coord reports offline for %d consecutive polls — escalating to RECONNECTING",
                consecutive_offline,
            )
            sm.status.last_probe_outcome = "escalated"
            # rc.6 BLK-3: see _health_loop — the same race applies here in
            # reverse. Guard against a double-transition that would raise
            # ValueError and kill this loop.
            if sm.state == NodeState.RECONNECTING:
                return
            sm.transition(
                NodeState.RECONNECTING,
                "Persistent offline status from coord — retrying registration",
            )
            return  # exit loop; orchestrator handles reconnect


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

    # Two-layer acquisition:
    #
    # 1. Try the OS lock. Fast path; genuine "another live daemon" case
    #    also hits this path and we exit after the stale-check finds a
    #    live PID.
    # 2. If the lock is already held, check whether the PID written in
    #    the lock file corresponds to a **live** process. If not, treat
    #    the lock as stale (crashed predecessor still holding a file
    #    handle on Windows under CI load, an uncleaned-up zombie on
    #    Unix, etc). Truncate the file, reacquire.
    #
    # This replaces a short retry loop that was too tight for Windows
    # CI smoke tests where the OS sometimes took >2s to release the
    # msvcrt lock after TerminateProcess.
    import time as _time

    def _try_acquire(fd_) -> bool:
        try:
            if is_windows:
                import msvcrt
                fd_.seek(0)
                msvcrt.locking(fd_.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd_.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _pid_is_our_daemon(pid: int) -> bool:
        """True only if PID is alive AND the process image is our binary.

        This catches PID reuse: if our prior daemon's PID got recycled to
        an unrelated process (typical on Windows CI runners after
        TerminateProcess), we treat the lock as stale rather than
        refusing to start. Without this check we hit the
        v1.5.0-test.85+ smoke-test failure where the second daemon in a
        sequence sees a recycled PID and thinks "another daemon is
        running" when there isn't one.
        """
        if pid <= 0:
            return False

        # Read the running process's image path; compare basename.
        binary_basename = os.path.basename(sys.executable).lower()
        # PyInstaller-frozen builds report the bundled binary; source
        # runs report the python interpreter. Either is fine — both are
        # what `os.getpid()` would resolve to from inside this daemon.

        if is_windows:
            try:
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
                )
                if not h:
                    return False
                try:
                    STILL_ACTIVE = 259
                    code = ctypes.c_ulong(0)
                    kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                    if code.value != STILL_ACTIVE:
                        return False  # cleanly exited, definitely not us

                    # Process is alive — check if it's actually our binary.
                    QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
                    QueryFullProcessImageNameW.argtypes = [
                        wintypes.HANDLE, wintypes.DWORD,
                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                    ]
                    QueryFullProcessImageNameW.restype = wintypes.BOOL
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(len(buf))
                    if not QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        # Couldn't read the image path — be safe and treat as
                        # not-ours so we take the lock instead of hanging.
                        return False
                    return os.path.basename(buf.value).lower() == binary_basename
                finally:
                    kernel32.CloseHandle(h)
            except Exception:
                # Uncertain on Windows: prefer "not ours" so we take the
                # lock. The msvcrt.locking() call below is the real
                # gatekeeper anyway — if a real daemon does hold it,
                # that fails and we refuse.
                return False
        else:
            # POSIX: kill(pid, 0) tells us alive/dead; /proc/<pid>/comm
            # tells us the binary name on Linux. macOS uses `ps` since
            # /proc isn't reliable.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # Alive but not ours-as-the-uid. Could still be our
                # daemon under a different user — be safe.
                return True

            try:
                if sys.platform.startswith("linux"):
                    comm = open(f"/proc/{pid}/comm").read().strip()
                    return comm.lower() in {
                        binary_basename,
                        "python", "python3", binary_basename.removesuffix(".exe"),
                    }
                else:  # macOS / BSD: use ps -p PID -o comm=
                    import subprocess
                    out = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "comm="],
                        capture_output=True, text=True, timeout=2,
                    )
                    if out.returncode != 0:
                        return False
                    comm = os.path.basename(out.stdout.strip()).lower()
                    return comm == binary_basename or comm.startswith("python")
            except Exception:
                # /proc read failure or ps failure — conservatively say
                # alive (don't accidentally take a real daemon's lock).
                return True

    # Backwards-compat alias for the older check (some unit tests may
    # still patch this name).
    _pid_alive = _pid_is_our_daemon

    if not _try_acquire(fd):
        # Read stored PID to decide whether the lock is stale.
        stale = False
        try:
            fd.seek(0)
            content = fd.read().strip()
            pid_line = content.splitlines()[-1] if content else ""
            prior_pid = int(pid_line) if pid_line.isdigit() else 0
            stale = not _pid_alive(prior_pid)
        except Exception:
            stale = False

        if stale:
            # The holder is gone — the OS just hasn't reaped the lock
            # yet (typical Windows post-TerminateProcess behaviour).
            # Close, reopen (truncating), retry a few times.
            fd.close()
            acquired = False
            for _ in range(8):
                _time.sleep(0.25)
                try:
                    fd = open(lock_path, "a+" if is_windows else "w")
                except OSError:
                    continue
                if _try_acquire(fd):
                    acquired = True
                    break
                fd.close()
            if not acquired:
                print(
                    f"Daemon lock {lock_path} appears stale but the OS "
                    f"won't release it. Delete the file manually if no "
                    f"space-router-node process is running, then retry.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            fd.close()
            print(
                f"Another space-router-node daemon is already running "
                f"against {store_path}. Refusing to start to avoid "
                f"receipt corruption. Lock: {lock_path}",
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

    # ── Trust-on-first-use sync of escrow config from coord ──
    # Track P2: on first launch, fetch `/config` once, persist the gateway
    # rate / payer into ``settings.json``. Subsequent launches see the
    # ``synced_from_coord_at`` stamp and skip the network call. Drift is
    # handled gateway-side via ``SIGN_REJECTED_PRICE_CAP`` rejection
    # messages (which include the expected rate). HTTP failures here are
    # WARN-only — they never block daemon startup.
    #
    # We do this BEFORE ``load_settings()`` builds the legacy ``Settings``
    # shape so the rate populated below is visible via ``s.NODE_RATE_PER_GB``
    # downstream (in ``_phase_register`` → ``_init_receipt_submitter``).
    #
    # The sync runs on EVERY launch, including the GUI path (which passes
    # ``settings_override=load_settings()`` from a snapshot taken before
    # this point).  Earlier versions skipped the sync when an override
    # was supplied — that meant GUI users never re-synced, and the
    # bootstrap rate from PRs #88 / #93 stayed stuck at 1e15 wei/GB
    # forever.  After sync, if anything changed, we drop the stale
    # override and re-load from disk so downstream code sees the synced
    # values rather than the snapshot.
    settings_changed_on_disk = False
    try:
        from app.escrow_config_sync import sync_escrow_config_from_coord
        from app.settings_loader import load_provider_settings, settings_path
        s_path = settings_path()
        v2_before = load_provider_settings()
        had_stamp = bool(v2_before.escrow.synced_from_coord_at)
        had_rate = bool(v2_before.escrow.leg2_rate_per_gb)
        rate_before = v2_before.escrow.leg2_rate_per_gb
        v2_after = sync_escrow_config_from_coord(v2_before)
        # Persist when the sync actually populated something new OR
        # overwrote a stale rate (the test.97 backfill scenario).
        now_has_stamp = bool(v2_after.escrow.synced_from_coord_at)
        now_has_rate = bool(v2_after.escrow.leg2_rate_per_gb)
        rate_changed = v2_after.escrow.leg2_rate_per_gb != rate_before
        if (
            (now_has_stamp and not had_stamp)
            or (now_has_rate and not had_rate)
            or rate_changed
        ):
            v2_after.save(s_path)
            settings_changed_on_disk = True
    except Exception:
        # Never let escrow sync block the daemon. Logged inside the
        # function for the expected error paths; a true blow-up here
        # (e.g. settings.json gone read-only) gets swallowed with a
        # warning so the rest of startup still happens.
        logger.warning("escrow config sync failed unexpectedly — continuing", exc_info=True)

    if settings_changed_on_disk and settings_override is not None:
        # GUI passed us a stale snapshot. Disk state is now authoritative;
        # reload so the rest of the boot uses the synced rate.
        logger.info("escrow sync wrote new settings — reloading from disk")
        settings_override = None

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
            except KeystorePassphraseRequired as exc:
                if state_machine:
                    # KeystoreWrongPassphrase is a subclass of
                    # KeystorePassphraseRequired — surface a distinct
                    # message so the prompt UI can say "incorrect" rather
                    # than "required" when the user has already tried.
                    if isinstance(exc, KeystoreWrongPassphrase):
                        reason = "Passphrase is incorrect — re-enter your passphrase"
                    else:
                        reason = "Identity key is encrypted — passphrase required"
                    state_machine.transition(NodeState.PASSPHRASE_REQUIRED, reason)
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
            if ctx.upnp_endpoint and constants.UPNP_LEASE_DURATION > 0:
                from app.upnp import renew_upnp_mapping

                async def _renew_tick() -> bool:
                    return await renew_upnp_mapping(
                        s.NODE_PORT, ctx.upnp_endpoint[1],
                        constants.UPNP_LEASE_DURATION,
                    )

                renewal_task = asyncio.create_task(
                    _upnp_renewal_loop(
                        _renew_tick,
                        long_interval=max(constants.UPNP_LEASE_DURATION // 2, 60),
                        short_interval=60,
                    )
                )

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
                                    lease_duration=constants.UPNP_LEASE_DURATION,
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
                        if ctx.upnp_endpoint and constants.UPNP_LEASE_DURATION > 0:
                            from app.upnp import renew_upnp_mapping

                            async def _renew_loop() -> None:
                                interval = max(constants.UPNP_LEASE_DURATION // 2, 60)
                                while True:
                                    await asyncio.sleep(interval)
                                    ok = await renew_upnp_mapping(
                                        s.NODE_PORT, ctx.upnp_endpoint[1],
                                        constants.UPNP_LEASE_DURATION,
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

            # Stop auto-claim monitor (P10)
            if ctx.auto_claim_monitor is not None:
                try:
                    await ctx.auto_claim_monitor.stop()
                except Exception:
                    logger.debug("Auto-claim monitor stop errored", exc_info=True)

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

    All progress prints are flushed so that scripted callers (no TTY) see
    each step land in their log file as it happens, rather than only at
    process exit when Python finally flushes its block buffer.
    """
    from app.paths import config_dir

    s = load_settings()

    # Check both well-known config dir and CWD for config files
    cfg_dir = config_dir()
    wellknown_env = cfg_dir / "spacerouter.env"
    cwd_env = os.path.abspath(".env")

    env_file = str(wellknown_env) if wellknown_env.is_file() else cwd_env
    certs_dir = os.path.dirname(os.path.abspath(s.IDENTITY_KEY_PATH)) or "certs"
    settings_file = cfg_dir / "settings.json"

    if sys.stdin.isatty():
        print("WARNING: This will delete your identity key and all configuration.", flush=True)
        confirm = input("Type YES to confirm: ").strip()
        if confirm != "YES":
            print("Reset cancelled.", flush=True)
            return False
    else:
        # Without a TTY we cannot prompt. Surface what is about to happen
        # so a scripted operator sees something even if the redirected
        # stdout buffer wouldn't flush until process exit.
        print(
            "Non-interactive --reset: removing all config without confirmation.",
            flush=True,
        )

    # rc.6 MAJ-3: tell the coord we're going away BEFORE we delete the
    # identity key — otherwise the dashboard sees this node as online
    # for the full health-check timeout (~3 min) after --reset returns.
    # Best-effort; do NOT block reset on coord failure.
    try:
        from app.registration import deregister_best_effort_sync
        if deregister_best_effort_sync(s):
            print("Notified coordination API (status → offline).", flush=True)
    except Exception:
        # Already logged inside the helper; reset must still proceed.
        pass

    # Delete settings.json (canonical v1.5 config)
    if settings_file.is_file():
        settings_file.unlink()
        print(f"Removed {settings_file}", flush=True)

    # Delete legacy .env (only present on upgrades from v1.4)
    if os.path.isfile(env_file):
        os.remove(env_file)
        print(f"Removed {env_file}", flush=True)

    # Delete certs directory (identity key + all certificates)
    if os.path.isdir(certs_dir):
        import shutil
        shutil.rmtree(certs_dir)
        print(f"Removed {certs_dir}/", flush=True)

    # Wipe operational state — receipts.db, incidents.json, logs/.
    # Pre-rc.3 these survived a CLI --reset, so the next start
    # surfaced stale failed-claim rows and old incidents.
    from app.paths import wipe_operational_state
    for note in wipe_operational_state(cfg_dir):
        print(note, flush=True)

    print("Reset complete.\n", flush=True)
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
        help="Node listen port, 1-65535 (default: 9090)",
    )
    net.add_argument(
        "--public-url", metavar="HOST",
        help="Public hostname or IP (tunnel mode)",
    )
    net.add_argument(
        "--public-port", type=int, metavar="PORT",
        help="Advertised public port, 1-65535 (tunnel mode)",
    )
    net.add_argument(
        "--no-upnp", action="store_true",
        help="Disable UPnP automatic port forwarding",
    )

    # Identity / wallet settings
    wallet = parser.add_argument_group("wallet")
    wallet.add_argument(
        "--staking-address", metavar="ADDR",
        help="EVM staking wallet address (0x followed by 40 hex chars)",
    )
    wallet.add_argument(
        "--collection-address", metavar="ADDR",
        help="EVM collection wallet address (0x followed by 40 hex chars)",
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
        "--include-claimed", action="store_true", dest="include_claimed",
        help="With --receipts --json: include already-claimed (settled) "
             "receipts in the output alongside pending and failed rows. "
             "Default off so the JSON payload stays focused on actionable "
             "rows.",
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
        help="With --claim: target this specific UUID. "
             "--claim refuses if the row is locked (failed_terminal).",
    )

    return parser


def _validate_cli_args(args: argparse.Namespace) -> None:
    """Reject invalid CLI args before the daemon starts.

    The Phase A real-user sweep showed the daemon accepting nonsense like
    --port 0 or --staking-address bogus and starting anyway, producing
    receipts that fail much later at claim time. We validate at the CLI
    boundary so bad input fails fast with a clear message.

    Errors print to stderr and exit with status 2 (argparse-style usage
    error). Each check is independent so we can surface every problem in
    a single run rather than playing whack-a-mole.
    """
    errors: list[str] = []

    if args.port is not None and not (1 <= args.port <= 65535):
        errors.append(
            f"--port must be in 1..65535, got {args.port}"
        )
    if args.public_port is not None and not (1 <= args.public_port <= 65535):
        errors.append(
            f"--public-port must be in 1..65535, got {args.public_port}"
        )

    from app.wallet import validate_wallet_address
    for flag, value in (
        ("--staking-address", args.staking_address),
        ("--collection-address", args.collection_address),
    ):
        if value is None:
            continue
        try:
            validate_wallet_address(value)
        except ValueError as exc:
            errors.append(f"{flag}: {exc}")

    if errors:
        print("space-router-node: invalid CLI arguments:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(2)


def _persist_network_mode_to_settings(
    public_url: str | None,
    public_port: int | None,
    no_upnp: bool,
) -> None:
    """Write tunnel-mode network settings to settings.json.

    Pre-rc.3 ``--public-url`` / ``--public-port`` only set os.environ for
    the running process, so they were silently forgotten on every
    restart. Headless tunnel-mode operators (CGNAT bypass via bore.pub
    or similar) expected the values to persist the way the GUI's
    "Tunnel mode" toggle does. Best-effort: never block startup if the
    write fails.
    """
    if public_url is None and public_port is None and not no_upnp:
        return
    try:
        from app.settings_loader import load_provider_settings, settings_path
        s = load_provider_settings()
        if public_url is not None:
            s.node.public_ip = public_url
            s.node.upnp_enabled = False
        if public_port is not None:
            s.node.public_port = int(public_port)
        if no_upnp:
            s.node.upnp_enabled = False
        s_path = settings_path()
        s_path.parent.mkdir(parents=True, exist_ok=True)
        s.save(s_path)
        logger.info(
            "Persisted network mode to %s (public_ip=%s, public_port=%s, "
            "upnp_enabled=%s)",
            s_path, s.node.public_ip, s.node.public_port, s.node.upnp_enabled,
        )
    except Exception as e:  # noqa: BLE001
        # Non-fatal: the os.environ override below still takes effect for
        # this run; only the persistence is lost.
        logger.warning("Could not persist network-mode CLI flags: %s", e)


def _apply_cli_args(args: argparse.Namespace) -> None:
    """Override environment variables from CLI arguments.

    CLI args take precedence over .env values. We set os.environ so that
    pydantic-settings picks them up when load_settings() is called.

    Network-mode flags (``--public-url``, ``--public-port``, ``--no-upnp``)
    are also persisted to settings.json so the operator doesn't have to
    re-pass them on every restart — matching the GUI's "Tunnel mode"
    toggle semantics.
    """
    if args.port is not None:
        os.environ["SR_NODE_PORT"] = str(args.port)
    if args.public_url is not None:
        os.environ["SR_PUBLIC_IP"] = args.public_url
    if args.public_port is not None:
        os.environ["SR_PUBLIC_PORT"] = str(args.public_port)
    if args.no_upnp:
        os.environ["SR_UPNP_ENABLED"] = "false"
    _persist_network_mode_to_settings(
        args.public_url, args.public_port, args.no_upnp,
    )
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
    include_claimed: bool = False,
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
        # rc.5 minor #2 — include claimed history for tools that want
        # the full picture. Off by default so the operator-facing
        # default stays focused on actionable rows.
        if include_claimed:
            rows += await store.list_by_view("claimed", limit=500)

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

    # P3/L3 — share the GUI's claim.lock so a CLI claim and a GUI
    # claim can't double-submit the same nonces. Stale-lock recovery
    # is inherited from the OS primitive; if a previous holder crashed,
    # the lock will be free.
    from app.payment.claim_lock import acquire_claim_lock, ClaimLockHeld

    try:
        with acquire_claim_lock(s):
            try:
                results = await claim_all(
                    s, settlement_key_hex,
                    include_retryable=include_retryable,
                    only_uuids=[only_uuid] if only_uuid else None,
                )
            except ValueError as e:
                print(f"Cannot claim: {e}", file=sys.stderr)
                sys.exit(1)
    except ClaimLockHeld:
        print(
            "another claim is in progress (GUI or CLI). Wait for it to "
            "finish or close the GUI.",
            file=sys.stderr,
        )
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

    parser = _build_arg_parser()
    args = parser.parse_args()

    # ``--receipts --json`` is consumed by tooling that reads stdout as
    # JSON. Route INFO/WARNING/ERROR to stderr so log lines never bleed
    # into the JSON payload. Also applies to ``--claim`` when run with
    # any later ``--json`` flag (currently no such flag, but the same
    # reasoning would apply).
    log_to_stderr = bool(getattr(args, "output_json", False) and args.receipts)
    setup_cli_logging(log_to_stderr=log_to_stderr)
    reset_activity()

    # Validate CLI input before doing any work. Bad --port / --staking-address
    # used to start the daemon anyway and surface as far-downstream failures
    # (Phase A findings #6 / #7).
    _validate_cli_args(args)

    # First-launch on PyInstaller bundles can spend 6-9s on import + crypto
    # init before any log line lands; without this hint the user thinks the
    # process hung. Flush so it shows even when stdout is redirected to a
    # file (Phase A finding #3). --receipts / --claim are short-running
    # commands with their own output; skip the hint there to keep
    # machine-readable output clean.
    if not (args.receipts or args.claim):
        print("space-router-node: starting...", flush=True)

    # Apply CLI args as env var overrides before loading settings
    _apply_cli_args(args)

    # Settlement commands — read outstanding receipts or submit them on-chain, then exit.
    if args.receipts:
        asyncio.run(_cmd_receipts(
            failed_only=args.failed,
            as_json=args.output_json,
            run_reaper=args.reap,
            include_claimed=getattr(args, "include_claimed", False),
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
            print(
                "Reset complete. Run interactively to reconfigure, or set "
                "values in ~/.spacerouter/settings.json before next launch.",
                flush=True,
            )
        return

    # --setup explicitly requested but no TTY: refuse with a clear error
    # rather than silently falling through to a default daemon start
    # (Phase A finding #11). The detect-and-auto-launch wizard path below
    # is unaffected; only the explicit flag is hard-stopped.
    if args.setup and not sys.stdin.isatty():
        print(
            "Error: --setup requires a TTY (interactive shell).\n"
            "  - To configure non-interactively, edit ~/.spacerouter/settings.json directly.\n"
            "  - Or pass values via flags: --staking-address, --port, --label, --log-level, etc.\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(2)

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
