"""Workstream A: staking-address recovery + production coord-url healing.

These exercise the real ``load_provider_settings`` resolution chain against
real temp directories and real env files — nothing is mocked. They pin the
self-heal that lets operators stranded by the v1.4→v1.5 macOS migration
skip-trap recover without re-typing their wallet.
"""

from __future__ import annotations

import pytest

from app.settings_loader import (
    _heal_test_coord_url_in_place,
    _legacy_env_candidates,
    _read_staking_address_from_env,
    _recover_staking_address_in_place,
    load_provider_settings,
)
from app.settings_v2 import Settings

# Neutral EIP-55 test vectors (not tied to any real operator). The validator
# lowercases, so the recovered value is the lowercase form.
ADDR_A = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
ADDR_A_LC = "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"
ADDR_B = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
ADDR_B_LC = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

OLD_TEST_URL = "https://spacerouter-coordination-api-test.fly.dev"
PROD_URL = "https://coordination.spacerouter.org"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path_factory):
    """Isolate the test from real machine state.

    - Strip SR_* vars so the env-var seed path (Step 3) never fires.
    - Repoint HOME at a fresh empty dir so the legacy App Support / XDG
      candidates can't pick up the developer's real spacerouter.env.
    """
    import os
    for key in [k for k in os.environ if k.startswith("SR_")]:
        monkeypatch.delenv(key, raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    # BUG-03 added a cwd-relative .env scan; chdir to a clean dir so the
    # recovery never picks up a stray .env from the repo/working directory.
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


def _write_settings(directory, *, staking=None, coord_url=None, variant="production"):
    """Write a real settings.json via the schema (not hand-rolled JSON)."""
    s = Settings(build_variant=variant)
    s.wallet.staking_address = staking
    if coord_url is not None:
        s.coordination.url = coord_url
    # Keep the passphrase reconcile off the real HOME keystore.
    s.wallet.settlement_key_path = str(directory / "certs" / "node-identity.key")
    s.save(directory / "settings.json")
    return s


# ── A1: recovery via the full load_provider_settings chain ──────────────────

def test_recovers_blank_staking_from_migrated_bak(tmp_path):
    """settings.json present but blank staking + a renamed legacy env in the
    same dir → the address is recovered and persisted back to settings.json."""
    _write_settings(tmp_path, staking=None)
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        f"# legacy config\nSR_STAKING_ADDRESS={ADDR_A}\n"
    )

    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.wallet.staking_address == ADDR_A_LC

    # persisted, so the next launch is clean without re-scanning
    reloaded = Settings.load(tmp_path / "settings.json")
    assert reloaded.wallet.staking_address == ADDR_A_LC


def test_recovers_from_legacy_wallet_address_key(tmp_path):
    """v0.1.2 single-wallet env (SR_WALLET_ADDRESS only) still recovers."""
    _write_settings(tmp_path, staking=None)
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        f'SR_WALLET_ADDRESS="{ADDR_B}"\n'
    )
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.wallet.staking_address == ADDR_B_LC


def test_does_not_overwrite_existing_staking(tmp_path):
    """A populated staking address is never touched, even if a legacy env
    holds a different value."""
    _write_settings(tmp_path, staking=ADDR_A)
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        f"SR_STAKING_ADDRESS={ADDR_B}\n"
    )
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.wallet.staking_address == ADDR_A_LC


def test_invalid_legacy_address_is_ignored(tmp_path):
    """A malformed address in the legacy env leaves staking blank (no crash)."""
    _write_settings(tmp_path, staking=None)
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        "SR_STAKING_ADDRESS=not-an-address\n"
    )
    loaded = load_provider_settings(directory=tmp_path)
    assert not loaded.wallet.staking_address


def test_cold_start_recovers_from_bak(tmp_path):
    """No settings.json at all (Step 4 cold-start) but a stray renamed legacy
    env in the dir → recovery still fires before the defaults are persisted.
    This is the lock-file-only skip-trap where the full migration was missed."""
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        f"SR_STAKING_ADDRESS={ADDR_A}\n"
    )
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.wallet.staking_address == ADDR_A_LC
    assert (tmp_path / "settings.json").exists()


# ── A3: production coord-url healing ────────────────────────────────────────

def test_heals_persisted_test_url_on_production(tmp_path):
    """Production build with the v1.5.0 test url persisted → rewritten to prod."""
    _write_settings(tmp_path, staking=ADDR_A, coord_url=OLD_TEST_URL, variant="production")
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.coordination.url == PROD_URL


def test_does_not_heal_on_test_build(tmp_path):
    """Test builds legitimately use the test url — leave it alone."""
    _write_settings(tmp_path, staking=ADDR_A, coord_url=OLD_TEST_URL, variant="test")
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.coordination.url == OLD_TEST_URL


def test_does_not_heal_custom_url_on_production(tmp_path):
    """An operator-set custom url never matches the exact known-bad default."""
    custom = "https://my-private-coord.example.com"
    _write_settings(tmp_path, staking=ADDR_A, coord_url=custom, variant="production")
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.coordination.url == custom


# ── unit-level coverage of the helpers ──────────────────────────────────────

