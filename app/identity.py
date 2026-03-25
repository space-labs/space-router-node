"""Node identity keypair management.

Generates and persists a secp256k1 keypair used for signing authenticated
API requests to the Coordination API.  The private key stays on the node
machine and is never transmitted.
"""

import logging
import os
import time

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

logger = logging.getLogger(__name__)

_w3 = Web3()


def load_or_create_identity(key_path: str) -> tuple[str, str]:
    """Load or generate a secp256k1 identity keypair.

    Returns ``(private_key_hex, node_address)``.

    On first run: generates a new key, saves the hex-encoded private key
    to *key_path* with ``0o600`` permissions.
    On subsequent runs: loads the key from *key_path*.
    """
    if os.path.isfile(key_path):
        with open(key_path) as f:
            private_key = f.read().strip()
        account = Account.from_key(private_key)
        logger.info("Loaded node identity from %s: %s", key_path, account.address)
        return private_key, account.address.lower()

    # Generate a new identity
    account = Account.create()
    private_key = account.key.hex()

    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
    with open(key_path, "w") as f:
        f.write(private_key + "\n")
    os.chmod(key_path, 0o600)

    logger.info("Generated new node identity at %s: %s", key_path, account.address)
    return private_key, account.address.lower()


def sign_request(private_key: str, action: str, target: str) -> tuple[str, int]:
    """Sign a Space Router API request.

    Creates an EIP-191 signature of ``space-router:{action}:{target}:{timestamp}``.

    *target* is the ``node_id`` for most actions, or ``wallet_address`` for
    registration.

    Returns ``(signature_hex, timestamp)``.
    """
    timestamp = int(time.time())
    message_text = f"space-router:{action}:{target}:{timestamp}"
    message = encode_defunct(text=message_text)
    signed = _w3.eth.account.sign_message(message, private_key=private_key)
    return signed.signature.hex(), timestamp
