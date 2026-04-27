"""In-place upgrade migration: backfill test-variant escrow defaults.

Regression for the test.95 -> test.101 in-place upgrade gap. PR 1 fixed
Fresh Restart, but users whose settings.json had already been wiped to
``escrow.enabled=false`` (with all escrow fields null) by the .95 bug
were still stuck — load_provider_settings just returned the existing
file as-is, and the receipt submitter never started.

This module pins the lazy backfill that fires once on load when:

* build_variant == "test"
* escrow.contract_address is None  (clear sign the section is unset)

The discriminator is contract_address rather than enabled — operators
who deliberately set ``enabled=false`` on a fully-configured escrow
get to keep that choice.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.settings_loader import (
    _backfill_test_escrow_in_place,
    load_provider_settings,
    settings_path,
)
from app.settings_v2 import Settings


def _persist(tmp_path: Path, **escrow_overrides) -> Path:
    s = Settings(build_variant="test")
    for k, v in escrow_overrides.items():
        setattr(s.escrow, k, v)
    sp = settings_path(tmp_path)
    s.save(sp)
    return sp


def test_backfills_unconfigured_test_variant(tmp_path):
    """The exact test.95 in-place upgrade scenario: enabled=false, all
    escrow fields null. After load, enabled flips to true and the
    testnet contract addresses are populated.
    """
    _persist(tmp_path, enabled=False)

    s = load_provider_settings(tmp_path)

    assert s.escrow.enabled is True
    assert s.escrow.contract_address.lower().startswith("0xc5740e4e")
    assert "testnet.creditcoin.network" in s.escrow.chain_rpc
    assert s.escrow.chain_id == 102031
    assert s.escrow.leg2_rate_per_gb == "1000000000000000"


def test_backfill_persists_to_disk(tmp_path):
    """The backfilled settings must be written back so subsequent loads
    see the same state — and so the user can inspect ~/.spacerouter/
    and confirm the migration ran.
    """
    sp = _persist(tmp_path, enabled=False)

    load_provider_settings(tmp_path)

    data = json.loads(sp.read_text())
    assert data["escrow"]["enabled"] is True
    assert data["escrow"]["contract_address"].lower().startswith("0xc5740e4e")


def test_does_not_touch_operator_configured_escrow(tmp_path):
    """If the operator set their own escrow contract address (test or
    custom deployment), we respect it — no clobbering. The fact that
    enabled=false stays false is a deliberate operator opt-out.
    """
    custom_addr = "0x" + "f" * 40
    _persist(
        tmp_path,
        enabled=False,
        contract_address=custom_addr,
        chain_rpc="https://custom.example/rpc",
        chain_id=999,
    )

    s = load_provider_settings(tmp_path)

    assert s.escrow.enabled is False
    assert s.escrow.contract_address == custom_addr
    assert s.escrow.chain_rpc == "https://custom.example/rpc"
    assert s.escrow.chain_id == 999


def test_does_not_run_on_prod_variant(tmp_path):
    """Prod doesn't auto-flip escrow on. Operators configure it
    explicitly until mainnet escrow is rolled out."""
    s = Settings(build_variant="production")
    s.save(settings_path(tmp_path))

    s = load_provider_settings(tmp_path)

    assert s.escrow.enabled is False
    assert s.escrow.contract_address is None


def test_helper_idempotent_on_already_backfilled(tmp_path):
    """A second load should be a no-op (returns False)."""
    s = Settings(build_variant="test")
    s.escrow.enabled = False
    assert _backfill_test_escrow_in_place(s) is True

    # Second pass: contract_address is now set, so we skip.
    assert _backfill_test_escrow_in_place(s) is False


def test_helper_skips_when_leg2_rate_already_set(tmp_path):
    """If only the rate is set (e.g. operator partially-configured the
    section without a contract), we still backfill the missing
    contract address but preserve their rate."""
    s = Settings(build_variant="test")
    s.escrow.leg2_rate_per_gb = "999"
    assert _backfill_test_escrow_in_place(s) is True
    assert s.escrow.leg2_rate_per_gb == "999"
    assert s.escrow.contract_address is not None
