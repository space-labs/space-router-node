"""Reconciliation of ``wallet.identity_passphrase_set`` against the keystore.

The flag in ``settings.json`` is a derived cache: the actual question is
whether ``node-identity.key`` exists and is encrypted JSON. Pre-rc.3 the
cached value was trusted as-is, so a user who manually deleted the
keystore (or whose reset wiped the keystore but somehow left
settings.json) would route the daemon into ``PASSPHRASE_REQUIRED`` for
a missing file and never recover.

These tests pin the self-healing reconcile in
:py:func:`app.settings_loader._reconcile_passphrase_flag_in_place`.
"""

from __future__ import annotations

import json
from pathlib import Path

from eth_account import Account

from app.settings_loader import (
    _reconcile_passphrase_flag_in_place,
    load_provider_settings,
    settings_path,
)
from app.settings_v2 import Settings


_PRIVKEY_HEX = "0x" + "11" * 32


def _seed_settings(tmp_path: Path, *, flag: bool, key_path_basename: str = "node-identity.key") -> Path:
    """Write a settings.json with ``identity_passphrase_set=flag``.

    Uses ``build_variant="production"`` so the test-variant escrow
    backfill (a sibling reconciler) doesn't fire and conflate the
    mtime / dirty-write assertions.
    """
    s = Settings(build_variant="production")
    s.wallet.identity_passphrase_set = flag
    s.wallet.settlement_key_path = str(tmp_path / key_path_basename)
    sp = settings_path(tmp_path)
    s.save(sp)
    return sp


def _write_plaintext_key(path: Path) -> None:
    path.write_text(_PRIVKEY_HEX + "\n")


def _write_encrypted_keystore(path: Path) -> None:
    encrypted = Account.encrypt(_PRIVKEY_HEX, "passphrase-x")
    path.write_text(json.dumps(encrypted))


def test_flag_cleared_when_keystore_missing(tmp_path):
    """User manually rm'd node-identity.key. Cached flag was True; load
    must flip it to False so the wizard re-creates without prompting
    for a passphrase that no longer applies.
    """
    _seed_settings(tmp_path, flag=True)
    # No keystore file written.

    s = load_provider_settings(tmp_path)

    assert s.wallet.identity_passphrase_set is False


def test_flag_cleared_when_keystore_is_plaintext_hex(tmp_path):
    """Cached flag was True but the keystore was downgraded to plaintext
    (e.g. operator manually replaced the file). Reconcile resets the
    flag — there's no passphrase to prompt for.
    """
    _seed_settings(tmp_path, flag=True)
    _write_plaintext_key(tmp_path / "node-identity.key")

    s = load_provider_settings(tmp_path)

    assert s.wallet.identity_passphrase_set is False


def test_flag_set_when_keystore_is_encrypted_json(tmp_path):
    """Cached flag was False but the keystore on disk is encrypted JSON.
    Reconcile flips the flag so the GUI knows to prompt for a
    passphrase on next start.
    """
    _seed_settings(tmp_path, flag=False)
    _write_encrypted_keystore(tmp_path / "node-identity.key")

    s = load_provider_settings(tmp_path)

    assert s.wallet.identity_passphrase_set is True


def test_flag_preserved_when_already_in_sync(tmp_path):
    """Encrypted keystore + flag=True is the steady state — load must
    not flip it back and forth and must not rewrite settings.json on
    every boot.
    """
    sp = _seed_settings(tmp_path, flag=True)
    _write_encrypted_keystore(tmp_path / "node-identity.key")
    pre_mtime = sp.stat().st_mtime_ns

    s = load_provider_settings(tmp_path)

    assert s.wallet.identity_passphrase_set is True
    # The reconcile must be a no-op when nothing changed (escrow backfill
    # also doesn't re-fire on a defaults-having build). settings.json
    # mtime should be unchanged.
    assert sp.stat().st_mtime_ns == pre_mtime


def test_helper_handles_missing_file(tmp_path):
    """Direct unit test of the helper: missing file → False, returns True
    only if the cached value differs from reality.
    """
    s = Settings()
    s.wallet.settlement_key_path = str(tmp_path / "absent.key")
    s.wallet.identity_passphrase_set = True

    assert _reconcile_passphrase_flag_in_place(s) is True
    assert s.wallet.identity_passphrase_set is False


def test_helper_idempotent_when_already_correct(tmp_path):
    s = Settings()
    s.wallet.settlement_key_path = str(tmp_path / "absent.key")
    s.wallet.identity_passphrase_set = False

    assert _reconcile_passphrase_flag_in_place(s) is False
