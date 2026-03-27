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

import json
import logging
import os
import time

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

logger = logging.getLogger(__name__)

_w3 = Web3()


class KeystorePassphraseRequired(Exception):
    """Raised when a keystore JSON file is found but no passphrase was supplied."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_keystore_json(content: str) -> bool:
    """Return True if *content* looks like a Web3 keystore JSON file."""
    try:
        data = json.loads(content)
        return isinstance(data, dict) and ("crypto" in data or "Crypto" in data)
    except (json.JSONDecodeError, ValueError):
        return False


def _migrate_to_keystore(key_path: str, private_key: str, passphrase: str) -> None:
    """Encrypt *private_key* and atomically replace *key_path* with keystore JSON."""
    keystore = Account.encrypt(private_key, passphrase)
    tmp_path = key_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(keystore, f)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, key_path)
    logger.info("Migrated identity key to encrypted keystore at %s", key_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_or_create_identity(key_path: str, passphrase: str = "") -> tuple[str, str]:
    """Load or generate a secp256k1 identity keypair.

    Returns ``(private_key_hex, node_address)``.

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
    if os.path.isfile(key_path):
        with open(key_path) as f:
            content = f.read().strip()

        if _is_keystore_json(content):
            if not passphrase:
                raise KeystorePassphraseRequired(
                    f"Encrypted keystore found at {key_path!r} but no passphrase "
                    "was supplied (SR_IDENTITY_PASSPHRASE is not set)."
                )
            try:
                private_key_bytes = Account.decrypt(json.loads(content), passphrase)
            except Exception as exc:
                logger.error(
                    "Failed to decrypt identity keystore at %s — check SR_IDENTITY_PASSPHRASE",
                    key_path,
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

        account = Account.from_key(private_key)
        logger.info("Loaded node identity from %s: %s", key_path, account.address)
        return private_key, account.address.lower()

    # --- No file: generate a new identity ---
    account = Account.create()
    private_key = account.key.hex()

    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)

    if passphrase:
        keystore = Account.encrypt(private_key, passphrase)
        with open(key_path, "w") as f:
            json.dump(keystore, f)
    else:
        with open(key_path, "w") as f:
            f.write(private_key + "\n")

    os.chmod(key_path, 0o600)
    logger.info("Generated new node identity at %s: %s", key_path, account.address)
    return private_key, account.address.lower()


def write_identity_key(key_path: str, private_key_hex: str, passphrase: str = "") -> str:
    """Write an externally-provided private key to *key_path*.

    Used during first-run setup when the user imports an existing key.
    Returns the derived node address (lowercase).

    Raises ``ValueError`` if *private_key_hex* is not a valid secp256k1 key.
    """
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

    os.chmod(key_path, 0o600)
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
    if timestamp is None:
        timestamp = int(time.time())
    message_text = f"space-router:vouch:{staking_address}:{collection_address}:{timestamp}"
    message = encode_defunct(text=message_text)
    signed = _w3.eth.account.sign_message(message, private_key=private_key)
    return signed.signature.hex(), timestamp
