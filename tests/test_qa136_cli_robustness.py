"""CLI robustness gaps QA hit on v1.5.2-test.136.

Three separate reports, all reproduced here against the real code:

- Starting with no stdin attached crashed with a bare ``EOFError`` traceback.
  The setup wizard is already gated on ``isatty()``, so the exposed path is the
  keystore passphrase prompt during normal startup.
- ``settings.json`` saved with a UTF-8 BOM (every Windows editor writes one by
  default) raised ``JSONDecodeError`` at startup.
- ``--help`` claimed the ``0x`` prefix was required, contradicting BUG-06,
  which deliberately accepts bare 40-hex and normalises it.
"""
from __future__ import annotations

import io
import json

import pytest

from app.cli_ui import NonInteractiveError, wizard_confirm, wizard_input


class _ClosedStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_prompt_without_a_terminal_raises_a_typed_error_not_eoferror(monkeypatch):
    monkeypatch.setattr("sys.stdin", _ClosedStdin())
    with pytest.raises(NonInteractiveError) as excinfo:
        wizard_input("Identity key is encrypted. Passphrase", password=True)
    msg = str(excinfo.value)
    assert "no interactive terminal" in msg
    assert "--help" in msg, "the error should tell the operator what to do instead"


def test_prompt_without_a_terminal_uses_a_default_when_one_exists(monkeypatch):
    monkeypatch.setattr("sys.stdin", _ClosedStdin())
    assert wizard_input("Coordination URL", default="https://example.test") == (
        "https://example.test"
    )


def test_confirm_without_a_terminal_takes_the_default(monkeypatch):
    monkeypatch.setattr("sys.stdin", _ClosedStdin())
    assert wizard_confirm("Continue?", default=False) is False
    assert wizard_confirm("Continue?", default=True) is True


def test_missing_stdin_object_is_survivable(monkeypatch):
    """`pythonw` / a detached GUI process can leave sys.stdin as None."""
    monkeypatch.setattr("sys.stdin", None)
    with pytest.raises(NonInteractiveError):
        wizard_input("Passphrase", password=True)


def test_settings_json_with_a_utf8_bom_loads(tmp_path, monkeypatch):
    """Windows editors write a BOM by default; it must not break startup."""
    from app.settings_v2 import Settings

    path = tmp_path / "settings.json"
    payload = {"wallet": {"staking_address": "0x" + "ab" * 20}}
    path.write_text(json.dumps(payload), encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "fixture must have a BOM"

    # The production loader, not a re-implementation of the read.
    settings = Settings.load(path)
    assert settings.wallet.staking_address.lower() == "0x" + "ab" * 20


def test_staking_address_help_does_not_claim_0x_is_required():
    """BUG-06 accepts bare 40-hex; --help said the prefix was required."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    idx = src.index('"--staking-address"')
    window = src[idx:idx + 300]
    assert "0x followed by 40 hex chars" not in window, (
        "--help still claims the 0x prefix is required, which contradicts "
        "BUG-06 accepting and normalising bare hex"
    )
    assert "optional" in window, "help should say the prefix is optional"


def test_wrong_passphrase_in_setup_retries_then_exits_cleanly(monkeypatch, tmp_path):
    """A wrong passphrase must not escape as a traceback.

    KeystoreWrongPassphrase subclasses KeystorePassphraseRequired, so the
    wizard's `except KeystorePassphraseRequired` caught the FIRST prompt but
    the retry call sat outside any try. One wrong entry killed the wizard with
    a full traceback and a PyInstaller "Failed to execute script" line.
    """
    import app.main as main_mod
    from app.identity import KeystorePassphraseRequired, KeystoreWrongPassphrase

    key_path = tmp_path / "node-identity.key"
    key_path.write_text('{"crypto": {}}')  # looks like a keystore

    calls = {"prompts": 0, "loads": 0}

    def _fake_load(path, passphrase=""):
        calls["loads"] += 1
        if not passphrase:
            raise KeystorePassphraseRequired("passphrase required")
        raise KeystoreWrongPassphrase("bad passphrase")

    def _fake_prompt(prompt, default="", password=False):
        calls["prompts"] += 1
        return "wrong-passphrase"

    import app.cli_ui as cli_ui

    monkeypatch.setattr(main_mod, "load_or_create_identity", _fake_load)
    # The wizard imports its prompts locally from app.cli_ui, so patch there.
    monkeypatch.setattr(cli_ui, "wizard_input", _fake_prompt)
    monkeypatch.setattr(cli_ui, "wizard_error", lambda *a, **k: None)
    monkeypatch.setattr(cli_ui, "wizard_success", lambda *a, **k: None)
    monkeypatch.setattr(cli_ui, "wizard_step", lambda *a, **k: None)

    class _S:
        IDENTITY_KEY_PATH = str(key_path)
    monkeypatch.setattr(main_mod, "load_settings", lambda *a, **k: _S())

    with pytest.raises(SystemExit) as excinfo:
        main_mod._first_run_setup()

    assert excinfo.value.code == 1, "a wrong passphrase should exit 1, not crash"
    assert calls["prompts"] == main_mod._PASSPHRASE_MAX_ATTEMPTS, (
        f"expected {main_mod._PASSPHRASE_MAX_ATTEMPTS} attempts, "
        f"got {calls['prompts']}"
    )
