"""Unit tests for app/identity.py — keystore encryption, decryption, and migration."""

import json
import os
import stat

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
# 3. Wrong passphrase raises ValueError
# ---------------------------------------------------------------------------

def test_load_keystore_wrong_passphrase(key_path):
    load_or_create_identity(key_path, passphrase=TEST_PASSPHRASE)

    with pytest.raises(ValueError, match="Failed to decrypt"):
        load_or_create_identity(key_path, passphrase="wrong-passphrase")


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
