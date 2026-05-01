"""Unit tests for app/identity.py — keystore encryption, decryption, and migration."""

import json
import logging
import os
import stat
import sys
from unittest import mock

import pytest
from eth_account import Account

from app.identity import (
    KeystorePassphraseRequired,
    _is_keystore_json,
    load_or_create_identity,
    write_identity_key,
)

TEST_PASSPHRASE = "test-passphrase-123"

# A deterministic test key so we can verify round-trips
_TEST_ACCOUNT = Account.from_key("0x" + "ab" * 32)
TEST_PRIVATE_KEY = _TEST_ACCOUNT.key.hex()
TEST_ADDRESS = _TEST_ACCOUNT.address.lower()


@pytest.fixture()
def key_path(tmp_path):
    return str(tmp_path / "certs" / "node-identity.key")


# ---------------------------------------------------------------------------
# 1. Plaintext (no passphrase) — create and reload
# ---------------------------------------------------------------------------

def test_create_plaintext_no_passphrase(key_path):
    pk, addr = load_or_create_identity(key_path)

    assert os.path.isfile(key_path)
    content = open(key_path).read().strip()
    assert not _is_keystore_json(content), "expected raw hex, got keystore JSON"
    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o600"

    # Idempotent reload returns same key/address
    pk2, addr2 = load_or_create_identity(key_path)
    assert pk == pk2
    assert addr == addr2


def test_generation_logs_diagnostic_breadcrumbs(tmp_path, caplog):
    """Identity generation logs the context that lets us root-cause
    "Node ID rotates every restart" from a user's log file alone:
    the key_path it looked for, whether the parent dir existed, what
    else was already in the directory, and the current build variant."""
    import logging
    caplog.set_level(logging.WARNING, logger="app.identity")
    key_path = str(tmp_path / "subdir" / "node-identity.key")

    load_or_create_identity(key_path)

    gen_records = [
        r for r in caplog.records
        if r.name == "app.identity" and "generating a new one" in r.getMessage()
    ]
    assert gen_records, [r.getMessage() for r in caplog.records]
    msg = gen_records[0].getMessage()
    assert "parent_dir=" in msg
    assert "exists=" in msg
    assert "parent_contents=" in msg
    assert "build_variant=" in msg


def test_reload_logs_load_path_and_variant(key_path, caplog):
    import logging
    load_or_create_identity(key_path)  # first-run creates
    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")

    load_or_create_identity(key_path)  # reload

    loaded = [
        r for r in caplog.records
        if r.name == "app.identity"
        and "Loaded node identity" in r.getMessage()
    ]
    assert loaded
    assert "build_variant=" in loaded[0].getMessage()


# ---------------------------------------------------------------------------
# 2. Keystore JSON (with passphrase) — create and round-trip
# ---------------------------------------------------------------------------

def test_create_keystore_with_passphrase(key_path):
    pk, addr = load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    assert os.path.isfile(key_path)
    content = open(key_path).read()
    assert _is_keystore_json(content), "expected keystore JSON, got raw hex"
    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o600"

    # Round-trip: reload with correct passphrase
    pk2, addr2 = load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)
    assert pk == pk2
    assert addr == addr2


# ---------------------------------------------------------------------------
# 3. Wrong passphrase raises KeystoreWrongPassphrase
# ---------------------------------------------------------------------------

def test_load_keystore_wrong_passphrase(key_path):
    """Wrong passphrase must raise the distinct ``KeystoreWrongPassphrase``
    subclass so the state machine routes to PASSPHRASE_REQUIRED with a
    "passphrase is incorrect" message — pre-rc.3 raised a generic
    ValueError that was classified as IDENTITY_KEY_ERROR ("Try Fresh
    Restart"), which destroys the user's identity if they comply.
    """
    from app.identity import KeystoreWrongPassphrase

    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    with pytest.raises(KeystoreWrongPassphrase, match="incorrect"):
        load_or_create_identity(key_path, passphrase="wrong-passphrase")


def test_keystore_wrong_passphrase_is_subclass_of_passphrase_required():
    """Existing handlers that route to PASSPHRASE_REQUIRED on
    ``except KeystorePassphraseRequired:`` must also catch the wrong-
    passphrase case — the resolution is identical (re-prompt), only the
    surfaced reason differs.
    """
    from app.identity import KeystorePassphraseRequired, KeystoreWrongPassphrase
    assert issubclass(KeystoreWrongPassphrase, KeystorePassphraseRequired)


# ---------------------------------------------------------------------------
# 4. Keystore exists but no passphrase raises KeystorePassphraseRequired
# ---------------------------------------------------------------------------