def test_read_env_tolerates_quotes_export_and_comments(tmp_path):
    env = tmp_path / "spacerouter.env"
    env.write_text(
        "# a comment\n"
        "\n"
        "export SR_OTHER=foo\n"
        f"  SR_STAKING_ADDRESS = '{ADDR_A}' \n"
    )
    assert _read_staking_address_from_env(env) == ADDR_A_LC


def test_read_env_missing_file_returns_none(tmp_path):
    assert _read_staking_address_from_env(tmp_path / "nope.env") is None


def test_recover_in_place_noop_when_no_sources(tmp_path):
    s = Settings(build_variant="production")
    s.wallet.staking_address = None
    assert _recover_staking_address_in_place(s, tmp_path) is False
    assert not s.wallet.staking_address


def test_heal_in_place_returns_flag(tmp_path):
    s = Settings(build_variant="production")
    s.coordination.url = OLD_TEST_URL
    assert _heal_test_coord_url_in_place(s) is True
    assert s.coordination.url == PROD_URL
    # idempotent: second pass is a no-op
    assert _heal_test_coord_url_in_place(s) is False


def test_legacy_candidates_scan_canonical_dir_first(tmp_path):
    cands = _legacy_env_candidates(tmp_path, "production")
    paths = [p for p, _ in cands]
    assert paths[0] == tmp_path / "spacerouter.env"
    assert tmp_path / "spacerouter.env.migrated.bak" in paths
    # canonical dir entries are not marker-gated
    assert cands[0][1] is False


def test_legacy_candidates_cwd_and_home_env_are_marker_gated(tmp_path):
    cands = _legacy_env_candidates(tmp_path, "production")
    gated = {p for p, req in cands if req}
    # cwd .env and ~/.env are scanned but require a SpaceRouter marker
    from pathlib import Path
    assert (Path.cwd() / ".env") in gated
    assert (Path.home() / ".env") in gated


def test_legacy_candidates_linux_includes_both_xdg_variants(tmp_path, monkeypatch):
    import app.settings_loader as sl
    monkeypatch.setattr(sl.sys, "platform", "linux")
    paths = [p for p, _ in sl._legacy_env_candidates(tmp_path, "test")]
    home = __import__("pathlib").Path.home()
    # test build: -test dir comes first (variant-matching), prod dir still present
    assert home / ".config" / "spacerouter-test" / "spacerouter.env" in paths
    assert home / ".config" / "spacerouter" / "spacerouter.env" in paths
    assert paths.index(home / ".config" / "spacerouter-test" / "spacerouter.env") \
        < paths.index(home / ".config" / "spacerouter" / "spacerouter.env")


def test_cwd_env_recovered_only_with_marker(tmp_path, monkeypatch):
    """A cwd .env with a SpaceRouter marker is recovered; one without is not."""
    monkeypatch.chdir(tmp_path)
    # without a marker → ignored (could be an unrelated dev .env)
    (tmp_path / ".env").write_text(f"SR_STAKING_ADDRESS={ADDR_A}\n")
    s = Settings(build_variant="production")
    s.wallet.staking_address = None
    assert _recover_staking_address_in_place(s, tmp_path / "nope") is False
    assert not s.wallet.staking_address
    # with a wizard marker → recovered
    (tmp_path / ".env").write_text(
        f"SR_STAKING_ADDRESS={ADDR_A}\nSR_UPNP_ENABLED=false\n"
    )
    s2 = Settings(build_variant="production")
    s2.wallet.staking_address = None
    assert _recover_staking_address_in_place(s2, tmp_path / "nope") is True
    assert s2.wallet.staking_address == ADDR_A_LC


def test_read_env_require_marker(tmp_path):
    env = tmp_path / "x.env"
    env.write_text(f"SR_STAKING_ADDRESS={ADDR_A}\n")
    # no marker → None when required, but fine when not required
    assert _read_staking_address_from_env(env, require_marker=True) is None
    assert _read_staking_address_from_env(env, require_marker=False) == ADDR_A_LC
    env.write_text(f"SR_STAKING_ADDRESS={ADDR_A}\nSR_REFERRAL_CODE=ALPHA\n")
    assert _read_staking_address_from_env(env, require_marker=True) == ADDR_A_LC


def test_recovery_does_not_reenter_variant_resolver(tmp_path, monkeypatch):
    """Recovery runs inside load_provider_settings; it must not call back
    into app.variant (which resolves by calling load_provider_settings),
    or it recurses and spams migration warnings. Guard against regressions
    by failing if the variant resolver is touched during recovery."""
    import app.variant as variant_mod

    calls = {"n": 0}
    real = variant_mod.get_build_variant

    def _spy():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(variant_mod, "get_build_variant", _spy)

    _write_settings(tmp_path, staking=None)
    (tmp_path / "spacerouter.env.migrated.bak").write_text(
        f"SR_STAKING_ADDRESS={ADDR_A}\n"
    )
    loaded = load_provider_settings(directory=tmp_path)
    assert loaded.wallet.staking_address == ADDR_A_LC
    assert calls["n"] == 0, "recovery re-entered the variant resolver"
