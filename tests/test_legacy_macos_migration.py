"""Tests for the macOS legacy ``Application Support`` -> ``~/.spacerouter`` copy.

The unification of config dirs (v1.5 plan, D1-b) abandoned macOS's
``~/Library/Application Support/SpaceRouter[-Test]/`` location in favour
of ``~/.spacerouter`` everywhere. Pre-existing v1.4 macOS users must
have their identity key, certs, receipts.db etc. carried over on first
launch — that's what :py:mod:`app.legacy_migration` does.

These tests fake ``Path.home()`` so the real user dir is never touched.
``sys.platform`` is monkeypatched to ``"darwin"`` so the same tests run
green on the Linux CI box.
"""

from __future__ import annotations

import json

import pytest

from app import legacy_migration


@pytest.fixture
def fake_macos(tmp_path, monkeypatch):
    """Yield ``(home, legacy_prod, legacy_test, target)`` rooted in tmp_path.

    Pre-creates the legacy parent directory but NOT the SpaceRouter /
    SpaceRouter-Test subfolders — individual tests opt in to whichever
    layout they want.
    """
    home = tmp_path / "home"
    home.mkdir()
    appsupport = home / "Library" / "Application Support"
    appsupport.mkdir(parents=True)

    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.platform", "darwin")

    return {
        "home": home,
        "prod": appsupport / "SpaceRouter",
        "test": appsupport / "SpaceRouter-Test",
        "target": home / ".spacerouter",
    }


def _seed_legacy(dir_: "Path", *, files: dict[str, str]) -> None:  # noqa: F821
    dir_.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = dir_ / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_no_op_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    target = tmp_path / "home" / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert not target.exists()


def test_no_op_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    target = tmp_path / "home" / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False


def test_no_op_when_no_legacy_dir(fake_macos):
    target = fake_macos["target"]
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert not target.exists()


def test_migrates_files_from_legacy_prod(fake_macos):
    legacy = fake_macos["prod"]
    _seed_legacy(
        legacy,
        files={
            "spacerouter.env": "SR_NODE_PORT=9090\n",
            "certs/node.crt": "-----BEGIN CERTIFICATE-----\nfake\n",
            "certs/node-identity.key": "deadbeef",
            "receipts.db": "sqlite-bytes",
        },
    )
    target = fake_macos["target"]

    moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "spacerouter.env").read_text() == "SR_NODE_PORT=9090\n"
    assert (target / "certs" / "node.crt").exists()
    assert (target / "certs" / "node-identity.key").read_text() == "deadbeef"
    assert (target / "receipts.db").read_text() == "sqlite-bytes"

    # Sentinel written and points at the source.
    sentinel = target / ".migrated_from_appsupport"
    assert sentinel.exists()
    assert str(legacy) in sentinel.read_text()

    # Source is preserved — operator cleans up manually.
    assert legacy.exists()
    assert (legacy / "spacerouter.env").exists()


def test_idempotent_via_sentinel(fake_macos):
    legacy = fake_macos["prod"]
    _seed_legacy(legacy, files={"a.txt": "v1"})
    target = fake_macos["target"]

    assert legacy_migration.maybe_migrate_legacy_macos(target) is True

    # Mutate target post-migration; re-running must leave that mutation
    # alone (the sentinel guards against re-copy).
    (target / "a.txt").write_text("v2-edited")

    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    assert (target / "a.txt").read_text() == "v2-edited"


def test_aborts_when_target_already_populated(fake_macos, caplog):
    legacy = fake_macos["prod"]
    _seed_legacy(legacy, files={"file.txt": "from-legacy"})

    target = fake_macos["target"]
    target.mkdir()
    (target / "preexisting.txt").write_text("user-data-here")

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is False
    # Preexisting data untouched.
    assert (target / "preexisting.txt").read_text() == "user-data-here"
    # And no copy happened.
    assert not (target / "file.txt").exists()
    # Operator-friendly warning so they can resolve it manually.
    assert "Skipping auto-migration" in caplog.text


