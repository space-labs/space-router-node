"""Node identity keypair management.

Generates and persists a secp256k1 keypair used for signing authenticated
API requests to the Coordination API.  The private key stays on the node
machine and is never transmitted.

Storage formats
---------------
- **Plaintext** (no passphrase): hex-encoded private key, e.g. ``0xabc...``
- **Keystore JSON** (passphrase set): standard Ethereum Web3 keystore JSON,
  produced by ``eth_account.Account.encrypt()``.

Format is detected automatically by content inspection: keystore JSON always
starts with ``{`` and contains a ``"crypto"`` or ``"Crypto"`` key; raw hex
never does.

Migration
---------
If a plaintext file exists and a passphrase is supplied on a subsequent run,
the file is automatically migrated to keystore JSON via an atomic rename.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# Lazy imports — these are heavy (web3, eth_account) and slow down startup
# if loaded at module level.  Imported on first use via _lazy_imports().
Account = None
encode_defunct = None
_w3 = None


def _lazy_imports():
    """Import heavy crypto libraries on first use."""
    global Account, encode_defunct, _w3
    if Account is None:
        from eth_account import Account as _Account
        from eth_account.messages import encode_defunct as _encode_defunct
        from web3 import Web3

        Account = _Account
        encode_defunct = _encode_defunct
        _w3 = Web3()


class KeystorePassphraseRequired(Exception):
    """Raised when a keystore JSON file is found but no passphrase was supplied."""


class KeystoreWrongPassphrase(KeystorePassphraseRequired):
    """Raised when the keystore is found but the passphrase fails to decrypt it.

    Subclasses :class:`KeystorePassphraseRequired` so existing handlers that
    route to the ``PASSPHRASE_REQUIRED`` state catch both — the resolution
    is the same: re-prompt the operator. Distinct class so the GUI / state
    machine can surface "passphrase is incorrect" instead of the generic
    "passphrase required" wording.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _restrict_permissions(path: str) -> None:
    """Restrict *path* so only the current user can read/write it.

    POSIX: ``chmod 0600`` (idempotent — only writes if mode is more permissive).
    Windows: best-effort ACL lockdown via ``icacls`` (current user full control,
    inherited ACEs removed). Failures log a WARN but never raise — startup
    must not depend on hardening succeeding.
    """
    if sys.platform == "win32":
        _restrict_permissions_windows(path)
    else:
        _restrict_permissions_posix(path)


def _restrict_permissions_posix(path: str) -> None:
    """POSIX-only ``chmod 0600`` that skips the syscall when already correct."""
    try:
        current_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        logger.warning("Could not stat %r to restrict permissions: %s", path, exc)
        return
    if current_mode != 0o600:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("Could not chmod %r to 0o600: %s", path, exc)


