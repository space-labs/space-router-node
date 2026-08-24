"""Item 2 as QA ran it: is the observed behaviour the self-heal, by design?

QA reported item 2 ("blank staking address rejected") as FAIL on Windows CLI:
clearing only settings.json recovered the old address from the legacy
spacerouter.env instead of refusing to start.

That IS the headline fix (recover a staking address lost on a v1.4 -> v1.5
upgrade). This test pins the precedence so the intent is unambiguous, and
documents the real gap: a deliberate user clear is INDISTINGUISHABLE from an
upgrade that dropped the value.
"""
from __future__ import annotations

import app.settings_loader as sl


def _blank_settings():
    class _W:
        staking_address = ""
    class _S:
        wallet = _W()
        build_variant = "production"
    return _S()


def test_legacy_value_is_recovered_when_settings_json_is_blank(monkeypatch, tmp_path):
    """QA's observed Windows CLI behaviour — and it is intentional."""
    legacy = tmp_path / "spacerouter.env"
    legacy.write_text("SR_STAKING_ADDRESS=0x" + "ab" * 20 + "\n")

    monkeypatch.setattr(
        sl, "_legacy_env_candidates", lambda d, v: [(legacy, False)])

    s = _blank_settings()
    recovered = sl._recover_staking_address_in_place(s, tmp_path)

    assert recovered is True, "the upgrade self-heal did not fire"
    assert s.wallet.staking_address == "0x" + "ab" * 20, (
        "recovery did not restore the legacy staking address"
    )


def test_no_legacy_value_means_no_recovery(monkeypatch, tmp_path):
    """With the legacy file gone it must refuse, which QA also confirmed."""
    monkeypatch.setattr(
        sl, "_legacy_env_candidates",
        lambda d, v: [(tmp_path / "absent.env", False)])

    s = _blank_settings()
    assert sl._recover_staking_address_in_place(s, tmp_path) is False
    assert s.wallet.staking_address == "", (
        "a staking address appeared from nowhere — the identity fallback is back"
    )


def test_a_populated_settings_value_is_never_overwritten(monkeypatch, tmp_path):
    """Recovery must only fill a blank, never clobber an explicit choice."""
    legacy = tmp_path / "spacerouter.env"
    legacy.write_text("SR_STAKING_ADDRESS=0x" + "cd" * 20 + "\n")
    monkeypatch.setattr(
        sl, "_legacy_env_candidates", lambda d, v: [(legacy, False)])

    s = _blank_settings()
    s.wallet.staking_address = "0x" + "11" * 20
    assert sl._recover_staking_address_in_place(s, tmp_path) is False
    assert s.wallet.staking_address == "0x" + "11" * 20


def test_deliberate_clear_is_indistinguishable_from_upgrade_loss(monkeypatch, tmp_path):
    """The real gap behind QA's item-2 report.

    Recovery keys ONLY off "settings.json has no staking address". There is no
    sentinel recording that a user cleared it on purpose, so both cases take
    the same branch. If we want a deliberate clear to stick, that sentinel is
    the fix -- not removing the recovery.
    """
    import inspect
    src = inspect.getsource(sl._recover_staking_address_in_place)
    assert "if (s.wallet.staking_address or \"\").strip():" in src, (
        "recovery no longer keys purely off a blank value — update this test"
    )
    for sentinel in ("cleared_by_user", "staking_address_cleared", "explicitly_cleared"):
        assert sentinel not in src, (
            f"a {sentinel!r} sentinel now exists — a deliberate clear can be "
            f"distinguished, so QA's item 2 expectation is now satisfiable"
        )
