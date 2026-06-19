"""Workstream B fixes from the Build 129 QA report.

B2 (MED 2, production risk): the collection address must follow a staking
change unless the operator explicitly set a different collection. Driven
through a REAL ``ConfigStore`` + ``Api`` on disk (only the network coord
lookup is stubbed, since it is an external dependency, not the logic here).

B3 (MED 3): the CLI wizard must block the zero address and re-prompt,
matching the GUI Settings gate. Driven through the REAL ``_first_run_setup``
wizard with the TTY primitives scripted (equivalent to piping stdin).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
C = "0x" + "cc" * 20
ZERO = "0x" + "00" * 20
VALID = "0x" + "12" * 20


# ── B2: collection follows staking unless explicit ──────────────────────────

def _real_api(monkeypatch):
    """Real ConfigStore + Api; only the on-chain coord lookup is stubbed."""
    from gui.api import Api
    from gui.config_store import ConfigStore

    monkeypatch.setattr(
        Api, "validate_staking_address",
        lambda self, address: {"ok": True, "status": "earning", "message": ""},
    )
    cfg = ConfigStore()  # writes into the isolated HOME ~/.spacerouter
    return Api(config=cfg, node_manager=MagicMock()), cfg


def test_blank_collection_is_stored_as_none(monkeypatch):
    _, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, "")
    s = cfg._load_settings_v2()
    assert s.wallet.staking_address == A
    # No eager copy of staking into collection.
    assert s.wallet.collection_address is None


def test_explicit_collection_is_persisted(monkeypatch):
    _, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, B)
    s = cfg._load_settings_v2()
    assert s.wallet.collection_address == B


def test_collection_follows_staking_change_and_revert(monkeypatch):
    """The exact QA repro: onboard with blank collection, change staking,
    revert staking. Collection must never strand at the old wallet."""
    api, cfg = _real_api(monkeypatch)

    cfg.save_wallets(A, "")  # onboarding, no explicit collection
    assert cfg._load_settings_v2().wallet.collection_address is None

    assert api.save_staking_address(B)["ok"] is True
    s = cfg._load_settings_v2()
    assert s.wallet.staking_address == B
    assert s.wallet.collection_address is None  # followed, not stranded at A

    assert api.save_staking_address(A)["ok"] is True
    s = cfg._load_settings_v2()
    assert s.wallet.staking_address == A
    assert s.wallet.collection_address is None


def test_explicit_collection_survives_staking_change(monkeypatch):
    api, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, B)  # operator chose a different collection on purpose
    assert api.save_staking_address(C)["ok"] is True
    s = cfg._load_settings_v2()
    assert s.wallet.staking_address == C
    assert s.wallet.collection_address == B  # preserved


def test_auto_copied_collection_is_dropped_on_change(monkeypatch):
    """Existing pre-v1.5.2 users have collection == staking persisted (the
    old auto-copy). That must be treated as non-explicit and dropped so it
    follows the new staking address."""
    api, cfg = _real_api(monkeypatch)
    # Simulate the legacy state directly: collection equal to staking.
    s = cfg._load_settings_v2()
    s.wallet.staking_address = A
    s.wallet.collection_address = A
    cfg._save_settings_v2(s)

    assert api.save_staking_address(B)["ok"] is True
    s = cfg._load_settings_v2()
    assert s.wallet.staking_address == B
    assert s.wallet.collection_address is None  # not stranded at A


# ── B2: editable Collection address row in Settings ─────────────────────────

def test_get_collection_empty_when_following_staking(monkeypatch):
    api, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, "")  # blank collection → follows staking (None)
    assert api.get_collection_address() == ""


def test_get_collection_returns_explicit_value(monkeypatch):
    api, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, B)
    assert api.get_collection_address() == B


def test_save_collection_blank_follows_staking(monkeypatch):
    api, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, B)  # start explicit
    assert api.save_collection_address("")["ok"] is True
    assert cfg._load_settings_v2().wallet.collection_address is None


def test_save_collection_explicit(monkeypatch):
    api, cfg = _real_api(monkeypatch)
    cfg.save_wallets(A, "")
    res = api.save_collection_address(B)
    assert res["ok"] is True
    assert cfg._load_settings_v2().wallet.collection_address == B


def test_save_collection_rejects_zero(monkeypatch):
    api, _ = _real_api(monkeypatch)
    res = api.save_collection_address(ZERO)
    assert res["ok"] is False


def test_save_collection_rejects_garbage(monkeypatch):
    api, _ = _real_api(monkeypatch)
    res = api.save_collection_address("0xnothex")
    assert res["ok"] is False


# ── B3: CLI wizard blocks the zero address ──────────────────────────────────

def test_cli_wizard_blocks_zero_then_accepts_valid(monkeypatch):
    """Drive the real _first_run_setup wizard. Feed the zero address first,
    then a valid one. Expect the zero rejection and the valid address
    persisted."""
    import app.cli_ui as cli_ui
    import app.main as main

    errors: list[str] = []
    staking_answers = iter([ZERO, VALID])

    def fake_input(prompt, default="", password=False):
        if prompt == "Staking wallet address":
            return next(staking_answers)
        # blank for collection, referral, and anything else
        return ""

    monkeypatch.setattr(cli_ui, "wizard_input", fake_input)
    monkeypatch.setattr(cli_ui, "wizard_select", lambda *a, **k: 0)   # generate key / UPnP
    monkeypatch.setattr(cli_ui, "wizard_confirm", lambda *a, **k: False)  # no passphrase
    monkeypatch.setattr(cli_ui, "wizard_error", lambda msg: errors.append(msg))
    for noop in ("wizard_banner", "wizard_step", "wizard_info",
                 "wizard_success", "wizard_done"):
        monkeypatch.setattr(cli_ui, noop, lambda *a, **k: None)

    ok = main._first_run_setup()
    assert ok is True

    assert any("Zero address cannot stake" in e for e in errors), errors

    # The valid address (lowercased) is what got persisted.
    env_file = main._wizard_env_file()
    from dotenv import get_key
    assert (get_key(env_file, "SR_STAKING_ADDRESS") or "").lower() == VALID.lower()


def test_cli_staking_flag_skips_prompt(monkeypatch):
    """BUG-02: `--staking-address` must be honored by the wizard, not ignored.
    Drive the real _first_run_setup with args.staking_address set and confirm
    the staking prompt is never shown and the flag value is persisted."""
    import argparse
    import app.cli_ui as cli_ui
    import app.main as main

    prompted: list[str] = []

    def fake_input(prompt, default="", password=False):
        prompted.append(prompt)
        return ""  # if the staking prompt is reached, blank would error/re-loop

    monkeypatch.setattr(cli_ui, "wizard_input", fake_input)
    monkeypatch.setattr(cli_ui, "wizard_select", lambda *a, **k: 0)
    monkeypatch.setattr(cli_ui, "wizard_confirm", lambda *a, **k: False)
    monkeypatch.setattr(cli_ui, "wizard_error", lambda msg: None)
    for noop in ("wizard_banner", "wizard_step", "wizard_info",
                 "wizard_success", "wizard_done"):
        monkeypatch.setattr(cli_ui, noop, lambda *a, **k: None)

    args = argparse.Namespace(staking_address=VALID, collection_address=None)
    ok = main._first_run_setup(args)
    assert ok is True
    # The flag was honored: the wizard never prompted for the staking address.
    assert "Staking wallet address" not in prompted, prompted

    env_file = main._wizard_env_file()
    from dotenv import get_key
    assert (get_key(env_file, "SR_STAKING_ADDRESS") or "").lower() == VALID.lower()
