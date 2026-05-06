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


# ---------------------------------------------------------------------------
# rc.6 MIN-4 — settlement_key_path default mismatch heal
# ---------------------------------------------------------------------------


def test_schema_default_settlement_key_path_is_correct():
    """Pre-rc.6 default was ~/.spacerouter/identity.key, but the actual
    keystore lives at ~/.spacerouter/certs/node-identity.key. Confirm
    the schema default matches the on-disk reality going forward."""
    s = Settings()
    assert s.wallet.settlement_key_path == "~/.spacerouter/certs/node-identity.key"


def test_heal_settlement_key_path_rewrites_bad_default():
    """Existing users on rc.3/rc.5 have the wrong path persisted in
    their settings.json. The heal helper must rewrite it on load."""
    from app.settings_loader import _heal_settlement_key_path_in_place

    s = Settings()
    s.wallet.settlement_key_path = "~/.spacerouter/identity.key"

    changed = _heal_settlement_key_path_in_place(s)
    assert changed is True
    assert s.wallet.settlement_key_path == "~/.spacerouter/certs/node-identity.key"


def test_heal_settlement_key_path_preserves_custom_value():
    """If the operator set a custom path (or it's already healed), the
    helper must NOT touch it."""
    from app.settings_loader import _heal_settlement_key_path_in_place

    s = Settings()
    custom = "/tmp/somewhere/operator-keystore.key"
    s.wallet.settlement_key_path = custom

    changed = _heal_settlement_key_path_in_place(s)
    assert changed is False
    assert s.wallet.settlement_key_path == custom


def test_heal_settlement_key_path_idempotent_on_correct_default():
    """Already-correct value must return False (no dirty write needed)."""
    from app.settings_loader import _heal_settlement_key_path_in_place

    s = Settings()
    # Default constructor already gives us the correct value.
    changed = _heal_settlement_key_path_in_place(s)
    assert changed is False


def test_load_provider_settings_heals_persisted_bad_path(tmp_path):
    """End-to-end: a settings.json on disk with the bad legacy path is
    healed transparently when load_provider_settings reads it. After
    healing, the reconcile (which tests the keystore file) sees the
    correct path and does the right thing."""
    from app.settings_loader import settings_path

    s = Settings(build_variant="production")
    # Persist the bad legacy path.
    s.wallet.settlement_key_path = "~/.spacerouter/identity.key"
    s.wallet.identity_passphrase_set = False
    sp = settings_path(tmp_path)
    s.save(sp)

    loaded = load_provider_settings(tmp_path)
    assert loaded.wallet.settlement_key_path == (
        "~/.spacerouter/certs/node-identity.key"
    )

    # And the heal was persisted to disk so the next load is a no-op.
    import json
    raw = json.loads(sp.read_text())
    assert raw["wallet"]["settlement_key_path"] == (
        "~/.spacerouter/certs/node-identity.key"
    )
