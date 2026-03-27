"""Persistent configuration storage for the SpaceRouter GUI.

Reads/writes a spacerouter.env file in a platform-appropriate location.
"""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.identity import write_identity_key
from app.wallet import validate_wallet_address

# Default Coordination API for production
_DEFAULT_COORDINATION_API_URL = "https://spacerouter-coordination-api.fly.dev"

# Pre-configured environments for easy switching
ENVIRONMENTS = {
    "production": {
        "label": "Production",
        "url": "https://spacerouter-coordination-api.fly.dev",
    },
    "test": {
        "label": "Test (CC Testnet)",
        "url": "https://spacerouter-coordination-api-test.fly.dev",
    },
    "staging": {
        "label": "Staging",
        "url": "https://spacerouter-coordination-api-staging.fly.dev",
    },
    "local": {
        "label": "Local",
        "url": "http://localhost:8000",
    },
}

_DEFAULTS = {
    "SR_COORDINATION_API_URL": _DEFAULT_COORDINATION_API_URL,
    "SR_STAKING_ADDRESS": "",
    "SR_COLLECTION_ADDRESS": "",
    "SR_NODE_PORT": "9090",
    "SR_UPNP_ENABLED": "true",
    "SR_MTLS_ENABLED": "true",
    "SR_LOG_LEVEL": "INFO",
    "SR_REGISTRATION_MODE": "v1",
    "SR_IDENTITY_PASSPHRASE": "",
}


def _config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SpaceRouter"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "SpaceRouter"
        return Path.home() / "AppData" / "Local" / "SpaceRouter"
    else:
        # Linux / fallback
        return Path.home() / ".config" / "spacerouter"


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

    def reset(self, keep_addresses: bool = False) -> None:
        """Reset config to defaults. Optionally keep wallet addresses."""
        saved = {}
        if keep_addresses:
            saved["SR_STAKING_ADDRESS"] = self.get("SR_STAKING_ADDRESS")
            saved["SR_COLLECTION_ADDRESS"] = self.get("SR_COLLECTION_ADDRESS")

        # Rewrite with defaults
        lines = [f"{k}={v}" for k, v in _DEFAULTS.items()]
        self._path.write_text("\n".join(lines) + "\n")

        # Restore addresses if requested
        if keep_addresses:
            for key, value in saved.items():
                if value:
                    set_key(str(self._path), key, value)

    def apply_to_env(self) -> None:
        """Load all config values into os.environ so pydantic-settings picks them up."""
        for key, value in self.load().items():
            if value and key not in os.environ:
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
            if key not in os.environ:
                os.environ[key] = str(certs_dir / filename)
