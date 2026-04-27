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
    """The pre-v1.4 SR_WALLET_ADDRESS → SR_STAKING_ADDRESS alias migration
    was retired with the v1.5 settings.json migration. Anyone running
    v0.1.2 in 2026 will go through the same single-shot migration that
    converts spacerouter.env to settings.json wholesale.
    """

    def test_fresh_config_has_no_legacy_wallet_address_key(self, store):
        """A brand-new config file must not contain SR_WALLET_ADDRESS.

        In v1.5+ a fresh install also doesn't write a default
        spacerouter.env at all — the file simply doesn't exist.
        """
        vals = dotenv_values(str(store.path))
        assert "SR_WALLET_ADDRESS" not in vals


class TestEnsureFileNoLongerWritesDefaults:
    """Nuclear ensure_file fix (v1.5 plan): a brand-new install must NOT
    pre-create spacerouter.env with defaults. The wizard / GUI onboarding
    is responsible for the first persistent write — to settings.json,
    not the env file. Pre-v1.5 behaviour scattered defaults to disk
    before the user had picked anything.
    """

    def test_brand_new_install_has_no_env_file(self, store):
        # ``store`` was just constructed against a fresh tmp_path; the
        # constructor must not have written anything.
        assert not store.path.exists()

    def test_brand_new_install_has_no_settings_json_either(self, store):
        # Migration only fires when an env file exists; otherwise we
        # leave the dir empty for the wizard to populate.
        assert not (store._dir / "settings.json").exists()

    def test_existing_env_file_is_migrated_and_renamed(self, tmp_path):
        """Existing v1.4 spacerouter.env → settings.json + .migrated.bak."""
        from unittest.mock import patch

        env_path = tmp_path / "spacerouter.env"
        env_path.write_text("SR_NODE_PORT=4321\n")

        with patch("gui.config_store._config_dir", return_value=tmp_path):
            from gui.config_store import ConfigStore
            ConfigStore()  # __init__ runs the migration

        assert (tmp_path / "settings.json").exists()
        assert (tmp_path / "spacerouter.env.migrated.bak").exists()
        assert not env_path.exists()


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
        # test.95 receipt-bug fix: escrow must be ON by default on test
        # builds, with a non-zero leg2 rate so the receipt submitter
        # actually starts. Coord TOFU sync overwrites the rate later.
        assert cs._DEFAULTS["SR_PAYMENT_ENABLED"] == "true"
        assert cs._DEFAULTS["SR_NODE_RATE_PER_GB"] == "1000000000000000"

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
        # Prod stays opt-in until mainnet escrow is rolled out — operators
        # must explicitly flip the flag for now.
        assert cs._DEFAULTS["SR_PAYMENT_ENABLED"] == "false"
        assert cs._DEFAULTS["SR_NODE_RATE_PER_GB"] == ""


# ---------------------------------------------------------------------------
# reset() + _DEFAULTS — Fresh Restart preserves escrow keys now that they
# live in _DEFAULTS. This is the fix for the v1.5 QA "Payment/Escrow
# settings manually added to env are deleted on restart" finding.
# ---------------------------------------------------------------------------


class TestFreshRestartPreservesEscrow:
    def test_reset_rewrites_settings_json_with_variant_defaults(self, monkeypatch, tmp_path):
        """Reset wipes user customisations but preserves variant defaults.

        On v1.5 the canonical store is settings.json, not spacerouter.env.
        After ``reset()`` the file should re-appear with a fresh defaults
        instance for the active build variant, so QA's wallet-edit-then-
        Fresh-Restart flow no longer destroys their config layout.
        """
        import importlib
        import json

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            # Seed config with a custom wallet (simulating user setup).
            store.save_wallets("0x" + "a" * 40)
            store.reset()

        # After reset, settings.json exists with defaults for the active
        # variant. The wallet-address customisation is gone (that's the
        # promise of reset), but the structure itself is sane.
        settings_path = tmp_path / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert data["build_variant"] == "test"
        assert data["wallet"]["staking_address"] in (None, "")

    def test_reset_preserves_escrow_on_test_variant(self, monkeypatch, tmp_path):
        """Regression for the test.95 ship-stopper: Fresh Restart used to
        wipe escrow.enabled to false, leaving the receipt submitter dead
        and Earnings spamming `no such table: signed_receipts`. After
        reset, escrow must still be on with the testnet contract addrs.
        """
        import importlib
        import json

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            store.reset()

        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["escrow"]["enabled"] is True
        assert data["escrow"]["contract_address"].lower().startswith("0xc5740e4e")
        assert "testnet.creditcoin.network" in data["escrow"]["chain_rpc"]
        assert data["escrow"]["chain_id"] == 102031
        assert data["escrow"]["leg2_rate_per_gb"] == "1000000000000000"

    def test_reset_does_not_force_escrow_on_prod_variant(self, monkeypatch, tmp_path):
        """Prod must NOT auto-opt-into escrow until mainnet rollout —
        operators decide. Reset on prod yields escrow.enabled=false and
        no testnet contract addrs.
        """
        import importlib
        import json

        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "production")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            store.reset()

        data = json.loads((tmp_path / "settings.json").read_text())
        assert data["escrow"]["enabled"] is False
        # Empty/None — operator hasn't configured prod escrow yet.
        assert not data["escrow"]["contract_address"]
        assert not data["escrow"]["chain_rpc"]