def _restrict_permissions_windows(path: str) -> None:
    """Best-effort Windows ACL lockdown via ``icacls``.

    Grants the current user full control and removes inherited ACEs so
    other local users cannot read the identity key. Logs a WARN on
    failure but never raises — we never want hardening to break startup.
    """
    try:
        import getpass
        username = getpass.getuser()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve current Windows user: %s", exc)
        return
    try:
        subprocess.run(
            [
                "icacls",
                path,
                "/inheritance:r",
                "/grant:r",
                f"{username}:F",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Could not restrict ACL on %r via icacls (user=%s): %s",
            path, username, exc,
        )


def _is_keystore_json(content: str) -> bool:
    """Return True if *content* looks like a Web3 keystore JSON file."""
    try:
        data = json.loads(content)
        return isinstance(data, dict) and ("crypto" in data or "Crypto" in data)
    except (json.JSONDecodeError, ValueError):
        return False


def _migrate_to_keystore(key_path: str, private_key: str, passphrase: str) -> None:
    """Encrypt *private_key* and atomically replace *key_path* with keystore JSON."""
    _lazy_imports()
    keystore = Account.encrypt(private_key, passphrase)
    tmp_path = key_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(keystore, f)
    _restrict_permissions(tmp_path)
    os.replace(tmp_path, key_path)
    _restrict_permissions(key_path)
    logger.info("Migrated identity key to encrypted keystore at %s", key_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_or_create_identity(key_path: str, passphrase: str = "") -> tuple[str, str]:
    """Load or generate a secp256k1 identity keypair.

    Returns ``(private_key_hex, identity_address)``.

    Storage behaviour
    -----------------
    - **File exists, keystore JSON, passphrase provided**: decrypt and return.
    - **File exists, keystore JSON, no passphrase**: raise
      :exc:`KeystorePassphraseRequired` — caller must prompt the user.
    - **File exists, raw hex, passphrase provided**: load key then migrate
      in-place to keystore JSON (atomic rename).
    - **File exists, raw hex, no passphrase**: load as-is (unchanged).
    - **No file, passphrase provided**: generate new key, write keystore JSON.
    - **No file, no passphrase**: generate new key, write raw hex.
    """
    _lazy_imports()
    if os.path.isfile(key_path):
        with open(key_path) as f:
            content = f.read().strip()

        is_keystore = _is_keystore_json(content)

        if is_keystore:
            if not passphrase:
                raise KeystorePassphraseRequired(
                    f"Encrypted keystore found at {key_path!r} but no passphrase "
                    "was supplied (SR_IDENTITY_PASSPHRASE is not set)."
                )
            try:
                private_key_bytes = Account.decrypt(json.loads(content), passphrase)
            except ValueError as exc:
                # eth_account raises ValueError("MAC mismatch") specifically
                # on wrong passphrase. Surface that as a distinct exception
                # so the state machine can route to PASSPHRASE_REQUIRED with
                # a "passphrase is incorrect" message — not the generic
                # "Try Fresh Restart" path used for unrecoverable key errors.
                logger.error(
                    "Wrong passphrase for identity keystore at %s",
                    key_path,
                )
                raise KeystoreWrongPassphrase(
                    f"Identity keystore at {key_path!r} could not be "
                    f"decrypted — passphrase is incorrect."
                ) from exc
            except Exception as exc:
                logger.error(
                    "Failed to decrypt identity keystore at %s: %s",
                    key_path, exc,
                )
                raise ValueError(
                    f"Failed to decrypt identity keystore: {exc}"
                ) from exc
            private_key = private_key_bytes.hex()
        else:
            # Raw hex file
            private_key = content
            if passphrase:
                _migrate_to_keystore(key_path, private_key, passphrase)
                # After migration the file is keystore JSON — reflect that so
                # the unencrypted-key hint below is suppressed.
                is_keystore = True

        # In-place permissions fix (POSIX only — Windows ACLs are not reflected
        # in st_mode). Catches operators upgrading from pre-R5 builds where the
        # key may have been written with the umask default (often 0o644).
        if sys.platform != "win32":
            try:
                current_mode = stat.S_IMODE(os.stat(key_path).st_mode)
            except OSError as exc:
                logger.warning(
                    "Could not stat identity key %r to verify permissions: %s",
                    key_path, exc,
                )
            else:
                if current_mode != 0o600:
                    try:
                        os.chmod(key_path, 0o600)
                        logger.info(
                            "Restricted identity key permissions to 0600 (was %s)",
                            oct(current_mode),
                        )
                    except OSError as exc:
                        logger.warning(
                            "Could not chmod identity key %r to 0o600: %s",
                            key_path, exc,
                        )

        # Opt-in encryption hint — log once per launch when the on-disk key is
        # plaintext hex AND no passphrase is configured. INFO only; never
        # gates startup. See A9-a in the v1.5 plan.
        if not is_keystore and not os.environ.get("SR_IDENTITY_PASSPHRASE"):
            logger.info(
                "Identity key is unencrypted. Set SR_IDENTITY_PASSPHRASE before "
                "next launch to encrypt it (Web3 keystore format). "
                "See docs/identity-encryption.md."
            )

        account = Account.from_key(private_key)
        try:
            from app.variant import BUILD_VARIANT as _variant
        except Exception:  # noqa: BLE001
            _variant = "<unknown>"
        logger.info(
            "Loaded node identity from %s: %s (build_variant=%s)",
            key_path, account.address, _variant,
        )
        return private_key, account.address.lower()

    # --- No file: generate a new identity ---
    # Diagnostic breadcrumbs so we can root-cause the "Node ID rotates
    # every restart" symptom QA hit on the macOS GUI build. If key_path
    # is stable but the file still isn't here on a second launch, the
    # logs tell us either (a) BUILD_VARIANT flipped and we're looking
    # at a different directory, (b) the parent directory was cleared
    # between launches, or (c) the first-launch write silently failed.
    parent_dir = os.path.dirname(key_path) or "."
    try:
        parent_exists = os.path.isdir(parent_dir)
        parent_contents = sorted(os.listdir(parent_dir)) if parent_exists else []
    except OSError as exc:
        parent_exists = False
        parent_contents = [f"<listdir error: {exc}>"]

    try:
        from app.variant import BUILD_VARIANT as _variant
    except Exception:  # noqa: BLE001
        _variant = "<unknown>"

    logger.warning(
        "No identity key at %r — generating a new one. "
        "parent_dir=%r exists=%s parent_contents=%r build_variant=%r",
        key_path, parent_dir, parent_exists, parent_contents, _variant,
    )

    account = Account.create()
    private_key = account.key.hex()

    os.makedirs(parent_dir, exist_ok=True)

    if passphrase:
        keystore = Account.encrypt(private_key, passphrase)
        with open(key_path, "w") as f:
            json.dump(keystore, f)
    else:
        with open(key_path, "w") as f:
            f.write(private_key + "\n")

    _restrict_permissions(key_path)
    # Confirm the write landed where we expected.
    try:
        written_size = os.path.getsize(key_path)
    except OSError as exc:
        written_size = -1
        logger.error(
            "Identity key write appears to have failed — stat(%r) raised %s",
            key_path, exc,
        )
    logger.info(
        "Generated new node identity at %s: %s (file size=%d)",
        key_path, account.address, written_size,
    )
    return private_key, account.address.lower()


def write_identity_key(key_path: str, private_key_hex: str, passphrase: str = "") -> str:
    """Write an externally-provided private key to *key_path*.

    Used during first-run setup when the user imports an existing key.
    Returns the derived node address (lowercase).

    Raises ``ValueError`` if *private_key_hex* is not a valid secp256k1 key.
    """
    _lazy_imports()
    account = Account.from_key(private_key_hex)  # raises ValueError if invalid
    private_key = account.key.hex()

    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)

    if passphrase:
        keystore = Account.encrypt(private_key, passphrase)
        with open(key_path, "w") as f:
            json.dump(keystore, f)
    else:
        with open(key_path, "w") as f:
            f.write(private_key + "\n")

    _restrict_permissions(key_path)
    logger.info("Wrote imported identity key to %s: %s", key_path, account.address)
    return account.address.lower()


def sign_request(
    private_key: str,
    action: str,
    target: str,
    *,
    timestamp: int | None = None,
) -> tuple[str, int]:
    """Sign a Space Router API request.

    Creates an EIP-191 signature of ``space-router:{action}:{target}:{timestamp}``.

    *target* is the ``node_id`` for most actions, or ``staking_address`` for
    registration.  Pass *timestamp* to reuse a previously generated value
    (required when multiple signatures must share the same timestamp, e.g.
    the identity and vouching signatures during v0.2.0 registration).

    Returns ``(signature_hex, timestamp)``.
    """
    _lazy_imports()
    if timestamp is None:
        timestamp = int(time.time())
    message_text = f"space-router:{action}:{target}:{timestamp}"
    message = encode_defunct(text=message_text)
    signed = _w3.eth.account.sign_message(message, private_key=private_key)
    return signed.signature.hex(), timestamp


def sign_vouch(
    private_key: str,
    staking_address: str,
    collection_address: str,
    timestamp: int | None = None,
) -> tuple[str, int]:
    """Sign a vouching message binding the identity to staking + collection wallets.

    Creates an EIP-191 signature of
    ``space-router:vouch:{staking_address}:{collection_address}:{timestamp}``.

    Returns ``(signature_hex, timestamp)``.
    """
    _lazy_imports()
    if timestamp is None:
        timestamp = int(time.time())
    message_text = f"space-router:vouch:{staking_address}:{collection_address}:{timestamp}"
    message = encode_defunct(text=message_text)
    signed = _w3.eth.account.sign_message(message, private_key=private_key)
    return signed.signature.hex(), timestamp