def test_picks_prod_when_variant_is_production(fake_macos, monkeypatch, caplog):
    """Build variant = production → SpaceRouter (non-Test) wins."""
    _seed_legacy(fake_macos["prod"], files={"who.txt": "prod"})
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})
    monkeypatch.setattr("app.variant.BUILD_VARIANT", "production")

    target = fake_macos["target"]

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "who.txt").read_text() == "prod"
    assert "ignoring" in caplog.text
    assert "SpaceRouter-Test" in caplog.text


def test_picks_test_when_variant_is_test(fake_macos, monkeypatch, caplog):
    """Build variant = test → SpaceRouter-Test wins. This was the
    v1.5.0-test.85 footgun: the migrator picked the prod dir (with a prod
    coord URL inside its spacerouter.env) and stamped it into a fresh
    test install, sending the test build at production coord. Lock the
    variant-match behaviour in."""
    _seed_legacy(fake_macos["prod"], files={"who.txt": "prod"})
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})
    monkeypatch.setattr("app.variant.BUILD_VARIANT", "test")

    target = fake_macos["target"]

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "who.txt").read_text() == "test"
    assert "ignoring" in caplog.text
    assert "build_variant='test'" in caplog.text


def test_picks_prod_when_variant_unknown(fake_macos, monkeypatch, caplog):
    """Unknown variant → falls back to "production wins" (pre-fix
    behaviour) so we don't surprise users with an arbitrary pick."""
    _seed_legacy(fake_macos["prod"], files={"who.txt": "prod"})
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})
    monkeypatch.setattr("app.variant.BUILD_VARIANT", "something-weird")

    target = fake_macos["target"]
    moved = legacy_migration.maybe_migrate_legacy_macos(target)

    assert moved is True
    assert (target / "who.txt").read_text() == "prod"


def test_uses_only_test_dir_when_prod_missing(fake_macos):
    _seed_legacy(fake_macos["test"], files={"who.txt": "test"})
    target = fake_macos["target"]

    assert legacy_migration.maybe_migrate_legacy_macos(target) is True
    assert (target / "who.txt").read_text() == "test"


def test_settings_json_is_carried_over(fake_macos):
    """A v1.4-shipping settings.json that lived in App Support must move."""
    payload = {
        "schema_version": 1,
        "build_variant": "test",
        "node": {"port": 9091},
    }
    _seed_legacy(
        fake_macos["prod"], files={"settings.json": json.dumps(payload)},
    )
    target = fake_macos["target"]

    legacy_migration.maybe_migrate_legacy_macos(target)

    moved = json.loads((target / "settings.json").read_text())
    assert moved["build_variant"] == "test"
    assert moved["node"]["port"] == 9091


def test_migrator_runs_inside_load_provider_settings(fake_macos):
    """End-to-end: settings_loader invokes the migrator before reading."""
    _seed_legacy(
        fake_macos["prod"],
        files={
            "settings.json": json.dumps(
                {"schema_version": 1, "node": {"port": 4242}}
            )
        },
    )

    from app.settings_loader import load_provider_settings

    s = load_provider_settings(directory=fake_macos["target"])
    assert s.node.port == 4242
    assert (fake_macos["target"] / ".migrated_from_appsupport").exists()


# --- Linux XDG migration -----------------------------------------------------
#
# Pre-v1.5 the Linux GUI stored config at ``~/.config/spacerouter/`` (the
# XDG Base Dir default). v1.5 unifies on ``~/.spacerouter`` everywhere, so
# Linux v1.4 -> v1.5 upgraders need the same one-shot copy as macOS users.
# These tests fake ``Path.home()`` and ``sys.platform`` so they run green
# on any CI box.


