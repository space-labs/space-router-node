"""Backwards-compatibility shim between v1.5 ``settings.json`` and the
old ``SR_*`` env-driven Pydantic ``Settings`` class.

History
-------
Pre-v1.5 the daemon read every config field from a flat
``pydantic_settings.BaseSettings`` whose values came from
``os.environ`` (with an ``SR_`` prefix) or a ``.env`` /
``spacerouter.env`` file. With Track P0 of the v1.5 stabilization
plan, the canonical store moved to ``~/.spacerouter/settings.json``
(see :py:mod:`app.settings_v2`, :py:mod:`app.settings_loader`).

This module is deliberately retained because **50+ call sites** still
read config via ``Settings`` instances (``s.NODE_PORT``,
``s.IDENTITY_KEY_PATH`` etc.). Rewriting every consumer would be a
high-risk multi-PR effort. Instead, :py:func:`load_settings` now
constructs an old-shape ``Settings`` instance from the new schema +
``config_dir()`` so existing consumers keep working unchanged.

What changed
------------
1. ``load_settings()`` no longer reads env vars directly. It calls
   :py:func:`app.settings_loader.load_provider_settings` (which runs
   the legacy macOS migrator + ``spacerouter.env`` migrator + cold
   env-var fallback) and maps the result onto the old ``Settings``
   field names.

2. Path fields (``IDENTITY_KEY_PATH``, ``TLS_CERT_PATH``,
   ``TLS_KEY_PATH``, ``GATEWAY_CA_CERT_PATH``,
   ``RECEIPT_STORE_PATH``) are recomputed from
   :py:func:`app.paths.config_dir` and *overwrite* whatever the
   Pydantic env-source resolved. This is the bugfix for the
   v1.5.0-test.80 regression where files landed at relative
   ``certs/...`` (resolved from CWD, sometimes the PyInstaller temp
   dir) instead of ``~/.spacerouter/certs/...``. Tests that need a
   tmp-path layout continue to construct ``Settings(**kwargs)``
   directly with explicit absolute paths — that path is unchanged.

3. The ``IDENTITY_PASSPHRASE`` is the only secret left in env.
   ``settings.json`` only persists ``identity_passphrase_set: bool``;
   the actual passphrase must come from ``SR_IDENTITY_PASSPHRASE`` at
   runtime. That guarantee predates v1.5 and is unchanged.

4. First-run write: when ``settings.json`` does not exist, the loader
   already persists defaults (or env-seeded values) to disk in
   :py:func:`app.settings_loader.load_provider_settings`. We rely on
   that — no extra disk write happens here.

5. The ``Settings`` Pydantic class remains importable with the same
   field set so tests, ``Settings(**kwargs)``, and any other direct
   constructions keep working. Its ``env_prefix`` is preserved for
   the same reason. ``load_settings()``, however, no longer goes
   through the Pydantic env-var resolution path — it builds the
   instance from the schema-validated ``settings.json`` data.
"""

from __future__ import annotations

import logging
import os
import warnings

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.variant import BUILD_VARIANT

logger = logging.getLogger(__name__)

_PROD_URL = "https://spacerouter-coordination-api.fly.dev"
_TEST_URL = "https://spacerouter-coordination-api-test.fly.dev"


