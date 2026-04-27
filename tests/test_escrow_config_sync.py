"""Tests for ``app.escrow_config_sync.sync_escrow_config_from_coord``.

Track P2 of the v1.5 stabilization plan — trust-on-first-use sync of
escrow config from the coordination API. See
``internal-docs/v1.5-provider-plan.md`` Section 6 A1 for the operator
decision driving this design (no periodic re-sync, no CLI flag, gateway
handles drift via rejection messages).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from app.escrow_config_sync import sync_escrow_config_from_coord
from app.settings_v2 import (
    CoordinationSection,
    EscrowSection,
    Settings,
)

COORD_URL = "https://coord.example.test"
CONFIG_URL = f"{COORD_URL}/config"

GATEWAY_PAYER = "0x1111111111111111111111111111111111111111"
LEG2_RATE_WEI = 500_000_000_000_000_000_000  # 500 SPACE / GB in wei


def _make_settings(
    *,
    leg2_rate_per_gb: str | None = None,
    synced_from_coord_at: str | None = None,
    gateway_payer_address: str | None = None,
    coord_url: str = COORD_URL,
) -> Settings:
    """Helper: build a Settings with build_variant='test' so http:// URLs are tolerated."""
    return Settings(
        build_variant="test",
        coordination=CoordinationSection(url=coord_url),
        escrow=EscrowSection(
            leg2_rate_per_gb=leg2_rate_per_gb,
            synced_from_coord_at=synced_from_coord_at,
            gateway_payer_address=gateway_payer_address,
        ),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_first_run_populates_rate():
    """Empty escrow → coord /config response → rate + payer + timestamp populated."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(
            200,
            json={
                "minimumStakingAmount": 1,
                "gatewayPayerAddress": GATEWAY_PAYER,
                "gatewayLeg1RatePerGb": LEG2_RATE_WEI * 2,
                "gatewayLeg2RatePerGb": LEG2_RATE_WEI,
            },
        )
    )

    settings = _make_settings()
    out = sync_escrow_config_from_coord(settings)

    assert out is settings  # mutates in place
    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
    assert out.escrow.gateway_payer_address == GATEWAY_PAYER
    assert out.escrow.synced_from_coord_at is not None
    # ISO8601 with timezone — datetime.fromisoformat tolerates any valid form.
    from datetime import datetime
    parsed = datetime.fromisoformat(out.escrow.synced_from_coord_at)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Skip when already synced
# ---------------------------------------------------------------------------


def test_sync_skipped_when_already_synced():
    """settings.json carries a sync stamp + rate → no HTTP call; settings unchanged."""
    settings = _make_settings(
        leg2_rate_per_gb=str(LEG2_RATE_WEI),
        synced_from_coord_at="2026-04-20T12:00:00+00:00",
        gateway_payer_address=GATEWAY_PAYER,
    )

    # If the function tried to make an HTTP call, this would blow up.
    with patch("app.escrow_config_sync.httpx.get") as mock_get:
        out = sync_escrow_config_from_coord(settings)
        mock_get.assert_not_called()

    assert out is settings
    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
    assert out.escrow.synced_from_coord_at == "2026-04-20T12:00:00+00:00"
    assert out.escrow.gateway_payer_address == GATEWAY_PAYER


@respx.mock
def test_sync_overrides_unstamped_rate():
    """Stale rate without sync stamp gets overridden by coord.

    PR #94 changed the heuristic: previously a rate set without a sync
    timestamp was treated as "operator-pinned" and skipped. In practice
    that branch trapped the test.97 backfill (PR #93) which left the
    rate at the bootstrap value (1e15) and the timestamp null — every
    receipt then got rejected as SIGN_REJECTED_UNKNOWN_REQUEST.

    Coord is now authoritative whenever the timestamp is null. An
    operator who genuinely wants to pin a non-coord rate must also
    set ``synced_from_coord_at`` (e.g. via a fixed timestamp such as
    ``"2099-01-01T00:00:00+00:00"``) so the sync skip-when-both-set
    rule fires.
    """
    respx.get(CONFIG_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "gatewayLeg2RatePerGb": LEG2_RATE_WEI,
                "gatewayPayerAddress": GATEWAY_PAYER,
            },
        )
    )

    settings = _make_settings(
        leg2_rate_per_gb="1000000000000000",  # bootstrap stale value
        synced_from_coord_at=None,
    )
    out = sync_escrow_config_from_coord(settings)

    # Coord rate clobbered the bootstrap value.
    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
    # Timestamp now stamped — so future syncs short-circuit.
    assert out.escrow.synced_from_coord_at is not None


# ---------------------------------------------------------------------------
# HTTP failure paths — must never raise, just WARN + return unchanged
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_handles_coord_timeout(caplog):
    """Coord times out → settings unchanged, WARN logged, no exception."""
    respx.get(CONFIG_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb is None
    assert out.escrow.synced_from_coord_at is None
    assert any("timed out" in r.message or "unreachable" in r.message
               for r in caplog.records)


@respx.mock
def test_sync_handles_coord_5xx(caplog):
    """Coord returns 503 → settings unchanged, WARN logged."""
    respx.get(CONFIG_URL).mock(return_value=Response(503, text="upstream down"))

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb is None
    assert out.escrow.synced_from_coord_at is None
    assert any("503" in r.message for r in caplog.records)


@respx.mock
def test_sync_handles_coord_connection_error(caplog):
    """Connection refused / DNS failure → WARN, settings unchanged."""
    respx.get(CONFIG_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb is None
    assert out.escrow.synced_from_coord_at is None


@respx.mock
def test_sync_handles_malformed_json(caplog):
    """Coord returns non-JSON → WARN, settings unchanged."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(200, text="<html>oops</html>",
                              headers={"content-type": "text/html"})
    )

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb is None
    assert out.escrow.synced_from_coord_at is None


