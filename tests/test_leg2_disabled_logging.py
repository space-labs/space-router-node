"""Regression: surface clear log when Leg 2 is gated off at startup.

The test.95 silent failure was a user-visible mystery because main.py's
``if PAYMENT_ENABLED and NODE_RATE_PER_GB > 0`` had no else branch — the
provider just silently never started the submitter. The Earnings card
then spammed `no such table: signed_receipts` (PR 2) and the user had
no obvious diagnostic path to root cause.

This test pins the explicit log behaviour: WARN on test variant when
escrow is off (almost always misconfiguration), INFO on prod (operator
choice).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.main import _log_leg2_gated_off


def _settings(payment_enabled: bool, rate: int = 0):
    return SimpleNamespace(
        PAYMENT_ENABLED=payment_enabled,
        NODE_RATE_PER_GB=rate,
    )


def test_warns_on_test_variant_when_payment_disabled(monkeypatch, caplog):
    import app.variant as variant_mod
    monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_leg2_gated_off(_settings(payment_enabled=False))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("Leg 2 disabled" in m and "escrow.enabled=false" in m for m in msgs)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_info_on_prod_variant_when_payment_disabled(monkeypatch, caplog):
    import app.variant as variant_mod
    monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "production")

    with caplog.at_level(logging.INFO, logger="app.main"):
        _log_leg2_gated_off(_settings(payment_enabled=False))

    levels = {r.levelno for r in caplog.records if "Leg 2 disabled" in r.getMessage()}
    assert logging.INFO in levels
    assert logging.WARNING not in levels


def test_explains_zero_rate_when_payment_enabled_but_rate_zero(monkeypatch, caplog):
    import app.variant as variant_mod
    monkeypatch.setattr(variant_mod, "BUILD_VARIANT", "test")

    with caplog.at_level(logging.WARNING, logger="app.main"):
        _log_leg2_gated_off(_settings(payment_enabled=True, rate=0))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("leg2_rate_per_gb=0" in m for m in msgs)
    assert any("must be > 0" in m for m in msgs)
