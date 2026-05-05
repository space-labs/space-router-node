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
        # builds. The leg2 rate is NOT pre-seeded — the canonical rate
        # comes from the gateway's /config endpoint via TOFU sync at
        # boot. Pre-seeding caused test.101's SIGN_REJECTED_UNKNOWN_REQUEST
        # regression. See PR #94.
        assert cs._DEFAULTS["SR_PAYMENT_ENABLED"] == "true"
        assert "SR_NODE_RATE_PER_GB" not in cs._DEFAULTS

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
        # Same as test variant: rate comes from coord, not from defaults.
        assert "SR_NODE_RATE_PER_GB" not in cs._DEFAULTS


# ---------------------------------------------------------------------------
# reset() + _DEFAULTS — Fresh Restart preserves escrow keys now that they
# live in _DEFAULTS. This is the fix for the v1.5 QA "Payment/Escrow
# settings manually added to env are deleted on restart" finding.
# ---------------------------------------------------------------------------


class TestFreshRestartPreservesEscrow:
    """Reset must wipe customisations but the next *load* must come back
    with sane variant defaults.

    rc.5 MAJ-3 changed reset() to DELETE settings.json (matching CLI
    --reset) instead of re-saving a defaults instance. The defaults are
    re-materialised on the next load via ``app.settings_loader``'s
    cold-start path + ``_backfill_test_escrow_in_place``. These tests
    exercise that round-trip: reset() then load_provider_settings.
    """

    @staticmethod
    def _force_seed_variant(monkeypatch, variant: str) -> None:
        """Make a fresh post-reset load come back with *variant* as the
        ``build_variant``.

        Strategy: patch ``app.variant.BUILD_VARIANT`` (used by
        ``gui.config_store``) and set ``SR_BUILD_VARIANT`` in os.environ
        so ``settings_loader``'s cold-start step 3 (env-mapping path)
        fires with the right value before falling through to the
        defaults-only step 4. Both are reverted by monkeypatch teardown
        so tests stay isolated.
        """
        import app.variant as variant_mod
        monkeypatch.setattr(variant_mod, "BUILD_VARIANT", variant)
        monkeypatch.setenv("SR_BUILD_VARIANT", variant)

    def test_reset_then_reload_yields_variant_defaults(self, monkeypatch, tmp_path):
        """After ``reset()`` plus a load, settings.json reappears with
        fresh defaults for the active build variant. Wallet
        customisations are gone (that's the promise of reset).
        """
        import importlib

        self._force_seed_variant(monkeypatch, "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            # Seed config with a custom wallet (simulating user setup).
            store.save_wallets("0x" + "a" * 40)
            store.reset()

        # rc.5: settings.json is deleted on reset. The cold-start path
        # rebuilds it on the next load.
        assert not (tmp_path / "settings.json").exists()

        from app.settings_loader import load_provider_settings
        s = load_provider_settings(tmp_path)
        # Rebuilt on load — wallet wiped, variant intact.
        assert s.build_variant == "test"
        assert s.wallet.staking_address in (None, "")
        # And settings.json is now back on disk.
        assert (tmp_path / "settings.json").exists()

    def test_reset_then_reload_preserves_escrow_on_test_variant(self, monkeypatch, tmp_path):
        """Regression for the test.95 ship-stopper: Fresh Restart used to
        wipe escrow.enabled to false, leaving the receipt submitter dead
        and Earnings spamming `no such table: signed_receipts`. After
        reset + reload, escrow must still be on with testnet contract
        addrs (via ``_backfill_test_escrow_in_place``).
        """
        import importlib

        self._force_seed_variant(monkeypatch, "test")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            store.reset()

        from app.settings_loader import load_provider_settings
        s = load_provider_settings(tmp_path)
        assert s.escrow.enabled is True
        assert s.escrow.contract_address.lower().startswith("0xc5740e4e")
        assert "testnet.creditcoin.network" in s.escrow.chain_rpc
        assert s.escrow.chain_id == 102031
        # Rate is left null after reset; TOFU sync at boot fills it.
        assert s.escrow.leg2_rate_per_gb is None

    def test_reset_then_reload_does_not_force_escrow_on_prod_variant(self, monkeypatch, tmp_path):
        """Prod must NOT auto-opt-into escrow until mainnet rollout —
        operators decide. Reset + reload on prod yields escrow.enabled=false.
        """
        import importlib

        self._force_seed_variant(monkeypatch, "production")

        import gui.config_store as cs
        cs = importlib.reload(cs)

        with patch.object(cs, "_config_dir", return_value=tmp_path):
            store = cs.ConfigStore()
            store.reset()

        from app.settings_loader import load_provider_settings
        s = load_provider_settings(tmp_path)
        assert s.escrow.enabled is False
        assert not s.escrow.contract_address
        assert not s.escrow.chain_rpc
