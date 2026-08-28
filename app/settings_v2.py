"""Canonical provider settings stored in ``~/.spacerouter/settings.json``.

This module is the foundation of the v1.5 stabilization plan (Track P0).
All provider configuration moves out of scattered ``SR_*`` env vars and
the GUI-managed ``spacerouter.env`` file into a single canonical JSON
document with a stable, versioned schema.

Wei amounts are stored as **strings** to avoid JavaScript
``Number.MAX_SAFE_INTEGER`` rounding issues — same convention used by
the gateway and SDK.

Migration entry point: :py:meth:`Settings.migrate_from_env_file`.

The macOS ``SR_BUILD_VARIANT`` env-var fragility (root cause of the
"Node ID rotates every restart" bug — see PR #68 / Section 13 of the
v1.5 plan) is fixed here for free: ``build_variant`` becomes a regular
persisted field, not an env-var lookup.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# SR_* env vars that earlier versions of the daemon read but v1.5 no
# longer needs. We log these at DEBUG instead of INFO so a v1.4 → v1.5
# .deb upgrade doesn't paper its journal with "unknown key" warnings
# for benign carry-over values. Add new entries here when retiring an
# env var so the operator's spacerouter.env doesn't need pruning by
# hand. See Phase A finding #4.
_DEPRECATED_ENV_KEYS: frozenset[str] = frozenset({
    # macOS Node-ID rotation fix (PR #68): variant now lives in
    # settings.json, not the environment.
    "SR_BUILD_VARIANT",
    # Receipt store path is derived from config_dir() now; the override
    # was always advisory and is no longer honoured.
    "SR_RECEIPT_STORE_PATH",
    # Pre-v1.5 development variant flag.
    "SR_API_VARIANT",
})


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _validate_evm_address(value: str | None) -> str | None:
    """Pydantic validator helper — defer to ``app.wallet`` for the rules.

    Imported lazily inside the function body to avoid module-import cycles
    (``app.wallet`` is light today but the rule keeps it that way as the
    codebase grows). Returns the canonical lowercase ``0x``-prefixed form.
    Raises ``ValueError`` for invalid addresses; Pydantic surfaces that as
    a structured validation error.
    """
    if value is None or value == "":
        return value
    from app.wallet import validate_wallet_address
    return validate_wallet_address(value)


def _validate_http_url(value: str | None) -> str | None:
    """Reject anything that doesn't start with http:// or https://."""
    if value is None or value == "":
        return value
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError(
            f"URL must start with http:// or https://, got {value!r}"
        )
    return value


class NodeSection(_Section):
    label: str | None = None
    port: int = 9090
    public_ip: str | None = None
    public_port: int | None = None
    upnp_enabled: bool = True
    mtls_enabled: bool = True
    log_level: str = "INFO"
    registration_mode: str = "auto"
    referral_code: str | None = None


class WalletSection(_Section):
    staking_address: str | None = None
    collection_address: str | None = None
    # rc.6 MIN-4: pre-rc.6 default was wrong (~/.spacerouter/identity.key),
    # but the on-disk keystore actually lives at
    # ~/.spacerouter/certs/node-identity.key. The mismatch broke
    # _reconcile_passphrase_flag_in_place — it always concluded the
    # keystore was missing and flipped identity_passphrase_set to False.
    settlement_key_path: str = "~/.spacerouter/certs/node-identity.key"
    identity_passphrase_set: bool = False

    @field_validator("staking_address", "collection_address")
    @classmethod
    def _check_addr(cls, v: str | None) -> str | None:
        return _validate_evm_address(v)


def _default_coord_url() -> str:
    """Pick the right coord URL for the current build variant.

    Production builds default to the public coordination URL; test builds
    default to the test fly.dev hostname. Other variants (dev/staging) get
    the public URL too — easier to override via settings.json than to
    silently route a fresh install to test.
    """
    try:
        from app._build_variant import BUILD_VARIANT  # type: ignore[import-not-found]
    except ImportError:
        BUILD_VARIANT = "production"
    if BUILD_VARIANT == "test":
        return "https://spacerouter-coordination-api-test.fly.dev"
    return "https://coordination.spacerouter.org"


class CoordinationSection(_Section):
    url: str = Field(default_factory=_default_coord_url)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        return _validate_http_url(v)


class EscrowSection(_Section):
    enabled: bool = False
    contract_address: str | None = None
    chain_rpc: str | None = None
    chain_id: int | None = None
    gateway_payer_address: str | None = None
    leg2_rate_per_gb: str | None = None  # wei as string
    synced_from_coord_at: str | None = None  # ISO8601

    @field_validator("contract_address", "gateway_payer_address")
    @classmethod
    def _check_addr(cls, v: str | None) -> str | None:
        return _validate_evm_address(v)

    @field_validator("chain_rpc")
    @classmethod
    def _check_rpc(cls, v: str | None) -> str | None:
        return _validate_http_url(v)