@pytest.fixture
def fake_linux(tmp_path, monkeypatch):
    """Yield ``(home, legacy, target)`` rooted in tmp_path.

    Pre-creates the XDG parent (``~/.config``) but NOT the ``spacerouter``
    subfolder — individual tests opt in.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = home / ".config"
    xdg.mkdir(parents=True)

    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.platform", "linux")

    return {
        "home": home,
        "legacy": xdg / "spacerouter",
        "target": home / ".spacerouter",
    }


def test_linux_no_op_when_no_legacy_dir(fake_linux):
    target = fake_linux["target"]
    assert legacy_migration.maybe_migrate_legacy_linux(target) is False
    assert not target.exists()


def test_linux_migrates_when_xdg_dir_exists(fake_linux):
    legacy = fake_linux["legacy"]
    _seed_legacy(
        legacy,
        files={
            "spacerouter.env": "SR_NODE_PORT=9090\n",
            "certs/node.crt": "-----BEGIN CERTIFICATE-----\nfake\n",
            "certs/node-identity.key": "deadbeef",
            "receipts.db": "sqlite-bytes",
        },
    )
    target = fake_linux["target"]

    moved = legacy_migration.maybe_migrate_legacy_linux(target)

    assert moved is True
    assert (target / "spacerouter.env").read_text() == "SR_NODE_PORT=9090\n"
    assert (target / "certs" / "node.crt").exists()
    assert (target / "certs" / "node-identity.key").read_text() == "deadbeef"
    assert (target / "receipts.db").read_text() == "sqlite-bytes"

    # Linux uses a distinct sentinel filename so it doesn't collide with
    # the macOS one on a hypothetical dual-OS copy of ~/.spacerouter.
    sentinel = target / ".migrated_from_xdg_config"
    assert sentinel.exists()
    assert str(legacy) in sentinel.read_text()

    # Source preserved — operator cleans up manually.
    assert legacy.exists()
    assert (legacy / "spacerouter.env").exists()


def test_linux_skipped_when_sentinel_present(fake_linux):
    """Sentinel from a prior run prevents re-migration even if the
    legacy dir still exists with newer content."""
    legacy = fake_linux["legacy"]
    _seed_legacy(legacy, files={"a.txt": "v2-from-legacy"})

    target = fake_linux["target"]
    target.mkdir()
    (target / ".migrated_from_xdg_config").write_text(str(legacy) + "\n")
    (target / "a.txt").write_text("v1-already-here")

    assert legacy_migration.maybe_migrate_legacy_linux(target) is False
    # Target unchanged.
    assert (target / "a.txt").read_text() == "v1-already-here"


def test_linux_aborts_when_target_populated(fake_linux, caplog):
    legacy = fake_linux["legacy"]
    _seed_legacy(legacy, files={"file.txt": "from-legacy"})

    target = fake_linux["target"]
    target.mkdir()
    (target / "preexisting.txt").write_text("user-data-here")

    with caplog.at_level("WARNING", logger="app.legacy_migration"):
        moved = legacy_migration.maybe_migrate_legacy_linux(target)

    assert moved is False
    # Preexisting target data untouched.
    assert (target / "preexisting.txt").read_text() == "user-data-here"
    # And no copy happened.
    assert not (target / "file.txt").exists()
    # Source untouched.
    assert (legacy / "file.txt").read_text() == "from-legacy"
    # Operator-friendly warning so they can resolve manually.
    assert "Skipping auto-migration" in caplog.text


def test_linux_no_op_on_macos(tmp_path, monkeypatch):
    """Even with a fake ``~/.config/spacerouter`` populated, the macOS
    migrator must not pull from it (platform guard)."""
    home = tmp_path / "home"
    home.mkdir()
    appsupport = home / "Library" / "Application Support"
    appsupport.mkdir(parents=True)
    xdg_dir = home / ".config" / "spacerouter"
    xdg_dir.mkdir(parents=True)
    (xdg_dir / "marker.txt").write_text("xdg-data")

    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("sys.platform", "darwin")

    target = home / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_macos(target) is False
    # And confirm the Linux migrator is the no-op on macOS too.
    assert legacy_migration.maybe_migrate_legacy_linux(target) is False
    assert not target.exists()


def test_linux_no_op_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    target = tmp_path / "home" / ".spacerouter"
    assert legacy_migration.maybe_migrate_legacy_linux(target) is False


def test_linux_settings_json_is_carried_over(fake_linux):
    """A v1.4-shipping settings.json that lived in ~/.config/spacerouter/
    must move."""
    payload = {
        "schema_version": 1,
        "node": {"port": 9091},
    }
    _seed_legacy(
        fake_linux["legacy"],
        files={"settings.json": json.dumps(payload)},
    )
    target = fake_linux["target"]

    legacy_migration.maybe_migrate_legacy_linux(target)

    moved = json.loads((target / "settings.json").read_text())
    assert moved["node"]["port"] == 9091


def test_linux_migrator_runs_inside_load_provider_settings(fake_linux):
    """End-to-end: settings_loader invokes the Linux migrator before reading."""
    _seed_legacy(
        fake_linux["legacy"],
        files={
            "settings.json": json.dumps(
                {"schema_version": 1, "node": {"port": 5252}}
            )
        },
    )

    from app.settings_loader import load_provider_settings

    s = load_provider_settings(directory=fake_linux["target"])
    assert s.node.port == 5252
    assert (fake_linux["target"] / ".migrated_from_xdg_config").exists()


# --- ConfigStore (GUI) trigger ----------------------------------------------
#
# rc.5 BLK-1: prior to this fix the macOS legacy migration only ran inside the
# daemon's ``load_provider_settings`` path. The GUI's ``ConfigStore.__init__``
# creates ``~/.spacerouter`` and writes settings.json before the daemon ever
# boots; by the time the daemon's loader called the migrator, the target dir
# was no longer empty and the safety check refused to copy. v1.4 macOS users
# upgrading via the GUI silently lost their identity key + receipts.
#
# These tests pin the rc.5 fix that hooks the migrator into ConfigStore too.


def test_macos_migration_runs_via_config_store_construction(fake_macos):
    """Constructing :py:class:`ConfigStore` triggers the macOS migration
    so the GUI's first onboarding write doesn't pre-populate the target
    dir and trip the migrator's non-empty-target safety bail.
    """
    legacy = fake_macos["prod"]
    _seed_legacy(
        legacy,
        files={
            "certs/node-identity.key": "deadbeef",
            "spacerouter.env": "SR_NODE_PORT=9090\n",
        },
    )
    target = fake_macos["target"]

    # Reload gui.config_store so the patched Path.home is in effect for
    # its ``_config_dir()`` resolution.
    import importlib
    import gui.config_store as cs_mod
    importlib.reload(cs_mod)

    cs_mod.ConfigStore()

    assert (target / "certs" / "node-identity.key").read_text() == "deadbeef"
    sentinel = target / ".migrated_from_appsupport"
    assert sentinel.exists()


def test_config_store_construction_is_idempotent(fake_macos):
    """Second ConfigStore() must NOT re-copy: the sentinel file gates the
    migrator after the first run.
    """
    legacy = fake_macos["prod"]
    _seed_legacy(legacy, files={"a.txt": "v1-from-legacy"})
    target = fake_macos["target"]

    import importlib
    import gui.config_store as cs_mod
    importlib.reload(cs_mod)

    cs_mod.ConfigStore()
    assert (target / "a.txt").read_text() == "v1-from-legacy"

    # User edits the migrated file. A second ConfigStore() construction
    # must leave the edit alone (sentinel-gated).
    (target / "a.txt").write_text("v2-edited")
    cs_mod.ConfigStore()
    assert (target / "a.txt").read_text() == "v2-edited"