def test_load_keystore_no_passphrase_raises(key_path):
    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    with pytest.raises(KeystorePassphraseRequired, match="SR_IDENTITY_PASSPHRASE"):
        load_or_create_identity(key_path, passphrase="")


# ---------------------------------------------------------------------------
# 5. Migration: plaintext → keystore JSON when passphrase is added
# ---------------------------------------------------------------------------

def test_migrate_plaintext_to_keystore(key_path):
    # First run: no passphrase → raw hex
    pk, addr = load_or_create_identity(key_path)
    assert not _is_keystore_json(open(key_path).read())

    # Second run: passphrase supplied → migrates in-place
    pk2, addr2 = load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)
    assert _is_keystore_json(open(key_path).read()), "file should now be keystore JSON"
    assert pk == pk2, "private key must be unchanged after migration"
    assert addr == addr2, "address must be unchanged after migration"


# ---------------------------------------------------------------------------
# 6. Backward compatibility: manually-written raw hex file loads correctly
# ---------------------------------------------------------------------------

def test_plaintext_backward_compat(key_path):
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "w") as f:
        f.write(TEST_PRIVATE_KEY + "\n")
    os.chmod(key_path, 0o600)

    pk, addr = load_or_create_identity(key_path)
    assert pk == TEST_PRIVATE_KEY
    assert addr == TEST_ADDRESS


# ---------------------------------------------------------------------------
# 7. Output key format is valid for signing (Account.from_key works)
# ---------------------------------------------------------------------------

def test_output_key_format_unchanged(key_path):
    pk, addr = load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)
    # If the returned key can be used with Account.from_key, signing will work
    account = Account.from_key(pk)
    assert account.address.lower() == addr


# ---------------------------------------------------------------------------
# 8. _is_keystore_json detection
# ---------------------------------------------------------------------------

def test_is_keystore_json_detection():
    keystore = Account.encrypt(TEST_PRIVATE_KEY, TEST_PASSPHRASE)
    assert _is_keystore_json(json.dumps(keystore))

    assert not _is_keystore_json(TEST_PRIVATE_KEY)
    assert not _is_keystore_json("0x" + "ab" * 32)
    assert not _is_keystore_json("")
    assert not _is_keystore_json("{not valid json")
    assert not _is_keystore_json('{"no_crypto_key": true}')


# ---------------------------------------------------------------------------
# write_identity_key helper
# ---------------------------------------------------------------------------

def test_write_identity_key_plaintext(key_path):
    addr = write_identity_key(key_path, TEST_PRIVATE_KEY)
    assert addr == TEST_ADDRESS
    assert not _is_keystore_json(open(key_path).read())


def test_write_identity_key_with_passphrase(key_path):
    addr = write_identity_key(key_path, TEST_PRIVATE_KEY, passphrase=TEST_PASSPHRASE)
    assert addr == TEST_ADDRESS
    assert _is_keystore_json(open(key_path).read())
    # Reload verifies decrypt works
    pk, addr2 = load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)
    assert addr2 == TEST_ADDRESS


def test_write_identity_key_invalid_raises(key_path):
    with pytest.raises(Exception):
        write_identity_key(key_path, "not-a-valid-key")


# ---------------------------------------------------------------------------
# R5: Identity-key permission hardening (A9-a)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod semantics")
def test_chmod_0600_on_new_write(key_path, monkeypatch):
    """Newly generated identity files must land at mode 0o600, regardless of
    the process umask (operators occasionally launch the daemon under a wider
    umask than 022)."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)
    # Force a permissive umask so a default-mode write would land at 0o666.
    old_umask = os.umask(0o000)
    try:
        load_or_create_identity(key_path)
    finally:
        os.umask(old_umask)
    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o600"

    # write_identity_key path
    other_path = key_path + ".other"
    old_umask = os.umask(0o000)
    try:
        write_identity_key(other_path, TEST_PRIVATE_KEY)
    finally:
        os.umask(old_umask)
    assert oct(stat.S_IMODE(os.stat(other_path).st_mode)) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod semantics")
def test_chmod_0600_fix_on_launch_when_world_readable(key_path, caplog, monkeypatch):
    """Operators upgrading from pre-R5 builds may have a 0o644 (or worse)
    identity key on disk. Reloading via load_or_create_identity must clamp
    it to 0o600 in-place and log INFO."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)
    # Seed a plaintext identity, then deliberately loosen its permissions
    # to mimic a pre-R5 file written under the umask default.
    load_or_create_identity(key_path)
    os.chmod(key_path, 0o644)
    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o644"

    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")
    load_or_create_identity(key_path)

    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o600"
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.identity"]
    assert any("Restricted identity key permissions to 0600" in m for m in msgs), msgs
    assert any("0o644" in m for m in msgs), msgs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod semantics")
