"""EVM wallet address validation.

The Home Node receives a wallet address from the operator (via
``SR_WALLET_ADDRESS``).  This module validates the address format before
it is used for registration and challenge-probe responses.
"""

import re

_EVM_ADDRESS_RE = re.compile(r"^(0x)?[0-9a-fA-F]{40}$")


def validate_wallet_address(address: str) -> str:
    """Validate and normalise an EVM wallet address.

    Accepts ``0x``-prefixed or bare 40-hex-char addresses.
    Returns the lowercased ``0x``-prefixed form.

    Raises ``ValueError`` for invalid input.
    """
    if not address or not _EVM_ADDRESS_RE.match(address):
        raise ValueError(
            f"Invalid EVM wallet address: {address!r} "
            "(expected 0x followed by 40 hex characters)"
        )
    bare = address.removeprefix("0x").removeprefix("0X")
    return f"0x{bare.lower()}"