class ClaimSection(_Section):
    auto_claim_enabled: bool = False
    auto_claim_threshold_space_wei: str = "10000000000000000000"  # 10 SPACE
    auto_claim_threshold_count: int = 10
    batch_size: int = 50


class ReceiptsSection(_Section):
    max_sign_attempts: int = 2
    max_claim_attempts: int = 2
    reaper_grace_seconds: int = 300
    reaper_interval_seconds: int = 300


SECTION_MODELS: dict[str, type[_Section]] = {
    "node": NodeSection,
    "wallet": WalletSection,
    "coordination": CoordinationSection,
    "escrow": EscrowSection,
    "claim": ClaimSection,
    "receipts": ReceiptsSection,
}


def _seed_build_variant() -> str:
    """Read the build-variant seed from the frozen-build helper if present.

    Production binaries are stamped at CI build time via ``app/_build_variant.py``;
    that value seeds a fresh ``settings.json``. We deliberately do **not** read
    ``os.environ['SR_BUILD_VARIANT']`` here — that's the bug we're fixing.
    """
    try:
        from app._build_variant import BUILD_VARIANT  # type: ignore[import-not-found]
        return BUILD_VARIANT
    except ImportError:
        return "production"


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    build_variant: str = Field(default_factory=_seed_build_variant)
    node: NodeSection = Field(default_factory=NodeSection)
    wallet: WalletSection = Field(default_factory=WalletSection)
    coordination: CoordinationSection = Field(default_factory=CoordinationSection)
    escrow: EscrowSection = Field(default_factory=EscrowSection)
    claim: ClaimSection = Field(default_factory=ClaimSection)
    receipts: ReceiptsSection = Field(default_factory=ReceiptsSection)

    # ── Cross-field validation ───────────────────────────────────────

    @model_validator(mode="after")
    def _enforce_https_outside_test(self) -> "Settings":
        """Production / staging builds must not talk to plaintext HTTP.

        On test builds we tolerate ``http://`` so QA can point at a local
        coordination API or RPC node without rolling certs. Any other
        variant ("production", "staging", anything custom) requires
        ``https://`` for both the coordination API and the chain RPC.

        Empty values are allowed — those are "not set yet" and get
        caught later by the wizard / pre-flight checks.
        """
        if self.build_variant == "test":
            return self
        from urllib.parse import urlparse
        # Loopback addresses are exempt — no MITM possible on local interface;
        # required for production-build CI smoke tests against a localhost mock.
        _LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
        for label, value in (
            ("coordination.url", self.coordination.url),
            ("escrow.chain_rpc", self.escrow.chain_rpc),
        ):
            if not value or value.startswith("https://"):
                continue
            host = (urlparse(value).hostname or "").lower()
            if host in _LOOPBACK:
                continue
            raise ValueError(
                f"{label} must use https:// on build_variant={self.build_variant!r} "
                f"(got {value!r}). Plaintext is only allowed in test builds or on loopback."
            )
        return self

    # ── Load / save ──────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Load settings from *path*, or return defaults if the file is missing.

        We deliberately do NOT auto-create the file here — first-run / wizard
        flows are responsible for creating ``settings.json``. This keeps load()
        side-effect-free.

        On JSON parse or schema-validation failure, raise with a helpful
        message naming the bad field(s).
        """
        if not path.exists():
            return cls()

        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"settings.json at {path} is not valid JSON: {e.msg} "
                f"(line {e.lineno}, column {e.colno})"
            ) from e

        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            # Build a compact "field: message" summary
            issues = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            )
            raise ValueError(
                f"settings.json at {path} failed validation: {issues}"
            ) from e

    def save(self, path: Path) -> None:
        """Atomic write: write to ``<path>.tmp``, then ``os.replace``.

        Sets 0600 on POSIX; ``Path.chmod`` is a no-op on Windows so the
        same code is safe cross-platform.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # ``mode='json'`` so wei strings stay strings; pretty-print for human edits.
        tmp.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=False) + "\n"
        )
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except (OSError, NotImplementedError):
            # Windows / read-only-mounted fs — best effort.
            pass

    # ── Migration from spacerouter.env ───────────────────────────────

    @classmethod
    def migrate_from_env_file(
        cls,
        env_path: Path,
        settings_path: Path,
        *,
        rename_after: bool = True,
    ) -> "Settings":
        """Migrate a legacy ``spacerouter.env`` into a fresh ``settings.json``.

        Behavior:

        * If ``settings.json`` already exists, do nothing — return the loaded
          one. Migration is idempotent.
        * If ``spacerouter.env`` exists but ``settings.json`` does not, parse
          the env file, map ``SR_*`` keys to schema fields, save the new
          ``settings.json``. When *rename_after* is true (daemon path), the
          env file is renamed to ``spacerouter.env.migrated.bak`` so we never
          re-migrate. The GUI path passes ``rename_after=False`` because the
          GUI still writes to the env file during the transition release.
        * If neither exists, return defaults.
        """
        if settings_path.exists():
            return cls.load(settings_path)

        if not env_path.exists():
            return cls()

        env_values = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        settings = cls.from_env_mapping(env_values)
        settings.save(settings_path)

        if rename_after:
            # Rename the env file to a backup so we never re-migrate. If the
            # .bak already exists (shouldn't, but be defensive), leave it;
            # ``os.replace`` overwrites atomically across platforms.
            bak = env_path.with_name(env_path.name + ".migrated.bak")
            try:
                os.replace(env_path, bak)
            except OSError as e:
                logger.warning(
                    "Could not rename %s → %s after migration: %s",
                    env_path,
                    bak,
                    e,
                )
            logger.info(
                "Migrated provider config from %s → %s (backup: %s)",
                env_path,
                settings_path,
                bak,
            )
        else:
            logger.info(
                "Seeded settings.json from %s (env file kept; GUI continues to write it)",
                env_path,
            )
        return settings

    # ── Env-mapping (used by migrate_from_env_file and config-fallback) ──

    @classmethod
    def from_env_mapping(cls, env: dict[str, str]) -> "Settings":
        """Build a Settings from a dict of ``SR_*`` keys (whatever shape)."""
        fields = cls.env_section_fields(env)
        kwargs: dict[str, Any] = {}
        build_variant = fields.pop("build_variant", None)
        if build_variant is not None:
            kwargs["build_variant"] = build_variant
        for name, section_model in SECTION_MODELS.items():
            if name in fields:
                kwargs[name] = section_model(**fields[name])
        return cls(**kwargs)

    @classmethod
    def env_section_fields(
        cls, env: dict[str, str], *, report_unknown: bool = True
    ) -> dict[str, Any]:
        """Map ``SR_*`` keys onto ``{section_name: {field: value}}``.

        Only keys actually present in *env* appear in the result, which is
        what lets a caller tell an explicit override apart from a schema
        default. ``build_variant`` comes back as a top-level string. Values
        are type-parsed but not schema-validated — validation happens when
        the caller feeds them to the section models.

        Unknown keys are logged at INFO and dropped — clean slate. The
        mapping table mirrors Section 9 of the v1.5 plan; non-trivial cases
        are commented inline. Callers that re-run this on every settings
        read pass ``report_unknown=False`` to keep the log quiet.
        """
        node: dict[str, Any] = {}
        wallet: dict[str, Any] = {}
        coordination: dict[str, Any] = {}
        escrow: dict[str, Any] = {}
        claim: dict[str, Any] = {}
        receipts: dict[str, Any] = {}
        build_variant: str | None = None

        # Used so we can warn about unknown keys in one consolidated log line.
        consumed: set[str] = set()

        def take(key: str) -> str | None:
            v = env.get(key)
            if v is None:
                return None
            consumed.add(key)
            v = v.strip()
            return v if v != "" else None

        # ── build_variant (the macOS rotation fix) ───────────────────
        bv = take("SR_BUILD_VARIANT")
        if bv:
            build_variant = bv

        # ── node ─────────────────────────────────────────────────────
        if (v := take("SR_NODE_PORT")) is not None:
            node["port"] = int(v)
        if (v := take("SR_NODE_LABEL")) is not None:
            node["label"] = v
        if (v := take("SR_PUBLIC_IP")) is not None:
            node["public_ip"] = v
        if (v := take("SR_PUBLIC_PORT")) is not None:
            try:
                node["public_port"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_PUBLIC_PORT=%r", v)
        if (v := take("SR_UPNP_ENABLED")) is not None:
            node["upnp_enabled"] = _parse_bool(v)
        if (v := take("SR_MTLS_ENABLED")) is not None:
            node["mtls_enabled"] = _parse_bool(v)
        if (v := take("SR_LOG_LEVEL")) is not None:
            node["log_level"] = v
        if (v := take("SR_REGISTRATION_MODE")) is not None:
            node["registration_mode"] = v
        if (v := take("SR_REFERRAL_CODE")) is not None:
            node["referral_code"] = v

        # ── wallet ───────────────────────────────────────────────────
        # Accept SR_WALLET_ADDRESS as a back-compat alias for
        # SR_STAKING_ADDRESS — v1.4 .deb / .rpm installs ship that name
        # in /etc/spacerouter/spacerouter.env and we want apt upgrade
        # to "just work" without the operator hand-editing the file.
        # Mirrors the AliasChoices pair already wired up in app/config.py.
        if (v := take("SR_STAKING_ADDRESS")) is not None:
            wallet["staking_address"] = v
        elif (v := take("SR_WALLET_ADDRESS")) is not None:
            wallet["staking_address"] = v
        if (v := take("SR_COLLECTION_ADDRESS")) is not None:
            wallet["collection_address"] = v
        if (v := take("SR_IDENTITY_KEY_PATH")) is not None:
            wallet["settlement_key_path"] = v
        # Passphrase is NEVER persisted into settings.json — only the boolean.
        passphrase = take("SR_IDENTITY_PASSPHRASE")
        if passphrase:
            wallet["identity_passphrase_set"] = True

        # ── coordination ─────────────────────────────────────────────
        if (v := take("SR_COORDINATION_API_URL")) is not None:
            coordination["url"] = v

        # ── escrow ───────────────────────────────────────────────────
        if (v := take("SR_PAYMENT_ENABLED")) is not None:
            escrow["enabled"] = _parse_bool(v)
        if (v := take("SR_ESCROW_CONTRACT_ADDRESS")) is not None:
            escrow["contract_address"] = v
        if (v := take("SR_ESCROW_CHAIN_RPC")) is not None:
            escrow["chain_rpc"] = v
        if (v := take("SR_ESCROW_CHAIN_ID")) is not None:
            try:
                escrow["chain_id"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_ESCROW_CHAIN_ID=%r", v)
        if (v := take("SR_GATEWAY_PAYER_ADDRESS")) is not None:
            escrow["gateway_payer_address"] = v
        # Renamed: provider's old "local guess" → gateway-canonical rate.
        if (v := take("SR_NODE_RATE_PER_GB")) is not None:
            escrow["leg2_rate_per_gb"] = v

        # ── claim ────────────────────────────────────────────────────
        if (v := take("SR_CLAIM_BATCH_SIZE")) is not None:
            try:
                claim["batch_size"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_CLAIM_BATCH_SIZE=%r", v)

        # ── receipts ─────────────────────────────────────────────────
        if (v := take("SR_RECEIPT_REAPER_GRACE_SECONDS")) is not None:
            try:
                receipts["reaper_grace_seconds"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_REAPER_GRACE_SECONDS=%r", v)
        if (v := take("SR_RECEIPT_REAPER_INTERVAL_SECONDS")) is not None:
            try:
                receipts["reaper_interval_seconds"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_REAPER_INTERVAL_SECONDS=%r", v)
        if (v := take("SR_RECEIPT_MAX_SIGN_ATTEMPTS")) is not None:
            try:
                receipts["max_sign_attempts"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_MAX_SIGN_ATTEMPTS=%r", v)
        if (v := take("SR_RECEIPT_MAX_CLAIM_ATTEMPTS")) is not None:
            try:
                receipts["max_claim_attempts"] = int(v)
            except ValueError:
                logger.info("ignoring non-integer SR_RECEIPT_MAX_CLAIM_ATTEMPTS=%r", v)

        # ── unknown-key sweep ────────────────────────────────────────
        # Known-deprecated keys log at DEBUG (no longer noise on every
        # upgrade), truly-unrecognised keys stay at INFO so a typo in a
        # hand-edited env file is still visible. The old single-line
        # "ignoring unknown key X" was alarming enough that real users
        # mistook benign migration leftovers for a config problem
        # (Phase A finding #4).
        if report_unknown:
            unknown = [k for k in env if k.startswith("SR_") and k not in consumed]
            for k in unknown:
                if k in _DEPRECATED_ENV_KEYS:
                    logger.debug(
                        "settings: dropping deprecated env key %s (no longer used)", k
                    )
                else:
                    logger.info(
                        "settings: ignoring unrecognised env key %s "
                        "(typo, or moved to settings.json)",
                        k,
                    )

        fields: dict[str, Any] = {}
        if build_variant is not None:
            fields["build_variant"] = build_variant
        for name, values in (
            ("node", node),
            ("wallet", wallet),
            ("coordination", coordination),
            ("escrow", escrow),
            ("claim", claim),
            ("receipts", receipts),
        ):
            if values:
                fields[name] = values

        return fields


def _parse_bool(v: str) -> bool:
    """Lenient bool parser for env-string values (``"true"`` / ``"1"`` / etc)."""
    return str(v).strip().lower() in ("1", "true", "yes", "on")
