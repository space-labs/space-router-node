"""Item 9: is bare 40-hex accepted, and is the STORED value normalised?

QA reported item 9 FAIL because the RC notes said a bare 40-char hex string
should be REJECTED. BUG-06 (test.131) says the opposite and is the intent:
"address validation accepts a bare 40-hex address and normalises to 0x
(GUI + Settings), matching the CLI; bare-hex zero is still blocked."

So acceptance is correct. The open question is whether the GUI path normalises
what it STORES, or keeps the bare form -- QA judged from the on-screen value.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.wallet import validate_wallet_address

_BARE = "ab" * 20                 # 40 hex chars, no 0x
_PREFIXED = "0x" + "ab" * 20


def test_bare_40_hex_is_accepted_and_normalised():
    """BUG-06's stated intent, at the validation layer."""
    out = validate_wallet_address(_BARE)
    assert out.lower().startswith("0x"), (
        f"bare hex was not normalised to 0x form: {out!r}"
    )
    assert out.lower() == _PREFIXED.lower()


def test_prefixed_address_is_unchanged():
    assert validate_wallet_address(_PREFIXED).lower() == _PREFIXED.lower()


def test_zero_address_is_blocked_at_the_caller_not_the_validator():
    """BUG-06 kept the zero address blocked -- one layer up.

    validate_wallet_address NORMALISES and returns 0x000...0; the block lives
    in the callers (CLI wizard and the GUI form), which is correct layering.
    Asserted here so nobody "fixes" the validator and silently moves the
    guarantee.
    """
    assert validate_wallet_address("0" * 40) == "0x" + "0" * 40, (
        "the validator no longer normalises the zero address — check whether "
        "the callers still block it"
    )

    cli = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '_ZERO_ADDRESS = "0x" + "0" * 40' in cli
    assert "is the zero address" in cli, (
        "the CLI wizard no longer blocks the zero staking address"
    )

    js = (Path(__file__).resolve().parents[1] / "gui" / "assets" / "app.js").read_text()
    assert "zero" in js.lower(), (
        "the GUI form no longer references a zero-address guard"
    )


def test_gui_save_path_stores_the_normalised_form():
    """The GUI stores via config_store, which must normalise before saving.

    If this passes, QA's 'no normalization at all' observation is about the
    on-screen field, not the persisted value -- cosmetic, not a data bug.
    """
    import inspect

    from gui import config_store

    src = inspect.getsource(config_store)
    assert "normalised_staking = validate_wallet_address(" in src, (
        "the GUI save path no longer normalises the staking address"
    )
    assert "s.wallet.staking_address = normalised_staking" in src, (
        "the GUI save path stores something other than the normalised value"
    )
