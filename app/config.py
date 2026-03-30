import logging
import warnings

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SR_",
        env_file=".env",
        populate_by_name=True,
    )

    NODE_PORT: int = 9090
    COORDINATION_API_URL: str = "http://localhost:8000"

    # Max concurrent proxy connections (DoS protection)
    MAX_CONNECTIONS: int = 256

    # Bind address — restrict to specific interface if needed
    BIND_ADDRESS: str = "0.0.0.0"

    NODE_LABEL: str = ""

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

    # Node identity keypair (auto-generated secp256k1 for signing API requests)
    IDENTITY_KEY_PATH: str = "certs/node-identity.key"
    IDENTITY_PASSPHRASE: str = ""   # If set, encrypt identity key with Web3 keystore JSON

    # TLS — auto-generates a self-signed cert if files don't exist
    TLS_CERT_PATH: str = "certs/node.crt"
    TLS_KEY_PATH: str = "certs/node.key"

    # mTLS — Gateway authentication (requires gateway_ca_cert from registration)
    MTLS_ENABLED: bool = True
    GATEWAY_CA_CERT_PATH: str = "certs/gateway-ca.crt"

    @field_validator("REGISTRATION_MODE")
    @classmethod
    def _validate_registration_mode(cls, v: str) -> str:
        allowed = ("v1", "v2", "auto")
        if v not in allowed:
            raise ValueError(f"REGISTRATION_MODE must be one of {allowed}, got {v!r}")
        return v


def load_settings() -> Settings:
    """Create a fresh Settings instance from current environment variables.

    Call this instead of importing the module-level ``settings`` when you need
    to pick up env-var changes (e.g. after a config reload or fresh restart).
    """
    s = Settings()
    if not s.COORDINATION_API_URL.startswith("https://"):
        if "localhost" not in s.COORDINATION_API_URL and "127.0.0.1" not in s.COORDINATION_API_URL:
            warnings.warn(
                f"COORDINATION_API_URL uses plain HTTP ({s.COORDINATION_API_URL}). "
                "This exposes registration data to MITM attacks. Use HTTPS in production.",
                stacklevel=2,
            )
    return s


# Lazy module-level singleton for backward compatibility.
# Code that imports ``from app.config import settings`` still works, but new
# code should call ``load_settings()`` for a guaranteed-fresh instance.
settings = load_settings()
