"""Config precedence: explicit CLI flag > env var > settings.json > default.

QA reported ``--staking-address`` as a partial pass on Windows CLI for
v1.5.2-test.136: hand-edits to settings.json persisted, but the flag was
silently ignored. It reproduces on macOS too, on the real frozen binary.

Root cause: ``load_provider_settings`` returns as soon as settings.json
exists, so the ``SR_*`` vars ``_apply_cli_args`` exports were only ever read
on the cold-start branch. ``--public-url`` / ``--public-port`` / ``--no-upnp``
appeared to work only because they take a second route — they write straight
into settings.json via ``_persist_network_mode_to_settings``.

The overlay is per-run: settings.json must stay byte-identical so a one-off
``--staking-address`` cannot re-key an operator's node.
"""
from __future__ import annotations

import json
import os

import pytest

from app.config import load_settings
from app.settings_loader import apply_env_overrides, load_provider_settings
from app.settings_v2 import Settings

_ON_DISK_STAKING = "0x" + "aa" * 20
_FLAG_STAKING = "0x" + "bb" * 20
_ON_DISK_COLLECTION = "0x" + "11" * 20
_FLAG_COLLECTION = "0x" + "22" * 20


@pytest.fixture
def configured_home(tmp_path, monkeypatch):
    """A populated settings.json in an isolated HOME, as an operator has.

    Every ``SR_*`` var is cleared first: these tests are about which tier of
    the precedence chain wins, so the environment tier has to start empty.
    """
    for key in [k for k in os.environ if k.startswith("SR_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = tmp_path / ".spacerouter"
    cfg.mkdir()
    Settings(
        build_variant="test",
        node={"port": 9090, "label": "on-disk", "log_level": "INFO"},
        wallet={
            "staking_address": _ON_DISK_STAKING,
            "collection_address": _ON_DISK_COLLECTION,
        },
        coordination={"url": "https://coord.on-disk.test"},
        escrow={
            "enabled": True,
            "contract_address": "0x" + "33" * 20,
            "chain_rpc": "https://rpc.on-disk.test",
            "chain_id": 102031,
        },
    ).save(cfg / "settings.json")
    return cfg


def _on_disk(cfg) -> dict:
    return json.loads((cfg / "settings.json").read_text())


def test_staking_address_flag_beats_a_populated_settings_json(
    configured_home, monkeypatch
):
    """The QA report, at the layer the flag actually lands on."""
    monkeypatch.setenv("SR_STAKING_ADDRESS", _FLAG_STAKING)

    assert load_settings().STAKING_ADDRESS == _FLAG_STAKING, (
        "--staking-address was silently ignored again"
    )


def test_collection_address_flag_beats_a_populated_settings_json(
    configured_home, monkeypatch
):
    monkeypatch.setenv("SR_COLLECTION_ADDRESS", _FLAG_COLLECTION)

    assert load_settings().COLLECTION_ADDRESS == _FLAG_COLLECTION


def test_wallet_overrides_do_not_touch_settings_json(configured_home, monkeypatch):
    """A one-off debug run must not permanently re-key the operator."""
    before = _on_disk(configured_home)
    monkeypatch.setenv("SR_STAKING_ADDRESS", _FLAG_STAKING)
    monkeypatch.setenv("SR_COLLECTION_ADDRESS", _FLAG_COLLECTION)

    load_settings()

    after = _on_disk(configured_home)
    assert after["wallet"]["staking_address"] == _ON_DISK_STAKING
    assert after["wallet"]["collection_address"] == _ON_DISK_COLLECTION
    assert after == before, "the per-run overlay leaked into settings.json"


def test_public_ip_and_node_port_env_vars_are_honoured(configured_home, monkeypatch):
    """The v1.4 -> v1.5 upgrade finding: both were ignored at 192.168.x:9090."""
    monkeypatch.setenv("SR_PUBLIC_IP", "127.0.0.1")
    monkeypatch.setenv("SR_NODE_PORT", "19191")

    s = load_settings()
    assert s.PUBLIC_IP == "127.0.0.1"
    assert s.NODE_PORT == 19191


def test_label_log_level_and_coordination_url_env_vars_are_honoured(
    configured_home, monkeypatch
):
    monkeypatch.setenv("SR_NODE_LABEL", "from-env")
    monkeypatch.setenv("SR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SR_COORDINATION_API_URL", "https://coord.from-env.test")

    s = load_settings()
    assert s.NODE_LABEL == "from-env"
    assert s.LOG_LEVEL == "DEBUG"
    assert s.COORDINATION_API_URL == "https://coord.from-env.test"


def test_upnp_disable_env_var_is_honoured(configured_home, monkeypatch):
    monkeypatch.setenv("SR_UPNP_ENABLED", "false")

    assert load_settings().UPNP_ENABLED is False


def test_settings_json_still_beats_the_built_in_defaults(configured_home):
    """Regression guard: with no flag and no env, the file must still win."""
    s = load_settings()

    assert s.STAKING_ADDRESS == _ON_DISK_STAKING
    assert s.COLLECTION_ADDRESS == _ON_DISK_COLLECTION
    assert s.NODE_PORT == 9090
    assert s.NODE_LABEL == "on-disk"
    assert s.COORDINATION_API_URL == "https://coord.on-disk.test"


def test_a_bare_40_hex_flag_value_is_normalised(configured_home, monkeypatch):
    """BUG-06 accepts a bare 40-hex address; the overlay must too."""
    monkeypatch.setenv("SR_STAKING_ADDRESS", "bb" * 20)

    assert load_settings().STAKING_ADDRESS == _FLAG_STAKING


def test_an_invalid_override_is_dropped_not_fatal(configured_home, monkeypatch):
    """A stray bad env var must not take out a working install."""
    monkeypatch.setenv("SR_STAKING_ADDRESS", "not-an-address")

    assert load_settings().STAKING_ADDRESS == _ON_DISK_STAKING


def test_https_enforcement_survives_the_overlay(tmp_path, monkeypatch):
    """A production build must not be talked onto plaintext http by an env var."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = tmp_path / ".spacerouter"
    cfg.mkdir()
    Settings(
        build_variant="production",
        coordination={"url": "https://coord.on-disk.test"},
    ).save(cfg / "settings.json")
    monkeypatch.setenv("SR_COORDINATION_API_URL", "http://mitm.example.test")

    assert load_settings().COORDINATION_API_URL == "https://coord.on-disk.test"


def test_build_variant_is_never_taken_from_the_environment(
    configured_home, monkeypatch
):
    """PR #68: SR_BUILD_VARIANT in env rotated the node id on every restart."""
    monkeypatch.setenv("SR_BUILD_VARIANT", "production")

    assert apply_env_overrides(load_provider_settings()).build_variant == "test"


def test_passphrase_presence_does_not_flip_the_derived_flag(
    configured_home, monkeypatch
):
    """``identity_passphrase_set`` is reconciled against the keystore, not env."""
    monkeypatch.setenv("SR_IDENTITY_PASSPHRASE", "hunter2")

    merged = apply_env_overrides(load_provider_settings())
    assert merged.wallet.identity_passphrase_set is False


def test_overlay_returns_the_same_object_when_nothing_changes(configured_home):
    s = load_provider_settings()
    assert apply_env_overrides(s) is s
