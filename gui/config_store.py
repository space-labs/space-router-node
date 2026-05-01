"""Persistent configuration storage for the SpaceRouter GUI.

Reads / writes ``~/.spacerouter/settings.json`` (the v1.5 canonical
config store; see ``app/settings_v2.py``). The legacy
``spacerouter.env`` file path is still recognised for one-shot
migration of pre-v1.5 installs but is never written to from this
module.

External callers (`gui/api.py`, etc.) keep using the SR_-prefixed
key names they always have — `get("SR_STAKING_ADDRESS")`,
`save_wallets(...)`, etc. — and a small translation layer maps those
keys onto the structured settings.json fields under the hood.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.identity import write_identity_key
from app.settings_v2 import Settings as _SettingsV2
from app.variant import BUILD_VARIANT
from app.wallet import validate_wallet_address

logger = logging.getLogger(__name__)

# Coordination API URLs per environment
_PROD_URL = "https://spacerouter-coordination-api.fly.dev"
_TEST_URL = "https://spacerouter-coordination-api-test.fly.dev"
_STAGING_URL = "https://spacerouter-coordination-api-staging.fly.dev"

# TokenPaymentEscrow deployment on Creditcoin testnet (CC3, chainId 102031).
# Baked in so QA and test-variant users don't have to hand-edit the env
# file — which risked being wiped by Fresh Restart and was the v1.5 QA
# footgun ("Payment/Escrow settings manually added to env are deleted on
# restart"). Mainnet escrow is not yet deployed; prod variant leaves the
# fields empty so operators configure them explicitly at rollout time.
_TEST_ESCROW_CONTRACT = "0xC5740e4e9175301a24FB6d22bA184b8ec0762852"
_TEST_ESCROW_CHAIN_RPC = "https://rpc.cc3-testnet.creditcoin.network"
_TEST_ESCROW_CHAIN_ID = "102031"

# Pre-configured environments for easy switching (test builds only)
ENVIRONMENTS = {
    "production": {
        "label": "Production",
        "url": _PROD_URL,
    },
    "test": {
        "label": "Test (CC Testnet)",
        "url": _TEST_URL,
    },
    "staging": {
        "label": "Staging",
        "url": _STAGING_URL,
    },
    "local": {
        "label": "Local",
        "url": "http://localhost:8000",
    },
}


def _default_coordination_url() -> str:
    """Return the default coordination API URL for the current build variant.

    Test builds target the test environment; production builds target prod.
    """
    if BUILD_VARIANT == "test":
        return _TEST_URL
    return _PROD_URL


def _default_escrow_contract() -> str:
    return _TEST_ESCROW_CONTRACT if BUILD_VARIANT == "test" else ""


def _default_escrow_chain_rpc() -> str:
    return _TEST_ESCROW_CHAIN_RPC if BUILD_VARIANT == "test" else ""


def _default_escrow_chain_id() -> str:
    return _TEST_ESCROW_CHAIN_ID if BUILD_VARIANT == "test" else ""


def _default_payment_enabled() -> str:
    # Test variant opts in by default — the alternative is "fresh test
    # builds silently never sign Leg 2 receipts because escrow is off,"
    # which is the bug shipped in test.95. Prod stays opt-in until the
    # mainnet escrow rollout flips this.
    return "true" if BUILD_VARIANT == "test" else "false"


_DEFAULTS = {
    "SR_COORDINATION_API_URL": _default_coordination_url(),
    "SR_STAKING_ADDRESS": "",
    "SR_COLLECTION_ADDRESS": "",
    "SR_NODE_PORT": "9090",
    "SR_UPNP_ENABLED": "true",
    "SR_PUBLIC_IP": "",
    "SR_PUBLIC_PORT": "",
    "SR_MTLS_ENABLED": "true",
    "SR_LOG_LEVEL": "INFO",
    "SR_REGISTRATION_MODE": "auto",
    "SR_IDENTITY_PASSPHRASE": "",
    # Escrow settings — test variant ships with testnet defaults so QA
    # never has to hand-edit; Fresh Restart preserves them because they
    # live in _DEFAULTS now. Prod leaves them empty (operator-configured).
    # NOTE: leg2_rate_per_gb is INTENTIONALLY not seeded here. The
    # canonical rate lives on the gateway's /config endpoint; the daemon's
    # TOFU sync at boot fetches it. Pre-seeding a placeholder rate
    # caused test.101's SIGN_REJECTED_UNKNOWN_REQUEST regression — the
    # bootstrap value (1e15) was 500,000× too low vs the gateway's
    # canonical 5e20 wei/GB.
    "SR_PAYMENT_ENABLED": _default_payment_enabled(),
    "SR_ESCROW_CONTRACT_ADDRESS": _default_escrow_contract(),
    "SR_ESCROW_CHAIN_RPC": _default_escrow_chain_rpc(),
    "SR_ESCROW_CHAIN_ID": _default_escrow_chain_id(),
}


def _config_dir() -> Path:
    from app.paths import config_dir
    return config_dir()


# Mapping from legacy SR_* env-key names (still used by external
# callers in gui/api.py) to (section_name, field_name) tuples in the
# v1.5 settings_v2 schema. Keys without a mapping (notably
# SR_IDENTITY_PASSPHRASE and SR_IDENTITY_KEY_PATH) are handled
# separately because they're either env-only secrets or auto-derived
# from config_dir().
_SR_KEY_TO_FIELD: dict[str, tuple[str, str]] = {
    "SR_COORDINATION_API_URL": ("coordination", "url"),
    "SR_STAKING_ADDRESS": ("wallet", "staking_address"),
    "SR_COLLECTION_ADDRESS": ("wallet", "collection_address"),
    "SR_NODE_PORT": ("node", "port"),
    "SR_UPNP_ENABLED": ("node", "upnp_enabled"),
    "SR_PUBLIC_IP": ("node", "public_ip"),
    "SR_PUBLIC_PORT": ("node", "public_port"),
    "SR_MTLS_ENABLED": ("node", "mtls_enabled"),
    "SR_LOG_LEVEL": ("node", "log_level"),
    "SR_REGISTRATION_MODE": ("node", "registration_mode"),
    "SR_NODE_LABEL": ("node", "label"),
    "SR_REFERRAL_CODE": ("node", "referral_code"),
    "SR_ESCROW_CONTRACT_ADDRESS": ("escrow", "contract_address"),
    "SR_ESCROW_CHAIN_RPC": ("escrow", "chain_rpc"),
    "SR_ESCROW_CHAIN_ID": ("escrow", "chain_id"),
}


def _stringify(value: object) -> str:
    """Translate a settings.json value into the SR_*-shaped string the
    legacy GUI callers expect."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_for_field(value, annot):
    """Coerce a raw GUI input (almost always ``str``) into the type the
    settings_v2 schema expects. Tolerant: empty string → ``None`` for
    optional fields; ``"true"``/``"false"`` (any case) → ``bool``;
    digit strings → ``int`` for int fields.
    """
    # Optional fields show up as a typing.Union/Optional in the model
    # field annotation. Strings that look empty are taken to mean "unset".
    if isinstance(value, str) and value == "":
        return None
    target_str = repr(annot)
    if "bool" in target_str and not isinstance(value, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if "int" in target_str and not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return value


class ConfigStore:
    """Manage settings.json (the v1.5 canonical config store).

    A small SR_* translation layer keeps external callers in
    ``gui/api.py`` working unchanged — they continue to use
    ``get("SR_STAKING_ADDRESS")`` etc. and the store maps those to
    the structured fields under the hood.
    """

    def __init__(self) -> None:
        self._dir = _config_dir()
        self._path = self._dir / "spacerouter.env"  # legacy, migration-only
        self._settings_json_path = self._dir / "settings.json"
        self._ensure_file()
        # Track P0: opportunistic forward-migration. Idempotent — bails
        # immediately if settings.json already exists. Failures are logged
        # but never raised.
        self.migrate_to_settings_json()

    def migrate_to_settings_json(self) -> "object | None":
        """Migrate an existing v1.4 ``spacerouter.env`` to ``settings.json``.

        Idempotent. Returns the loaded :py:class:`app.settings_v2.Settings`
        when something happens, ``None`` when settings.json already exists
        (so callers don't need to special-case the no-op path).

        After migration the env file is renamed to ``.migrated.bak`` so
        we don't keep two sources of truth.
        """
        try:
            return _SettingsV2.migrate_from_env_file(
                self._path,
                self._settings_json_path,
                rename_after=True,
            )
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "settings.json migration skipped due to error: %s", e
            )
            return None

    def _ensure_file(self) -> None:
        """Create config dir; never write a default spacerouter.env.

        Brand-new installs land here with no env file and no settings.json
        — that's fine. The first-run wizard (CLI) or onboarding flow
        (GUI) writes settings.json directly. Operators with an existing
        spacerouter.env from v1.4 still get migrated through
        :py:meth:`migrate_to_settings_json`, which then renames the env
        file to ``.migrated.bak``.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load_settings_v2(self) -> "_SettingsV2":
        """Load settings.json (or a defaults instance if it doesn't exist)."""
        if self._settings_json_path.exists():
            return _SettingsV2.load(self._settings_json_path)
        return _SettingsV2(build_variant=BUILD_VARIANT)

    def _save_settings_v2(self, settings: "_SettingsV2") -> None:
        settings.save(self._settings_json_path)

    def _set_field(self, sr_key: str, value) -> None:
        """Update one settings.json field via the SR_* legacy key alias."""
        mapping = _SR_KEY_TO_FIELD.get(sr_key)
        if mapping is None:
            raise KeyError(f"No settings.json mapping for {sr_key!r}")
        section_name, field_name = mapping
        s = self._load_settings_v2()
        section = getattr(s, section_name)
        # Type-coerce strings → bool/int per the schema field type.
        annot = type(section).model_fields[field_name].annotation
        coerced = _coerce_for_field(value, annot)
        setattr(section, field_name, coerced)
        self._save_settings_v2(s)

    @property
    def path(self) -> Path:
        # External callers occasionally treat this as a file path for
        # set_key()-style writes. Now points at settings.json so any
        # remaining direct writes hit the canonical store. No external
        # caller should be using this any more — flagged for removal.
        return self._settings_json_path

    def load(self) -> dict[str, str | None]:
        """Return all known SR_*-shaped values, derived from settings.json.

        Kept for backwards-compat with ``cs.load()`` callers; new code
        should use ``_load_settings_v2()`` directly.
        """
        s = self._load_settings_v2()
        out: dict[str, str | None] = {}
        for sr_key, (section_name, field_name) in _SR_KEY_TO_FIELD.items():
            section = getattr(s, section_name)
            out[sr_key] = _stringify(getattr(section, field_name))
        return out

    def get(self, key: str, default: str = "") -> str:
        if key in _SR_KEY_TO_FIELD:
            return self.load().get(key) or default
        # Unmapped keys: secrets like SR_IDENTITY_PASSPHRASE live in env;
        # auto-derived paths (SR_IDENTITY_KEY_PATH etc.) come from
        # ``app.config.load_settings()``. For raw env reads we fall
        # through; this also keeps ``cs.get("SR_BUILD_VARIANT")`` working.
        return os.environ.get(key, default)

    def save_wallets(self, staking_address: str, collection_address: str = "") -> tuple[str, str]:
        """Validate and persist staking and collection addresses.

        Returns ``(normalised_staking, normalised_collection)``.
        """
        normalised_staking = validate_wallet_address(staking_address)
        if collection_address.strip():
            normalised_collection = validate_wallet_address(collection_address)
        else:
            normalised_collection = normalised_staking

        s = self._load_settings_v2()
        s.wallet.staking_address = normalised_staking
        s.wallet.collection_address = normalised_collection
        self._save_settings_v2(s)

        return normalised_staking, normalised_collection

    def save_environment(self, env_key: str) -> str:
        """Switch the coordination API URL to the given environment.

        Returns the URL that was set.
        """
        env = ENVIRONMENTS.get(env_key)
        if not env:
            raise ValueError(f"Unknown environment: {env_key}")
        self._set_field("SR_COORDINATION_API_URL", env["url"])
        return env["url"]

    def get_environment(self) -> str:
        """Return the current environment key based on the coordination URL."""
        url = self.get("SR_COORDINATION_API_URL")
        for key, env in ENVIRONMENTS.items():
            if env["url"] == url:
                return key
        return "custom"

    def needs_onboarding(self) -> bool:
        """True if the identity key file has not been created yet."""
        key_path = str(self._dir / "certs" / "node-identity.key")
        return not os.path.isfile(key_path)

    def save_onboarding(
        self,
        passphrase: str = "",
        staking: str = "",
        collection: str = "",
        identity_key_hex: str = "",
    ) -> None:
        """Persist onboarding choices and optionally pre-write an imported identity key.

        - *passphrase*: written to ``os.environ['SR_IDENTITY_PASSPHRASE']``
          (passphrases never land in settings.json — only the boolean
          ``wallet.identity_passphrase_set`` flag does).
        - *staking*: staking wallet address; empty → uses identity address at runtime.
        - *collection*: collection wallet address; empty → uses staking address.
        - *identity_key_hex*: if provided, the raw private key is written to the
          identity key file immediately (encrypted if *passphrase* is set).
        """
        if staking:
            staking = validate_wallet_address(staking)
        if collection:
            collection = validate_wallet_address(collection)

        s = self._load_settings_v2()
        s.wallet.identity_passphrase_set = bool(passphrase)
        if staking:
            s.wallet.staking_address = staking
        if collection:
            s.wallet.collection_address = collection
        self._save_settings_v2(s)

        if passphrase:
            os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase

        if identity_key_hex:
            key_path = str(self._dir / "certs" / "node-identity.key")
            write_identity_key(key_path, identity_key_hex, passphrase)

    def save_settings(self, coordination_api_url: str, mtls_enabled: bool) -> None:
        """Persist advanced settings (coordination API URL and mTLS toggle)."""
        s = self._load_settings_v2()
        s.coordination.url = coordination_api_url
        s.node.mtls_enabled = bool(mtls_enabled)
        self._save_settings_v2(s)

    def save_network_mode(self, mode: str, public_host: str = "", port: str = "") -> None:
        """Persist network mode settings.

        Args:
            mode: 'upnp' or 'tunnel'
            public_host: hostname/IP for tunnel mode (e.g. 'bore.pub')
            port: remote/advertised port for tunnel mode (e.g. '21781').
                  The node always listens on SR_NODE_PORT (9090) locally.
        """
        s = self._load_settings_v2()
        if mode == "upnp":
            s.node.upnp_enabled = True
            s.node.public_ip = None
            s.node.public_port = None
        elif mode == "tunnel":
            s.node.upnp_enabled = False
            s.node.public_ip = public_host or None
            s.node.public_port = int(port) if port else None
        self._save_settings_v2(s)

    def get_network_mode(self) -> dict:
        """Return current network mode settings."""
        upnp = self.get("SR_UPNP_ENABLED", "true").lower() == "true"
        public_ip = self.get("SR_PUBLIC_IP", "")
        public_port = self.get("SR_PUBLIC_PORT", "")
        if upnp:
            return {"mode": "upnp", "public_host": "", "port": ""}
        else:
            return {"mode": "tunnel", "public_host": public_ip, "port": public_port}

    def reset(self) -> None:
        """Fully reset config to defaults, deleting identity key and certificates."""
        import shutil

        from app.paths import wipe_operational_state

        # Delete identity key file
        key_path = str(self._dir / "certs" / "node-identity.key")
        if os.path.isfile(key_path):
            os.remove(key_path)

        # Delete all certificates in the certs directory
        certs_dir = self._dir / "certs"
        if certs_dir.is_dir():
            shutil.rmtree(certs_dir)

        # Wipe operational artefacts — receipts.db, incidents.json,
        # logs/. Reset Node promises a clean slate, but pre-rc.3 only
        # touched settings + identity. Stale failed-claim rows and old
        # incident banners survived a "fresh" restart and confused QA.
        for note in wipe_operational_state(self._dir):
            logger.info("reset: %s", note)

        # Reset settings.json to defaults for the current build variant.
        # Anything previously persisted (wallet, coord URL, etc.) is wiped
        # intentionally — that's what reset promises. We feed _DEFAULTS
        # through from_env_mapping so the test variant's escrow opt-in
        # (PAYMENT_ENABLED + leg2 rate + contract addrs) survives Fresh
        # Restart. test.95 shipped a bare `_SettingsV2()` here, which
        # wiped escrow.enabled to false and left the receipt submitter
        # dead until the user hand-edited settings.json.
        env_defaults = {k: v for k, v in _DEFAULTS.items() if v}
        env_defaults["SR_BUILD_VARIANT"] = BUILD_VARIANT
        defaults = _SettingsV2.from_env_mapping(env_defaults)
        self._save_settings_v2(defaults)

    def apply_to_env(self) -> None:
        """Load all config values into os.environ so pydantic-settings picks them up."""
        for key, value in self.load().items():
            if value:
                os.environ[key] = value

        # Track P0 belt-and-suspenders: ALSO export SR_BUILD_VARIANT from
        # the persisted settings.json (when present). The macOS rotation
        # bug was caused by this env var being unstable across launchers
        # (Finder vs shell). Persisting + re-exporting locks it down for
        # any code path still doing ``os.environ.get("SR_BUILD_VARIANT")``.
        # Once the env-var sweep lands in a future PR, this block goes away.
        try:
            from app.settings_v2 import Settings as _SettingsV2
            if self._settings_json_path.exists():
                bv = _SettingsV2.load(self._settings_json_path).build_variant
                os.environ["SR_BUILD_VARIANT"] = bv
        except Exception:  # noqa: BLE001
            # Best-effort; never block startup on this.
            pass

        # Point TLS cert + identity key paths to the writable config directory.
        # The default relative paths ("certs/...") resolve inside the PyInstaller
        # temp dir which is read-only.
        certs_dir = self._dir / "certs"
        for key, filename in (
            ("SR_TLS_CERT_PATH", "node.crt"),
            ("SR_TLS_KEY_PATH", "node.key"),
            ("SR_GATEWAY_CA_CERT_PATH", "gateway-ca.crt"),
            ("SR_IDENTITY_KEY_PATH", "node-identity.key"),
        ):
            os.environ[key] = str(certs_dir / filename)

        # Receipts DB path: unify with the rest of the GUI-writable config
        # directory so the CLI (``space-router-node --receipts``) and GUI
        # reference the same file. Pre-fix the GUI used the pydantic default
        # ``~/.spacerouter/receipts.db`` while certs/identity lived under
        # ``~/Library/Application Support/SpaceRouter[-Test]/``, so QA saw
        # "env specifies one path, DB created at another". Migration: if
        # the legacy file exists and the new target doesn't, move it so
        # pre-v1.5 receipts aren't orphaned.
        receipts_db = self._dir / "receipts.db"
        legacy_db = Path.home() / ".spacerouter" / "receipts.db"
        if legacy_db.is_file() and not receipts_db.exists():
            receipts_db.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy_db.replace(receipts_db)
            except OSError:
                # Best-effort — fall back to copy if rename across devices fails.
                import shutil as _shutil
                _shutil.copy2(legacy_db, receipts_db)
        os.environ["SR_RECEIPT_STORE_PATH"] = str(receipts_db)
