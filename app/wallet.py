"""EVM wallet key management and cryptographic challenge signing.

On first startup the Home Node auto-generates a secp256k1 private key (just
like TLS certificates in ``tls.py``).  The key is persisted to disk so the
same wallet address is reused across restarts.

During registration the derived Ethereum address is sent as ``wallet_address``.
During the Coordination API challenge probe the node signs its public IP with
the private key, proving ownership.
"""

import logging
import os
import secrets

from eth_keys import keys
from eth_hash.auto import keccak

logger = logging.getLogger(__name__)


def ensure_wallet_key(key_path: str) -> str:
    """Load an existing wallet key or generate a new one.

    Returns the hex-encoded private key (without ``0x`` prefix).
    """
    if os.path.isfile(key_path):
        with open(key_path) as f:
            hex_key = f.read().strip().removeprefix("0x")
        logger.info("Wallet key loaded from %s", key_path)
        return hex_key

    logger.info("Generating new wallet key …")
    private_bytes = secrets.token_bytes(32)
    hex_key = private_bytes.hex()

    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, hex_key.encode())
    finally:
        os.close(fd)

    logger.info("Wallet key saved to %s", key_path)
    return hex_key


def private_key_to_address(private_key_hex: str) -> str:
    """Derive the checksummed Ethereum address from a hex-encoded private key."""
    pk = keys.PrivateKey(bytes.fromhex(private_key_hex.removeprefix("0x")))
    return pk.public_key.to_checksum_address()


def sign_challenge(private_key_hex: str, public_ip: str) -> str:
    """Sign the challenge message and return the hex-encoded signature.

    Message: ``"SpaceRouter challenge: <public_ip>"``
    Signing scheme: EIP-191 personal sign (``\\x19Ethereum Signed Message:\\n`` prefix).

    Returns a 130-character hex string (65 bytes: 32B r + 32B s + 1B v).
    """
    message = f"SpaceRouter challenge: {public_ip}".encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(message)}".encode("utf-8")
    msg_hash = keccak(prefix + message)

    pk = keys.PrivateKey(bytes.fromhex(private_key_hex.removeprefix("0x")))
    signature = pk.sign_msg_hash(msg_hash)

    return signature.to_bytes().hex()


def verify_challenge(signature_hex: str, public_ip: str, expected_address: str) -> bool:
    """Verify a challenge signature against the expected wallet address.

    Recovers the signer's address from the signature and compares it
    (case-insensitive) to *expected_address*.
    """
    message = f"SpaceRouter challenge: {public_ip}".encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(message)}".encode("utf-8")
    msg_hash = keccak(prefix + message)

    sig = keys.Signature(bytes.fromhex(signature_hex))
    recovered_pk = sig.recover_public_key_from_msg_hash(msg_hash)
    recovered_address = recovered_pk.to_checksum_address()

    return recovered_address.lower() == expected_address.lower()
