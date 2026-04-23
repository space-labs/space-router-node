"""Persistent configuration storage for the SpaceRouter GUI.

Reads/writes a spacerouter.env file in a platform-appropriate location.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.identity import write_identity_key
from app.variant import BUILD_VARIANT
from app.wallet import validate_wallet_address

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
    "SR_ESCROW_CONTRACT_ADDRESS": _default_escrow_contract(),
    "SR_ESCROW_CHAIN_RPC": _default_escrow_chain_rpc(),
    "SR_ESCROW_CHAIN_ID": _default_escrow_chain_id(),
}


def _config_dir() -> Path:
    from app.paths import config_dir
    return config_dir()


class ConfigStore:
    """Manage spacerouter.env configuration file."""

    def __init__(self) -> None:
        self._dir = _config_dir()
        self._path = self._dir / "spacerouter.env"
        self._ensure_file()

    def _ensure_file(self) -> None:
        """Create config dir and file with defaults if they don't exist."""
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            lines = [f"{k}={v}" for k, v in _DEFAULTS.items()]
            self._path.write_text("\n".join(lines) + "\n")
        else:
            self._migrate_wallet_address()

    def _migrate_wallet_address(self) -> None:
        """Migrate SR_WALLET_ADDRESS → SR_STAKING_ADDRESS for existing configs."""
        vals = dotenv_values(self._path)
        if vals.get("SR_WALLET_ADDRESS") and not vals.get("SR_STAKING_ADDRESS"):
            set_key(str(self._path), "SR_STAKING_ADDRESS", vals["SR_WALLET_ADDRESS"])

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str | None]:
        """Return all config values from the env file."""
        return dotenv_values(self._path)

    def get(self, key: str, default: str = "") -> str:
        vals = self.load()
        return vals.get(key) or default

    def save_wallets(self, staking_address: str, collection_address: str = "") -> tuple[str, str]:
        """Validate and persist staking and collection addresses.

        Returns ``(normalised_staking, normalised_collection)``.
        """
        normalised_staking = validate_wallet_address(staking_address)
        set_key(str(self._path), "SR_STAKING_ADDRESS", normalised_staking)

        if collection_address.strip():
            normalised_collection = validate_wallet_address(collection_address)
        else:
            normalised_collection = normalised_staking
        set_key(str(self._path), "SR_COLLECTION_ADDRESS", normalised_collection)

        return normalised_staking, normalised_collection

    def save_environment(self, env_key: str) -> str:
        """Switch the coordination API URL to the given environment.

        Returns the URL that was set.
        """
        env = ENVIRONMENTS.get(env_key)
        if not env:
            raise ValueError(f"Unknown environment: {env_key}")
        set_key(str(self._path), "SR_COORDINATION_API_URL", env["url"])
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
        key_path = self.get("SR_IDENTITY_KEY_PATH") or str(
            self._dir / "certs" / "node-identity.key"
        )
        return not os.path.isfile(key_path)

    def save_onboarding(
        self,
        passphrase: str = "",
        staking: str = "",
        collection: str = "",
        identity_key_hex: str = "",
    ) -> None:
        """Persist onboarding choices and optionally pre-write an imported identity key.

        - *passphrase*: written as SR_IDENTITY_PASSPHRASE (may be empty).
        - *staking*: staking wallet address; empty → uses identity address at runtime.
        - *collection*: collection wallet address; empty → uses staking address.
        - *identity_key_hex*: if provided, the raw private key is written to the
          identity key file immediately (encrypted if *passphrase* is set).
        """
        if staking:
            staking = validate_wallet_address(staking)
        if collection:
            collection = validate_wallet_address(collection)

        set_key(str(self._path), "SR_IDENTITY_PASSPHRASE", passphrase)
        if staking:
            set_key(str(self._path), "SR_STAKING_ADDRESS", staking)
        if collection:
            set_key(str(self._path), "SR_COLLECTION_ADDRESS", collection)

        if identity_key_hex:
            key_path = self.get("SR_IDENTITY_KEY_PATH") or str(
                self._dir / "certs" / "node-identity.key"
            )
            write_identity_key(key_path, identity_key_hex, passphrase)

    def save_settings(self, coordination_api_url: str, mtls_enabled: bool) -> None:
        """Persist advanced settings (coordination API URL and mTLS toggle)."""
        set_key(str(self._path), "SR_COORDINATION_API_URL", coordination_api_url)
        set_key(str(self._path), "SR_MTLS_ENABLED", str(mtls_enabled).lower())

    def save_network_mode(self, mode: str, public_host: str = "", port: str = "") -> None:
        """Persist network mode settings.

        Args:
            mode: 'upnp' or 'tunnel'
            public_host: hostname/IP for tunnel mode (e.g. 'bore.pub')
            port: remote/advertised port for tunnel mode (e.g. '21781').
                  The node always listens on SR_NODE_PORT (9090) locally.
        """
        if mode == "upnp":
            set_key(str(self._path), "SR_UPNP_ENABLED", "true")
            set_key(str(self._path), "SR_PUBLIC_IP", "")
            set_key(str(self._path), "SR_PUBLIC_PORT", "")
        elif mode == "tunnel":
            set_key(str(self._path), "SR_UPNP_ENABLED", "false")
            set_key(str(self._path), "SR_PUBLIC_IP", public_host)
            set_key(str(self._path), "SR_PUBLIC_PORT", port or "")

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

        # Delete identity key file
        key_path = self.get("SR_IDENTITY_KEY_PATH") or str(
            self._dir / "certs" / "node-identity.key"
        )
        if os.path.isfile(key_path):
            os.remove(key_path)

        # Delete all certificates in the certs directory
        certs_dir = self._dir / "certs"
        if certs_dir.is_dir():
            shutil.rmtree(certs_dir)

        # Rewrite config with defaults
        lines = [f"{k}={v}" for k, v in _DEFAULTS.items()]
        self._path.write_text("\n".join(lines) + "\n")

    def apply_to_env(self) -> None:
        """Load all config values into os.environ so pydantic-settings picks them up."""
        for key, value in self.load().items():
            if value:
                os.environ[key] = value

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
