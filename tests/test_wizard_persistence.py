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
