"""Wizard / CLI flag persistence to canonical paths.

Pre-rc.3:
- The first-run wizard wrote to ``./.env`` (relative to cwd), which the
  daemon's settings_loader never reads. Wallet addresses, network mode,
  and the passphrase boolean were silently lost on the first daemon
  start.
- ``--public-url`` / ``--public-port`` only set ``os.environ`` for the
  running process, so headless tunnel-mode operators had to re-pass
  them on every restart.

These tests pin the rc.3 fixes that move both writes onto canonical
paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_wizard_env_file_uses_canonical_spacerouter_dir(tmp_path):
    """``_wizard_env_file()`` must point inside ``~/.spacerouter`` so the
    daemon's settings_loader picks it up via the migrate-from-env step.
    Pre-rc.3 the constant was ``.env`` (relative cwd), an unreachable
    path for the loader.
    """
    fake_home = tmp_path
    with patch.dict("os.environ", {"HOME": str(fake_home)}):
        # Reload the module so Path.home() picks up the patched HOME.
        import importlib
        import app.main
        importlib.reload(app.main)
        env_file = Path(app.main._wizard_env_file())

    assert env_file.parent.name == ".spacerouter"
    assert env_file.name == "spacerouter.env"
    # Helper must ensure the parent dir exists so set_key doesn't crash
    # on a fresh install.
    assert env_file.parent.is_dir()


def test_persist_network_mode_writes_to_settings_json(tmp_path):
    """``--public-url`` and ``--public-port`` must land in settings.json
    so the operator doesn't have to re-pass them on every restart.
    """
    from app.main import _persist_network_mode_to_settings
    from app.settings_loader import settings_path

    with patch("app.settings_loader._spacerouter_dir", return_value=tmp_path):
        _persist_network_mode_to_settings(
            public_url="bore.pub", public_port=12345, no_upnp=False,
        )

        sp = settings_path(tmp_path)
        assert sp.exists()

        # Re-read via the loader so we see the persisted shape.
        from app.settings_loader import load_provider_settings
        s = load_provider_settings(tmp_path)
        assert s.node.public_ip == "bore.pub"
        assert s.node.public_port == 12345
        # Setting --public-url implies tunnel mode.
        assert s.node.upnp_enabled is False


def test_persist_network_mode_noop_when_no_flags_passed(tmp_path):
    """Without any tunnel flags, no settings.json is written. Avoids
    creating files for a default ``spacerouter-node`` invocation.
    """
    from app.main import _persist_network_mode_to_settings
    from app.settings_loader import settings_path

    with patch("app.settings_loader._spacerouter_dir", return_value=tmp_path):
        _persist_network_mode_to_settings(
            public_url=None, public_port=None, no_upnp=False,
        )
        assert not settings_path(tmp_path).exists()


def test_persist_network_mode_no_upnp_alone_persists_off_state(tmp_path):
    """``--no-upnp`` without an explicit public_url is still meaningful
    (manual port forwarding without auto-discovery). Must persist
    ``upnp_enabled=False`` even when public_ip / public_port are
    unchanged.
    """
    from app.main import _persist_network_mode_to_settings
    from app.settings_loader import load_provider_settings

    with patch("app.settings_loader._spacerouter_dir", return_value=tmp_path):
        _persist_network_mode_to_settings(
            public_url=None, public_port=None, no_upnp=True,
        )
        s = load_provider_settings(tmp_path)
        assert s.node.upnp_enabled is False


# --- A2: wizard answers must reach settings.json -----------------------------
#
# Pre-rc.5 the wizard only wrote to spacerouter.env. settings_loader's
# env-file migration was a no-op once a defaults-only settings.json had
# been persisted by the cold-start path (which fires the FIRST time
# load_settings() runs — before the wizard, when computing needs_setup).
# These tests pin the rc.5 fix that writes wizard answers directly into
# settings.json.


def test_persist_wizard_results_writes_to_settings_json(tmp_path, monkeypatch):
    """Calling ``_persist_wizard_results`` produces a settings.json that
    reflects every field the wizard collected.
    """
    from app.main import _persist_wizard_results
    from app.settings_loader import settings_path
    from app.settings_v2 import Settings as _Settings

    monkeypatch.setattr(
        "app.settings_loader._spacerouter_dir", lambda: tmp_path
    )

    _persist_wizard_results(
        staking_address="0x" + "a" * 40,
        collection_address="0x" + "b" * 40,
        referral_code="my-partner",
        upnp_enabled=False,
        public_ip="bore.pub",
        public_port="21781",
        passphrase_set=True,
    )

    sp = settings_path(tmp_path)
    assert sp.exists()
    # Read raw (no reconcile) — the wizard wrote the boolean unconditionally;
    # ``load_provider_settings`` would re-derive it from the on-disk keystore
    # which doesn't exist in this tmp tree.
    s = _Settings.load(sp)
    assert s.wallet.staking_address == "0x" + "a" * 40
    assert s.wallet.collection_address == "0x" + "b" * 40
    assert s.node.referral_code == "my-partner"
    assert s.node.upnp_enabled is False
    assert s.node.public_ip == "bore.pub"
    assert s.node.public_port == 21781
    assert s.wallet.identity_passphrase_set is True


def test_persist_wizard_results_overrides_cold_start_defaults(tmp_path, monkeypatch):
    """Regression for the BLK-1 footgun: when a defaults-only
    settings.json was already created by the cold-start path, the
    wizard's writes must replace those defaults — NOT be silently
    dropped.
    """
    from app.main import _persist_wizard_results
    from app.settings_loader import load_provider_settings, settings_path
    from app.settings_v2 import Settings

    monkeypatch.setattr(
        "app.settings_loader._spacerouter_dir", lambda: tmp_path
    )

    # Simulate the cold-start path having pre-populated settings.json
    # with defaults (no staking / collection set).
    sp = settings_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    Settings().save(sp)

    _persist_wizard_results(
        staking_address="0x" + "c" * 40,
        collection_address="",
        referral_code="",
        upnp_enabled=True,
        public_ip="",
        public_port="",
        passphrase_set=False,
    )

    s = load_provider_settings(tmp_path)
    assert s.wallet.staking_address == "0x" + "c" * 40
    # Bare wizard, no passphrase taken — flag stays False.
    assert s.wallet.identity_passphrase_set is False


def test_persist_wizard_results_preserves_existing_coord_url(tmp_path, monkeypatch):
    """Wizard doesn't touch coordination URL or escrow settings — those
    are seeded by ``ConfigStore`` / ``settings_loader`` defaults. The
    helper must not clobber them.
    """
    from app.main import _persist_wizard_results
    from app.settings_loader import load_provider_settings, settings_path
    from app.settings_v2 import Settings

    monkeypatch.setattr(
        "app.settings_loader._spacerouter_dir", lambda: tmp_path
    )

    sp = settings_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    pre = Settings()
    pre.coordination.url = "https://custom-coord.example.com"
    pre.save(sp)

    _persist_wizard_results(
        staking_address="0x" + "d" * 40,
        collection_address="",
        referral_code="",
        upnp_enabled=True,
        public_ip="",
        public_port="",
        passphrase_set=False,
    )

    s = load_provider_settings(tmp_path)
    assert s.coordination.url == "https://custom-coord.example.com"
    assert s.wallet.staking_address == "0x" + "d" * 40