def _default_coordination_url() -> str:
    """Return the default coordination API URL for the current build variant."""
    if BUILD_VARIANT == "test":
        return _TEST_URL
    return _PROD_URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SR_",
        env_file=".env",
        populate_by_name=True,
        extra="ignore",  # Tolerate env vars from removed config fields (e.g. SETTLEMENT_*)
    )

    NODE_PORT: int = 9090
    COORDINATION_API_URL: str = _default_coordination_url()

    # Max concurrent proxy connections (DoS protection)
    MAX_CONNECTIONS: int = 256

    # Bind address — restrict to specific interface if needed
    BIND_ADDRESS: str = "0.0.0.0"

    NODE_LABEL: str = ""
    REFERRAL_CODE: str = ""

    PUBLIC_IP: str = ""  # Auto-detected if empty
    PUBLIC_PORT: int = 0  # Override advertised port (0 = use NODE_PORT)

    # Wallet addresses
    # AliasChoices: accept SR_WALLET_ADDRESS (v0.1.2 name) as well as SR_STAKING_ADDRESS.
    # populate_by_name=True lets tests still pass STAKING_ADDRESS= as a kwarg.
    STAKING_ADDRESS: str = Field(
        default="",
        validation_alias=AliasChoices("SR_STAKING_ADDRESS", "SR_WALLET_ADDRESS"),
    )
    COLLECTION_ADDRESS: str = ""    # Collection wallet; if empty, falls back to staking address

    # v0.2.0 registration mode
    REGISTRATION_MODE: str = "auto"  # "v1" (v0.1.2) | "v2" (v0.2.0) | "auto"

    # UPnP / NAT-PMP automatic port forwarding
    UPNP_ENABLED: bool = True
    UPNP_LEASE_DURATION: int = 3600  # seconds; 0 = permanent

    BUFFER_SIZE: int = 65536
    REQUEST_TIMEOUT: float = 30.0
    RELAY_TIMEOUT: float = 300.0

    LOG_LEVEL: str = "INFO"

    # Registration retry limits
    REGISTER_MAX_RETRIES: int = 5

    # Node identity keypair (auto-generated secp256k1 for signing API requests).
    # NOTE — these *_PATH fields are computed from app.paths.config_dir()
    # by load_settings() and overwritten there. The defaults below are
    # placeholders for direct ``Settings(**kwargs)`` constructor calls in
    # tests; production code should never read them as-is. ``SR_*_PATH``
    # env vars CAN still seed these fields when constructing via the
    # legacy Pydantic env path, but :py:func:`load_settings` always wins
    # over that and re-derives absolute paths from ``config_dir()``, so
    # the daemon never lands files under a relative ``certs/`` dir.
    IDENTITY_KEY_PATH: str = "certs/node-identity.key"
    IDENTITY_PASSPHRASE: str = ""   # If set, encrypt identity key with Web3 keystore JSON

    # TLS — auto-generates a self-signed cert if files don't exist
    TLS_CERT_PATH: str = "certs/node.crt"
    TLS_KEY_PATH: str = "certs/node.key"

    # mTLS — Gateway authentication (requires gateway_ca_cert from registration)
    MTLS_ENABLED: bool = True
    GATEWAY_CA_CERT_PATH: str = "certs/gateway-ca.crt"

    # ── Payment (v1.5.0) ────────────────────────────────────────────
    # Leg 2 receipt exchange with Gateway after relay. Provider generates the receipt,
    # Gateway EIP-712 signs it, Provider stores it locally and later submits claimBatch()
    # on-chain to settle. See app/payment/receipt_store.py.
    PAYMENT_ENABLED: bool = False
    NODE_RATE_PER_GB: int = 0               # Price per GB in token's smallest unit
    NODE_IDENTITY_ADDRESS: str = ""         # EVM address, zero-padded to bytes32 for receipts

    # Path to the SQLite file holding gateway-signed Leg 2 receipts until claimed.
    # Auto-derived from config_dir() in load_settings(); see *_PATH note above.
    RECEIPT_STORE_PATH: str = "~/.spacerouter/receipts.db"

    # On-chain settlement — only needed for the `claim` CLI command, not the relay path.
    ESCROW_CONTRACT_ADDRESS: str = ""       # TokenPaymentEscrow proxy address
    ESCROW_CHAIN_RPC: str = ""              # Creditcoin RPC endpoint
    ESCROW_CHAIN_ID: int = 102031           # Creditcoin testnet
    GATEWAY_PAYER_ADDRESS: str = ""         # Gateway's EOA — payer in escrow
    CLAIM_BATCH_SIZE: int = 50              # Max receipts per claimBatch tx

    # ── Auto-claim (P10) ──────────────────────────────────────────────
    # Optional background monitor that fires claim_all() when claimable
    # receipts cross either threshold (OR semantics). Default OFF — operators
    # opt in via settings.json. See app/payment/auto_claim.py.
    AUTO_CLAIM_ENABLED: bool = False
    # Stored as str to survive >2^53 wei amounts JSON-side; coerced to int
    # at consumption time inside the monitor.
    AUTO_CLAIM_THRESHOLD_SPACE_WEI: str = "10000000000000000000"  # 10 SPACE
    AUTO_CLAIM_THRESHOLD_COUNT: int = 10

    @field_validator("REGISTRATION_MODE")
    @classmethod
    def _validate_registration_mode(cls, v: str) -> str:
        allowed = ("v1", "v2", "auto")
        if v not in allowed:
            raise ValueError(f"REGISTRATION_MODE must be one of {allowed}, got {v!r}")
        return v


