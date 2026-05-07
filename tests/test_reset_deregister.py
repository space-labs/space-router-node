"""rc.8 #7 — ``--reset`` deregister edge cases.

Two bugs from Jin's rc.7 Linux QA:

* **7a**: encrypted keystore + no ``SR_IDENTITY_PASSPHRASE`` →
  ``KeystorePassphraseRequired`` was caught silently inside
  ``deregister_best_effort_sync``. Result: dereg skipped, coord row
  stayed "online" until the health-check timeout.
* **7b**: coord 500 during dereg → ``deregister_node`` swallowed the
  error and returned None, so the wrapper still returned True and the
  CLI printed the misleading "Notified coordination API" line.

Local wipe must proceed in both edge cases — operators rely on
``--reset`` always finishing.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def encrypted_keystore_setup(tmp_path):
    """Settings whose IDENTITY_KEY_PATH points at a Web3 keystore JSON file.

    Content shape mirrors what ``Account.encrypt`` writes — only the
    presence of a ``crypto`` key matters for ``_is_keystore_json``.
    """
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    key_path = certs_dir / "node-identity.key"
    key_path.write_text(json.dumps({
        "version": 3,
        "id": "fake",
        "address": "00" * 20,
        "crypto": {"cipher": "aes-128-ctr", "ciphertext": "deadbeef"},
    }))
    return tmp_path, certs_dir, key_path


@pytest.fixture()
def plaintext_keystore_setup(tmp_path):
    """Settings whose IDENTITY_KEY_PATH is a plaintext hex private key."""
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    key_path = certs_dir / "node-identity.key"
    key_path.write_text("0x" + "ab" * 32 + "\n")
    return tmp_path, certs_dir, key_path


# ---------------------------------------------------------------------------
# 7a — encrypted keystore + --reset + no SR_IDENTITY_PASSPHRASE
# ---------------------------------------------------------------------------


class TestEncryptedKeystoreReset:
    def test_helper_detects_encrypted_keystore(self, encrypted_keystore_setup):
        from app.main import _identity_keystore_is_encrypted

        _, _, key_path = encrypted_keystore_setup
        s = MagicMock()
        s.IDENTITY_KEY_PATH = str(key_path)
        assert _identity_keystore_is_encrypted(s) is True

    def test_helper_returns_false_for_plaintext(self, plaintext_keystore_setup):
        from app.main import _identity_keystore_is_encrypted

        _, _, key_path = plaintext_keystore_setup
        s = MagicMock()
        s.IDENTITY_KEY_PATH = str(key_path)
        assert _identity_keystore_is_encrypted(s) is False

    def test_helper_returns_false_for_missing_file(self, tmp_path):
        from app.main import _identity_keystore_is_encrypted

        s = MagicMock()
        s.IDENTITY_KEY_PATH = str(tmp_path / "missing.key")
        assert _identity_keystore_is_encrypted(s) is False

    def test_non_interactive_encrypted_no_passphrase_exits_with_actionable_message(
        self, encrypted_keystore_setup, capsys, monkeypatch,
    ):
        """7a non-interactive: must SystemExit(1) and print actionable
        guidance to stderr — silent-skip is the bug we're fixing."""
        tmp_path, certs_dir, key_path = encrypted_keystore_setup
        monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys:
            mock_settings.return_value.IDENTITY_KEY_PATH = str(key_path)
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stderr = __import__("sys").stderr
            mock_sys.exit = MagicMock(side_effect=SystemExit(1))

            from app.main import _do_reset
            with pytest.raises(SystemExit):
                _do_reset()

        err = capsys.readouterr().err
        assert "SR_IDENTITY_PASSPHRASE" in err
        assert "encrypted keystore" in err
        # Local wipe must NOT have happened — we bailed before any deletes.
        assert key_path.exists()

    def test_interactive_encrypted_prompts_then_proceeds(
        self, encrypted_keystore_setup, monkeypatch,
    ):
        """7a interactive: getpass returns a passphrase; reset proceeds
        through deregister + local wipe."""
        tmp_path, certs_dir, key_path = encrypted_keystore_setup
        monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys, \
             patch("getpass.getpass", return_value="hunter2") as mock_getpass, \
             patch(
                 "app.registration.deregister_best_effort_sync",
                 return_value=True,
             ) as mock_dereg:
            mock_settings.return_value.IDENTITY_KEY_PATH = str(key_path)
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = True
            # Confirm prompt — return YES through builtins.input.
            with patch("builtins.input", return_value="YES"):
                from app.main import _do_reset
                _do_reset()

        # getpass was called once for the passphrase.
        assert mock_getpass.call_count == 1
        # Env was populated so downstream load_settings picks it up.
        assert os.environ.get("SR_IDENTITY_PASSPHRASE") == "hunter2"
        # Dereg helper was invoked.
        assert mock_dereg.call_count == 1
        # Local wipe ran.
        assert not key_path.exists()

        # Cleanup env to keep test isolation.
        monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

    def test_unencrypted_skips_passphrase_prompt(
        self, plaintext_keystore_setup, monkeypatch,
    ):
        """Regression: rc.6 MAJ-3 happy path — unencrypted key + healthy
        coord → no prompt, success message."""
        tmp_path, certs_dir, key_path = plaintext_keystore_setup
        monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys, \
             patch("getpass.getpass") as mock_getpass, \
             patch(
                 "app.registration.deregister_best_effort_sync",
                 return_value=True,
             ) as mock_dereg:
            mock_settings.return_value.IDENTITY_KEY_PATH = str(key_path)
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = False

            from app.main import _do_reset
            _do_reset()

        # No prompt for plaintext keys.
        assert mock_getpass.call_count == 0
        # Dereg still ran.
        assert mock_dereg.call_count == 1
        # Local wipe ran.
        assert not key_path.exists()


