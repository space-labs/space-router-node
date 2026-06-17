"""GUI bridge methods for the mandatory-staking flow.

Covers the new Settings-panel remediation path:

- ``validate_staking_address("")`` now returns ``ok=False`` (was
  ``ok=True, status="unset"`` when empty meant "fall back to identity").
- ``get_staking_address`` reads the current configured value.
- ``save_staking_address`` validates format and persists via
  ``ConfigStore.save_wallets`` without nuking ``collection_address``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gui.api import Api


GOOD_ADDR = "0x" + "ab" * 20
ZERO_ADDR = "0x" + "00" * 20


def _make_api(config_get_map=None, save_wallets_return=None):
    """Build an Api wired to a config mock with the given .get() map."""
    cfg = MagicMock()
    if config_get_map is not None:
        cfg.get.side_effect = lambda k, default=None: config_get_map.get(k, default)
    if save_wallets_return is not None:
        cfg.save_wallets.return_value = save_wallets_return
    node = MagicMock()
    return Api(config=cfg, node_manager=node), cfg


# ── validate_staking_address ───────────────────────────────────────


def test_validate_empty_now_rejects():
    """Empty input used to fall back to identity address; that path is
    gone, so the bridge must hard-reject empty so the GUI Settings save
    flow can't write a blank value to disk."""
    api, _ = _make_api()
    result = api.validate_staking_address("")
    assert result["ok"] is False
    assert result["status"] == "required"


def test_validate_whitespace_treated_as_empty():
    api, _ = _make_api()
    result = api.validate_staking_address("   ")
    assert result["ok"] is False
    assert result["status"] == "required"


def test_validate_garbage_format_rejected():
    api, _ = _make_api()
    result = api.validate_staking_address("not-an-address")
    assert result["ok"] is False
    assert result["status"] == "invalid"


def test_validate_zero_address_rejected():
    api, _ = _make_api()
    result = api.validate_staking_address(ZERO_ADDR)
    assert result["ok"] is False
    assert result["status"] == "invalid"


# ── get_staking_address ────────────────────────────────────────────


def test_get_staking_address_returns_configured_value():
    api, _ = _make_api(config_get_map={"SR_STAKING_ADDRESS": GOOD_ADDR})
    assert api.get_staking_address() == GOOD_ADDR


def test_get_staking_address_returns_empty_when_unset():
    api, _ = _make_api(config_get_map={})
    assert api.get_staking_address() == ""


# ── save_staking_address ───────────────────────────────────────────


def test_save_rejects_empty():
    """The bridge must refuse to write blank — otherwise the daemon
    will hit MISSING_WALLET on next start and the operator is stuck."""
    api, cfg = _make_api()
    result = api.save_staking_address("")
    assert result["ok"] is False
    cfg.save_wallets.assert_not_called()


def test_save_rejects_invalid_format():
    api, cfg = _make_api()
    result = api.save_staking_address("0xdeadbeef")
    assert result["ok"] is False
    cfg.save_wallets.assert_not_called()


def test_save_persists_and_preserves_collection(monkeypatch):
    """The save bridge must thread the existing collection_address into
    ConfigStore.save_wallets — otherwise a user who customised the
    collection wallet at onboarding loses it when they later edit the
    staking address."""

    # Bypass the on-chain validation lookup (covered in its own tests).
    def _ok(self, address):
        return {"ok": True, "status": "earning", "message": ""}

    monkeypatch.setattr(Api, "validate_staking_address", _ok)

    custom_collection = "0x" + "cd" * 20
    api, cfg = _make_api(
        config_get_map={"SR_COLLECTION_ADDRESS": custom_collection},
        save_wallets_return=(GOOD_ADDR, custom_collection),
    )

    result = api.save_staking_address(GOOD_ADDR)

    assert result["ok"] is True
    assert result["restart_required"] is True
    assert result["staking_address"] == GOOD_ADDR
    cfg.save_wallets.assert_called_once_with(GOOD_ADDR, custom_collection)


def test_save_handles_missing_collection_gracefully(monkeypatch):
    """When no collection was ever set, ``cfg.get("SR_COLLECTION_ADDRESS")``
    returns None — the bridge must coerce to empty string before calling
    save_wallets (which expects a str)."""

    def _ok(self, address):
        return {"ok": True, "status": "earning", "message": ""}

    monkeypatch.setattr(Api, "validate_staking_address", _ok)

    api, cfg = _make_api(
        config_get_map={},  # nothing configured
        save_wallets_return=(GOOD_ADDR, GOOD_ADDR),
    )

    result = api.save_staking_address(GOOD_ADDR)
    assert result["ok"] is True
    # Empty string, not None — confirms the coalesce in save_staking_address.
    cfg.save_wallets.assert_called_once_with(GOOD_ADDR, "")