def test_chmod_fix_skipped_when_already_0600(key_path, caplog, monkeypatch):
    """No INFO log when the key is already at 0o600 — keep startup quiet."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)
    load_or_create_identity(key_path)
    assert oct(stat.S_IMODE(os.stat(key_path).st_mode)) == "0o600"

    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")
    load_or_create_identity(key_path)

    msgs = [r.getMessage() for r in caplog.records if r.name == "app.identity"]
    assert not any("Restricted identity key permissions" in m for m in msgs), msgs


def test_unencrypted_hint_logged_when_no_passphrase(key_path, caplog, monkeypatch):
    """Plaintext key + no SR_IDENTITY_PASSPHRASE → one INFO hint per launch."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)
    load_or_create_identity(key_path)  # first run creates plaintext

    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")
    load_or_create_identity(key_path)  # reload triggers the hint

    hints = [
        r for r in caplog.records
        if r.name == "app.identity"
        and "Identity key is unencrypted" in r.getMessage()
        and "SR_IDENTITY_PASSPHRASE" in r.getMessage()
    ]
    assert len(hints) == 1, [r.getMessage() for r in caplog.records]


def test_no_hint_when_keystore_encrypted(key_path, caplog, monkeypatch):
    """Encrypted keystore → no unencrypted-hint noise."""
    monkeypatch.setenv("SR_IDENTITY_PASSPHRASE", TEST_PASSPHRASE)
    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")
    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    hints = [
        r for r in caplog.records
        if r.name == "app.identity"
        and "Identity key is unencrypted" in r.getMessage()
    ]
    assert not hints, [r.getMessage() for r in caplog.records]


def test_no_hint_when_passphrase_set_but_plaintext_pending_migration(
    key_path, caplog, monkeypatch,
):
    """Plaintext-on-disk that gets migrated to keystore mid-call must not log
    the hint — by the time we check, the file is encrypted."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)
    load_or_create_identity(key_path)  # plaintext

    monkeypatch.setenv("SR_IDENTITY_PASSPHRASE", TEST_PASSPHRASE)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.identity")
    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)  # migrates

    hints = [
        r for r in caplog.records
        if r.name == "app.identity"
        and "Identity key is unencrypted" in r.getMessage()
    ]
    assert not hints, [r.getMessage() for r in caplog.records]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only ACL hardening")
def test_windows_acl_restriction_attempted(key_path):
    """On Windows, _restrict_permissions must shell out to icacls with the
    expected lockdown args. We mock subprocess.run so the test runs on any
    Windows host without modifying real ACLs."""
    with mock.patch("app.identity.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)
        load_or_create_identity(key_path)

    assert mock_run.called, "icacls should have been invoked"
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "icacls"
    assert cmd[1] == key_path
    assert "/inheritance:r" in cmd
    assert "/grant:r" in cmd
    # The user spec is "<username>:F" — last positional cmd element.
    user_spec = cmd[-1]
    assert user_spec.endswith(":F"), cmd


def test_windows_acl_invoked_via_helper_on_simulated_win32(key_path):
    """Cross-platform test: force the helper to take the Windows branch and
    verify subprocess.run is called with the right args. This runs on POSIX
    too so it stays in the regular test matrix."""
    from app import identity

    with mock.patch.object(identity.sys, "platform", "win32"), \
         mock.patch.object(identity.subprocess, "run") as mock_run, \
         mock.patch("getpass.getuser", return_value="testuser"):
        mock_run.return_value = mock.Mock(returncode=0)
        identity._restrict_permissions(key_path)

    assert mock_run.called
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "icacls",
        key_path,
        "/inheritance:r",
        "/grant:r",
        "testuser:F",
    ]


def test_windows_acl_failure_logs_warn_does_not_raise(key_path, caplog):
    """If icacls exits non-zero (or is missing), the helper must swallow the
    error with a WARN log so startup is never blocked by hardening."""
    import subprocess as _sp

    from app import identity

    caplog.set_level(logging.WARNING, logger="app.identity")
    with mock.patch.object(identity.sys, "platform", "win32"), \
         mock.patch.object(identity.subprocess, "run",
                           side_effect=_sp.CalledProcessError(1, "icacls")), \
         mock.patch("getpass.getuser", return_value="testuser"):
        # Must not raise.
        identity._restrict_permissions(key_path)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("icacls" in r.getMessage() for r in warns), [r.getMessage() for r in warns]