def _coerce_int(value: object, *, default: int = 0) -> int:
    """Tolerant int parser: accept ``str`` (wei), ``int``, ``None``."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _settings_from_provider_settings(new) -> Settings:
    """Build the legacy ``Settings`` shape from a v1.5 ``settings_v2.Settings``.

    Path fields are derived from :py:func:`app.paths.config_dir` so the
    daemon never lands files under a relative ``certs/`` directory again
    (the v1.5.0-test.80 bug).
    """
    from app.paths import config_dir

    cfg = config_dir()
    certs = cfg / "certs"
    identity_key = certs / "node-identity.key"
    tls_cert = certs / "node.crt"
    tls_key = certs / "node.key"
    gateway_ca = certs / "gateway-ca.crt"
    receipts_db = cfg / "receipts.db"

    # IDENTITY_PASSPHRASE remains env-only — the new schema only stores
    # a boolean indicating whether one is set. Keep that contract.
    passphrase = os.environ.get("SR_IDENTITY_PASSPHRASE", "")

    # ``_env_file=None`` skips pydantic-settings' default ``.env`` probe.
    # Every field below is set explicitly from the v1.5 provider settings,
    # so there is nothing left for a .env to override. Probing the cwd
    # also crashed under sudo from /root: pathlib.is_file('.env') raised
    # PermissionError when /root was 0700, taking out --reset on Linux
    # boxes installed via the .deb (Phase A E2E finding).
    return Settings(
        _env_file=None,
        NODE_PORT=new.node.port,
        COORDINATION_API_URL=new.coordination.url,
        NODE_LABEL=new.node.label or "",
        REFERRAL_CODE=new.node.referral_code or "",
        PUBLIC_IP=new.node.public_ip or "",
        PUBLIC_PORT=new.node.public_port or 0,
        STAKING_ADDRESS=new.wallet.staking_address or "",
        COLLECTION_ADDRESS=new.wallet.collection_address or "",
        REGISTRATION_MODE=new.node.registration_mode,
        UPNP_ENABLED=new.node.upnp_enabled,
        LOG_LEVEL=new.node.log_level,
        IDENTITY_KEY_PATH=str(identity_key),
        IDENTITY_PASSPHRASE=passphrase,
        TLS_CERT_PATH=str(tls_cert),
        TLS_KEY_PATH=str(tls_key),
        MTLS_ENABLED=new.node.mtls_enabled,
        GATEWAY_CA_CERT_PATH=str(gateway_ca),
        # Payment / escrow
        PAYMENT_ENABLED=new.escrow.enabled,
        NODE_RATE_PER_GB=_coerce_int(new.escrow.leg2_rate_per_gb, default=0),
        RECEIPT_STORE_PATH=str(receipts_db),
        ESCROW_CONTRACT_ADDRESS=new.escrow.contract_address or "",
        ESCROW_CHAIN_RPC=new.escrow.chain_rpc or "",
        ESCROW_CHAIN_ID=_coerce_int(new.escrow.chain_id, default=102031),
        GATEWAY_PAYER_ADDRESS=new.escrow.gateway_payer_address or "",
        CLAIM_BATCH_SIZE=new.claim.batch_size,
        AUTO_CLAIM_ENABLED=new.claim.auto_claim_enabled,
        AUTO_CLAIM_THRESHOLD_SPACE_WEI=new.claim.auto_claim_threshold_space_wei,
        AUTO_CLAIM_THRESHOLD_COUNT=new.claim.auto_claim_threshold_count,
    )


def load_settings() -> Settings:
    """Build a legacy-shape ``Settings`` from the v1.5 ``settings.json``.

    Resolution chain (delegated to
    :py:func:`app.settings_loader.load_provider_settings`):

    1. ``~/.spacerouter/settings.json`` → load.
    2. Else ``~/.spacerouter/spacerouter.env`` → migrate, save, load.
    3. Else cold start with ``SR_*`` env vars → save, load.
    4. Else defaults (no file written until something gets configured).

    Whatever that chain resolves is then overlaid with any ``SR_*`` value
    still live in ``os.environ`` — see
    :py:func:`app.settings_loader.apply_env_overrides`. That is what makes
    explicit CLI flags (which ``app.main._apply_cli_args`` exports as
    ``SR_*``) and operator env vars beat a populated ``settings.json``
    instead of being silently ignored. The overlay is in-memory only; the
    callers that write ``settings.json`` back (the network-mode flags, the
    escrow TOFU sync) deliberately read the un-overlaid values.

    The legacy macOS Application-Support migration also runs as part of
    that chain so v1.4 macOS installs are picked up before any read.

    If the loader returns a Settings whose ``settings.json`` did NOT
    exist on disk before the call but does now, we log so operators can
    see the cold-start happened.
    """
    from app.settings_loader import (
        apply_env_overrides, load_provider_settings, settings_path,
    )

    s_path = settings_path()
    pre_existed = s_path.exists()

    new = apply_env_overrides(load_provider_settings())
    s = _settings_from_provider_settings(new)

    if not pre_existed and s_path.exists():
        logger.info("settings.json created at %s (cold start)", s_path)

    if not s.COORDINATION_API_URL.startswith("https://"):
        if "localhost" not in s.COORDINATION_API_URL and "127.0.0.1" not in s.COORDINATION_API_URL:
            warnings.warn(
                f"COORDINATION_API_URL uses plain HTTP ({s.COORDINATION_API_URL}). "
                "This exposes registration data to MITM attacks. Use HTTPS in production.",
                stacklevel=2,
            )
    return s


# Removed eager module-level ``settings = load_settings()`` — it triggered
# pydantic initialization at import time, adding seconds to CLI startup.
# All callers should use ``load_settings()`` for a fresh instance.