# ---------------------------------------------------------------------------
# 7b — coord 500 honesty
# ---------------------------------------------------------------------------


class TestCoord500HonestFailure:
    def test_wrapper_returns_false_on_http_error(self, tmp_path):
        """``deregister_best_effort_sync`` must return False when the
        underlying ``deregister_node`` raises (e.g. coord 500). Pre-fix
        it returned True because the inner helper swallowed the error."""
        from app.config import Settings
        from app.registration import deregister_best_effort_sync

        settings = Settings(
            COORDINATION_API_URL="http://example.invalid",
            IDENTITY_KEY_PATH=str(tmp_path / "missing.key"),
            IDENTITY_PASSPHRASE="",
        )

        async def _boom(*args, **kwargs):
            import httpx
            req = httpx.Request("PATCH", "http://example.invalid/x")
            raise httpx.HTTPStatusError(
                "500 server error",
                request=req,
                response=httpx.Response(500, request=req),
            )

        with patch(
            "app.identity.load_or_create_identity",
            return_value=("0x" + "ab" * 32, "0x" + "cd" * 20),
        ), patch("app.registration.deregister_node", side_effect=_boom):
            ok = deregister_best_effort_sync(settings)
        assert ok is False

    def test_do_reset_prints_honest_failure_on_coord_500(
        self, plaintext_keystore_setup, capsys,
    ):
        """7b: a False from the wrapper must surface as "Coord deregister
        failed" — NOT the misleading "Notified coordination API" line.
        Local wipe still completes."""
        tmp_path, certs_dir, key_path = plaintext_keystore_setup

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys, \
             patch(
                 "app.registration.deregister_best_effort_sync",
                 return_value=False,
             ) as mock_dereg:
            mock_settings.return_value.IDENTITY_KEY_PATH = str(key_path)
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = False

            from app.main import _do_reset
            _do_reset()

        out = capsys.readouterr().out
        assert "Notified coordination API" not in out
        assert "Coord deregister failed" in out
        assert "local state still wiped" in out.lower()
        # Wrapper was invoked exactly once.
        assert mock_dereg.call_count == 1
        # Local wipe ran (the whole point of "still wiped").
        assert not key_path.exists()

    def test_do_reset_prints_success_when_coord_healthy(
        self, plaintext_keystore_setup, capsys,
    ):
        """Regression: rc.6 MAJ-3 happy path. Coord returns 200 →
        wrapper True → success message printed (unchanged)."""
        tmp_path, certs_dir, key_path = plaintext_keystore_setup

        with patch("app.main.load_settings") as mock_settings, \
             patch("app.paths.config_dir", return_value=tmp_path), \
             patch("app.main.sys") as mock_sys, \
             patch(
                 "app.registration.deregister_best_effort_sync",
                 return_value=True,
             ):
            mock_settings.return_value.IDENTITY_KEY_PATH = str(key_path)
            mock_sys.argv = ["prog", "--reset"]
            mock_sys.stdin.isatty.return_value = False

            from app.main import _do_reset
            _do_reset()

        out = capsys.readouterr().out
        assert "Notified coordination API (status → offline)." in out
        assert "Coord deregister failed" not in out
