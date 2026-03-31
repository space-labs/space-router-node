"""Tests for gui/config_store.py — backward-compat migration and core behaviour."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values


@pytest.fixture()
def store(tmp_path):
    """Return a ConfigStore whose config directory is isolated to tmp_path."""
    with patch("gui.config_store._config_dir", return_value=tmp_path):
        from gui.config_store import ConfigStore
        yield ConfigStore()


# ---------------------------------------------------------------------------
# Backward-compat migration: SR_WALLET_ADDRESS → SR_STAKING_ADDRESS
# ---------------------------------------------------------------------------

class TestWalletAddressMigration:
    def test_existing_config_with_sr_wallet_address_is_migrated(self, store):
        """An existing spacerouter.env that has SR_WALLET_ADDRESS but no
        SR_STAKING_ADDRESS must have SR_STAKING_ADDRESS written into the file."""
        addr = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        # Overwrite the config file to simulate a v0.1.2 config
        store.path.write_text(f"SR_WALLET_ADDRESS={addr}\n")

        store._migrate_wallet_address()

        vals = dotenv_values(str(store.path))
        assert vals.get("SR_STAKING_ADDRESS") == addr

    def test_migration_does_not_overwrite_existing_sr_staking_address(self, store):
        """If SR_STAKING_ADDRESS is already set, migration must not overwrite it."""
        addr_old = "0x" + "aa" * 20
        addr_new = "0x" + "bb" * 20
        store.path.write_text(
            f"SR_WALLET_ADDRESS={addr_old}\nSR_STAKING_ADDRESS={addr_new}\n"
        )

        store._migrate_wallet_address()

        vals = dotenv_values(str(store.path))
        assert vals.get("SR_STAKING_ADDRESS") == addr_new

    def test_fresh_config_has_no_legacy_wallet_address_key(self, store):
        """A brand-new config file must not contain SR_WALLET_ADDRESS."""
        vals = dotenv_values(str(store.path))
        assert "SR_WALLET_ADDRESS" not in vals


# ---------------------------------------------------------------------------
# needs_onboarding()
# ---------------------------------------------------------------------------

class TestNeedsOnboarding:
    def test_returns_true_when_key_file_missing(self, store):
        assert store.needs_onboarding() is True

    def test_returns_false_when_key_file_exists(self, store, tmp_path):
        key_path = tmp_path / "certs" / "node-identity.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("fakehex\n")
        assert store.needs_onboarding() is False


# ---------------------------------------------------------------------------
# apply_to_env() — cert paths redirect to writable config directory
# ---------------------------------------------------------------------------

class TestApplyToEnv:
    def test_cert_paths_set_to_config_dir(self, store, tmp_path):
        # Clear any prior values
        for key in ("SR_TLS_CERT_PATH", "SR_TLS_KEY_PATH",
                    "SR_GATEWAY_CA_CERT_PATH", "SR_IDENTITY_KEY_PATH"):
            os.environ.pop(key, None)

        store.apply_to_env()

        certs_dir = tmp_path / "certs"
        assert os.environ.get("SR_TLS_CERT_PATH") == str(certs_dir / "node.crt")
        assert os.environ.get("SR_TLS_KEY_PATH") == str(certs_dir / "node.key")
        assert os.environ.get("SR_GATEWAY_CA_CERT_PATH") == str(certs_dir / "gateway-ca.crt")
        assert os.environ.get("SR_IDENTITY_KEY_PATH") == str(certs_dir / "node-identity.key")

        # Cleanup
        for key in ("SR_TLS_CERT_PATH", "SR_TLS_KEY_PATH",
                    "SR_GATEWAY_CA_CERT_PATH", "SR_IDENTITY_KEY_PATH"):
            os.environ.pop(key, None)

    def test_apply_to_env_overwrites_existing_env_vars(self, store, tmp_path):
        """apply_to_env always writes from config so settings changes take effect."""
        os.environ["SR_TLS_CERT_PATH"] = "/custom/path/node.crt"
        try:
            store.apply_to_env()
            certs_dir = tmp_path / "certs"
            assert os.environ["SR_TLS_CERT_PATH"] == str(certs_dir / "node.crt")
        finally:
            os.environ.pop("SR_TLS_CERT_PATH", None)
            os.environ.pop("SR_TLS_KEY_PATH", None)
            os.environ.pop("SR_GATEWAY_CA_CERT_PATH", None)
            os.environ.pop("SR_IDENTITY_KEY_PATH", None)