# ---------------------------------------------------------------------------
# Defensive paths — coord returns 0 / empty / partial data
# ---------------------------------------------------------------------------


@respx.mock
def test_sync_zero_rate_treated_as_unset(caplog):
    """gatewayLeg2RatePerGb=0 → don't persist, WARN."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(
            200,
            json={
                "gatewayPayerAddress": GATEWAY_PAYER,
                "gatewayLeg2RatePerGb": 0,
            },
        )
    )

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    # Rate stayed unset.
    assert out.escrow.leg2_rate_per_gb is None
    # Payer DID get applied (it was non-empty in the response).
    assert out.escrow.gateway_payer_address == GATEWAY_PAYER
    # Timestamp DID stamp because at least one field (payer) was applied.
    assert out.escrow.synced_from_coord_at is not None
    assert any("not-yet-configured" in r.message or "0" in r.message
               for r in caplog.records)


@respx.mock
def test_sync_empty_payer_treated_as_unset(caplog):
    """Empty gatewayPayerAddress → don't persist, WARN, but rate still applies."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(
            200,
            json={
                "gatewayPayerAddress": "",
                "gatewayLeg2RatePerGb": LEG2_RATE_WEI,
            },
        )
    )

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.gateway_payer_address is None
    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
    assert out.escrow.synced_from_coord_at is not None
    assert any("gatewayPayerAddress" in r.message for r in caplog.records)


@respx.mock
def test_sync_partial_response_preserves_existing():
    """Coord response missing gatewayPayerAddress; payer already set → leave payer alone, set rate."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(
            200,
            json={
                # gatewayPayerAddress field omitted
                "gatewayLeg2RatePerGb": LEG2_RATE_WEI,
            },
        )
    )

    pre_existing_payer = "0x9999999999999999999999999999999999999999"
    settings = _make_settings(gateway_payer_address=pre_existing_payer)
    out = sync_escrow_config_from_coord(settings)

    # Payer left untouched (it was already set, AND coord didn't send one).
    assert out.escrow.gateway_payer_address == pre_existing_payer
    # Rate populated from coord.
    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
    assert out.escrow.synced_from_coord_at is not None


@respx.mock
def test_sync_no_op_when_response_has_nothing_to_apply(caplog):
    """Coord returns 0 rate AND empty payer → no fields applied → no timestamp stamped."""
    respx.get(CONFIG_URL).mock(
        return_value=Response(
            200,
            json={
                "gatewayPayerAddress": "",
                "gatewayLeg2RatePerGb": 0,
            },
        )
    )

    settings = _make_settings()
    with caplog.at_level("WARNING", logger="app.escrow_config_sync"):
        out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb is None
    assert out.escrow.gateway_payer_address is None
    # Critical: no false-positive sync stamp. Next launch retries.
    assert out.escrow.synced_from_coord_at is None


@respx.mock
def test_sync_url_with_trailing_slash():
    """coord URL with trailing slash should still build a clean /config URL."""
    respx.get("https://coord-trailing.example.test/config").mock(
        return_value=Response(
            200,
            json={
                "gatewayPayerAddress": GATEWAY_PAYER,
                "gatewayLeg2RatePerGb": LEG2_RATE_WEI,
            },
        )
    )

    settings = _make_settings(coord_url="https://coord-trailing.example.test/")
    out = sync_escrow_config_from_coord(settings)

    assert out.escrow.leg2_rate_per_gb == str(LEG2_RATE_WEI)
