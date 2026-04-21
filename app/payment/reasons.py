"""Error codes and human-readable messages for Leg 2 receipt failures.

Two failure surfaces:

- **Sign failures** — the coord API / gateway refused to sign a receipt the
  provider submitted (after-relay ``POST /nodes/{id}/receipts`` or an async
  rejection surfaced via ``GET /nodes/{id}/rejected-receipts``).
- **Claim failures** — the on-chain ``claimBatch`` tx reverted or timed out.

Both counters are capped independently at :data:`MAX_SIGN_ATTEMPTS` /
:data:`MAX_CLAIM_ATTEMPTS` (overridable via env). When a counter hits its
cap, the row is locked (``locked = 1``) and hidden from automatic retry
selectors. Locked rows remain visible for audit.
"""

from __future__ import annotations

import os

# --- Sign-side codes ---------------------------------------------------------

SIGN_REJECTED_UNREGISTERED_NODE = "SIGN_REJECTED_UNREGISTERED_NODE"
SIGN_REJECTED_BYTE_MISMATCH = "SIGN_REJECTED_BYTE_MISMATCH"
SIGN_REJECTED_PRICE_CAP = "SIGN_REJECTED_PRICE_CAP"
SIGN_REJECTED_BAD_SIGNATURE = "SIGN_REJECTED_BAD_SIGNATURE"
SIGN_REJECTED_UNKNOWN_REQUEST = "SIGN_REJECTED_UNKNOWN_REQUEST"
SIGN_TIMEOUT = "SIGN_TIMEOUT"

# --- Claim-side codes --------------------------------------------------------

CLAIM_REVERTED = "CLAIM_REVERTED"
CLAIM_RPC_UNREACHABLE = "CLAIM_RPC_UNREACHABLE"
CLAIM_TX_TIMEOUT = "CLAIM_TX_TIMEOUT"
CLAIM_NONCE_ALREADY_USED = "CLAIM_NONCE_ALREADY_USED"

SIGN_CODES = frozenset({
    SIGN_REJECTED_UNREGISTERED_NODE,
    SIGN_REJECTED_BYTE_MISMATCH,
    SIGN_REJECTED_PRICE_CAP,
    SIGN_REJECTED_BAD_SIGNATURE,
    SIGN_REJECTED_UNKNOWN_REQUEST,
    SIGN_TIMEOUT,
})

CLAIM_CODES = frozenset({
    CLAIM_REVERTED,
    CLAIM_RPC_UNREACHABLE,
    CLAIM_TX_TIMEOUT,
    CLAIM_NONCE_ALREADY_USED,
})

ALL_CODES = SIGN_CODES | CLAIM_CODES

MESSAGES: dict[str, str] = {
    SIGN_REJECTED_UNREGISTERED_NODE:
        "Your node wallet is not registered in the escrow contract. "
        "Contact support to complete on-chain registration.",
    SIGN_REJECTED_BYTE_MISMATCH:
        "The gateway's measured traffic disagreed with your node's report.",
    SIGN_REJECTED_PRICE_CAP:
        "The receipt's rate exceeded the network's price cap.",
    SIGN_REJECTED_BAD_SIGNATURE:
        "The submission signature didn't verify against your identity key.",
    SIGN_REJECTED_UNKNOWN_REQUEST:
        "The gateway has no record of this traffic — receipt cannot be signed.",
    SIGN_TIMEOUT:
        "The signing service didn't respond in time. Will retry automatically.",
    CLAIM_REVERTED:
        "The on-chain claim transaction reverted.",
    CLAIM_RPC_UNREACHABLE:
        "The Creditcoin RPC endpoint was unreachable. Will retry automatically.",
    CLAIM_TX_TIMEOUT:
        "The claim transaction took longer than expected to confirm.",
    CLAIM_NONCE_ALREADY_USED:
        "This receipt was already settled on-chain — no further action needed.",
}


def message_for(code: str | None) -> str:
    """Return a user-facing message for an error code, or empty string."""
    if not code:
        return ""
    return MESSAGES.get(code, code)


def is_sign_code(code: str | None) -> bool:
    return code in SIGN_CODES if code else False


def is_claim_code(code: str | None) -> bool:
    return code in CLAIM_CODES if code else False


# Attempt caps — override via env for QA.
MAX_SIGN_ATTEMPTS = int(os.environ.get("SR_RECEIPT_MAX_SIGN_ATTEMPTS", "2"))
MAX_CLAIM_ATTEMPTS = int(os.environ.get("SR_RECEIPT_MAX_CLAIM_ATTEMPTS", "2"))


# Transient codes don't increment the attempts counter — they're retried
# indefinitely. Only explicit rejections / terminal tx failures count.
TRANSIENT_CODES = frozenset({
    SIGN_TIMEOUT,
    CLAIM_RPC_UNREACHABLE,
    CLAIM_TX_TIMEOUT,
})


def counts_against_retry_budget(code: str | None) -> bool:
    """Return True if this failure should increment sign/claim_attempts."""
    if not code:
        return False
    return code not in TRANSIENT_CODES
