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

    def test_receipts_db_path_unified_under_config_dir(self, store, tmp_path):
        """apply_to_env must pin SR_RECEIPT_STORE_PATH to the same writable
        config dir the GUI uses, so the CLI and GUI share one DB."""
        os.environ.pop("SR_RECEIPT_STORE_PATH", None)
        try:
            store.apply_to_env()
            assert os.environ["SR_RECEIPT_STORE_PATH"] == str(tmp_path / "receipts.db")
        finally:
            os.environ.pop("SR_RECEIPT_STORE_PATH", None)


# ---------------------------------------------------------------------------
# _DEFAULTS — per-variant escrow config
# ---------------------------------------------------------------------------


class TestEscrowDefaults:
    def test_test_variant_ships_testnet_escrow_defaults(self, monkeypatch, tmp_path):
        """QA-surface fix: Fresh Restart wiping the env file must not
        strand test-variant users without escrow config. The test variant
        bakes in the Creditcoin testnet contract/RPC/chain-id."""
        import importlib

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        assert cs._DEFAULTS["SR_ESCROW_CONTRACT_ADDRESS"].startswith("0x")
        assert "testnet.creditcoin.network" in cs._DEFAULTS["SR_ESCROW_CHAIN_RPC"]
        assert cs._DEFAULTS["SR_ESCROW_CHAIN_ID"] == "102031"

    def test_prod_variant_leaves_escrow_empty(self, monkeypatch):
        """Prod keeps the fields empty so operators configure them at
        rollout — mainnet escrow isn't a deployed constant yet."""
        import importlib

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "production")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        assert cs._DEFAULTS["SR_ESCROW_CONTRACT_ADDRESS"] == ""
        assert cs._DEFAULTS["SR_ESCROW_CHAIN_RPC"] == ""
        assert cs._DEFAULTS["SR_ESCROW_CHAIN_ID"] == ""


# ---------------------------------------------------------------------------
# reset() + _DEFAULTS — Fresh Restart preserves escrow keys now that they
# live in _DEFAULTS. This is the fix for the v1.5 QA "Payment/Escrow
# settings manually added to env are deleted on restart" finding.
# ---------------------------------------------------------------------------


class TestFreshRestartPreservesEscrow:
    def test_reset_rewrites_with_variant_defaults_not_blank(self, monkeypatch, tmp_path):
        import importlib

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            # Seed the config with an escrow value (simulating QA having
            # set it manually — this was the pain point).
            store.save_wallets("0x" + "a" * 40)
            store.reset()

        rewritten = dotenv_values(str(tmp_path / "spacerouter.env"))
        # After reset, the file is written from _DEFAULTS. Because the
        # escrow contract is now in _DEFAULTS for test builds, it
        # survives the rewrite — QA no longer has to re-add it.
        assert rewritten.get("SR_ESCROW_CONTRACT_ADDRESS", "").startswith("0x")
