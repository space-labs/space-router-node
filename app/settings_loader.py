"""Top-level provider settings loader.

Resolution order (Track P0 of the v1.5 stabilization plan):

1. If ``settings.json`` exists → load and return it.
2. Else if ``spacerouter.env`` exists → migrate it to ``settings.json``,
   rename the env file to a ``.migrated.bak`` so we never re-migrate.
3. Else → fall back to env-var resolution via the legacy ``app.config``
   path, then **save** the resolved values as ``settings.json`` so the
   next launch is JSON-driven.

This is deliberately a thin wrapper. The full env-var sweep across
``app/main.py``, ``gui/api.py``, etc. is a follow-up track (P5/P10);
this module only adds the new entry point + the migration glue.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.settings_v2 import Settings

logger = logging.getLogger(__name__)


def _spacerouter_dir() -> Path:
    """Return the user's ``~/.spacerouter`` directory.

    We use ``Path.home() / ".spacerouter"`` regardless of platform — this
    matches the schema's ``settlement_key_path`` default. The GUI uses a
    different (platform-native) location for its existing
    ``spacerouter.env``; that path is passed in explicitly via
    :py:func:`load_provider_settings_from`.
    """
    return Path.home() / ".spacerouter"


def settings_path(directory: Path | None = None) -> Path:
    return (directory or _spacerouter_dir()) / "settings.json"


def env_path(directory: Path | None = None) -> Path:
    return (directory or _spacerouter_dir()) / "spacerouter.env"


def load_provider_settings(directory: Path | None = None) -> Settings:
    """Resolve provider settings using the full Track P0 chain.

    *directory* defaults to ``~/.spacerouter``. With v1.5's path
    unification both GUI and CLI now agree on this location, so callers
    rarely need to override.
    """
    directory = directory or _spacerouter_dir()

    # Step 0 — one-shot copy of legacy per-platform config dirs.
    # macOS pulls from ~/Library/Application Support/SpaceRouter[-Test]/.
    # Linux pulls from ~/.config/spacerouter/ (XDG default).
    # Windows v1.4 already used ~/.spacerouter, so no migration there.
    # Both calls are no-ops on the wrong platform or when the relevant
    # sentinel says we already did it.
    # Done BEFORE we look for settings.json so the migrated file ends up
    # exactly where load() expects it.
    try:
        from app.legacy_migration import maybe_migrate_legacy_macos
        moved = maybe_migrate_legacy_macos(directory)
        if moved:
            logger.info("legacy macOS migration: migrated to %s", directory)
        else:
            logger.debug("legacy macOS migration: skipped (not applicable)")
    except Exception:  # noqa: BLE001
        # Best-effort: never let a migration glitch block startup.
        logger.warning("legacy macOS migration skipped due to error", exc_info=True)

    try:
        from app.legacy_migration import maybe_migrate_legacy_linux
        moved = maybe_migrate_legacy_linux(directory)
        if moved:
            logger.info("legacy Linux XDG migration: migrated to %s", directory)
        else:
            logger.debug("legacy Linux XDG migration: skipped (not applicable)")
    except Exception:  # noqa: BLE001
        # Best-effort: never let a migration glitch block startup.
        logger.warning(
            "legacy Linux XDG migration skipped due to error", exc_info=True
        )

    s_path = settings_path(directory)
    e_path = env_path(directory)

    # Step 1 — JSON exists, just load it.
    if s_path.exists():
        s = Settings.load(s_path)
        dirty = False
        # In-place upgrade for users coming from the test.95 receipt-bug
        # era: their settings.json was wiped to bare defaults by Fresh
        # Restart and never repopulated. test.97/.101's reset() fix only
        # acts on future resets, so any user who already had escrow
        # disabled is stuck in a dead state until they manually edit the
        # file. Backfill testnet defaults on this load if the section
        # is unconfigured.
        if _backfill_test_escrow_in_place(s):
            dirty = True
        # Reconcile ``identity_passphrase_set`` against the actual keystore
        # on disk. Pre-rc.3 the flag was trusted as written, so a user
        # who manually deleted ``node-identity.key`` (or ran reset and
        # re-ran the wizard without a passphrase) would still see the
        # GUI assume a passphrase was set, which routed startup into
        # PASSPHRASE_REQUIRED for a key that wasn't there. Settings.json
        # is the cache; the keystore file is the source of truth.
        if _reconcile_passphrase_flag_in_place(s):
            dirty = True
        if dirty:
            try:
                s.save(s_path)
                logger.info(
                    "Reconciled %s with on-disk state (escrow defaults / "
                    "passphrase flag).",
                    s_path,
                )
            except OSError as e:
                logger.warning(
                    "Could not persist reconciled settings to %s: %s",
                    s_path, e,
                )
        logger.info("settings loaded from: %s", s_path)
        return s

    # Step 2 — legacy env file exists, migrate.
    if e_path.exists():
        s = Settings.migrate_from_env_file(e_path, s_path)
        logger.info("settings loaded from: %s (migrated from %s)", s_path, e_path)
        return s

    # Step 3 — last-resort env-var resolution. Build a Settings from
    # whatever ``SR_*`` vars are in os.environ, persist it, and use it.
    env_vars = {k: v for k, v in os.environ.items() if k.startswith("SR_")}
    if env_vars:
        s = Settings.from_env_mapping(env_vars)
        directory.mkdir(parents=True, exist_ok=True)
        s.save(s_path)
        logger.info("settings loaded from: %s (seeded from environment)", s_path)
        return s

    # Step 4 — no config anywhere. Persist a defaults-only ``settings.json``
    # so the next launch is JSON-driven (and so the daemon's first-run
    # log clearly shows where canonical config lives). The pre-Phase-1
    # behaviour was to return defaults without writing — that left the
    # macOS test build's ``~/.spacerouter/`` empty after a cold start
    # (only ``daemon.lock`` was created). See v1.5.0-test.80 E2E report.
    s = Settings()
    _backfill_test_escrow_in_place(s)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        s.save(s_path)
        logger.info("settings loaded from: %s (cold-start defaults persisted)", s_path)
    except OSError as e:
        # Best-effort: if disk is read-only or perms refuse, fall back
        # to in-memory defaults rather than blocking startup.
        logger.warning(
            "could not persist cold-start settings.json at %s: %s — using defaults in memory",
            s_path,
            e,
        )
    return s


# Testnet defaults — duplicated from gui/config_store.py on purpose.
# settings_loader is reachable from both the daemon (no GUI deps) and the
# CLI; importing gui.* would pull pywebview into a CLI-only invocation.
# Keep the two lists in lock-step.
_TEST_ESCROW_CONTRACT_ADDRESS = "0xC5740e4e9175301a24FB6d22bA184b8ec0762852"
_TEST_ESCROW_CHAIN_RPC = "https://rpc.cc3-testnet.creditcoin.network"
_TEST_ESCROW_CHAIN_ID = 102031


def reconcile_passphrase_flag_in_place(s: Settings) -> bool:
    """Public wrapper for :py:func:`_reconcile_passphrase_flag_in_place`.

    Exposed so non-daemon callers (notably the GUI's :py:class:`ConfigStore`,
    which has its own settings.json read path that doesn't go through
    :py:func:`load_provider_settings`) can apply the same keystore-vs-flag
    reconciliation. See the underscore-prefixed implementation for the
    full rationale.
    """
    return _reconcile_passphrase_flag_in_place(s)


def _reconcile_passphrase_flag_in_place(s: Settings) -> bool:
    """Refresh ``wallet.identity_passphrase_set`` from the on-disk keystore.

    Returns True if the value changed (caller saves on True). The flag
    is a derived cache: the truth is whether the keystore at
    :py:attr:`WalletSection.settlement_key_path` is encrypted JSON. We
    recompute it on every load so a manual ``rm node-identity.key`` (or
    a reset that wipes the keystore but somehow leaves settings.json) is
    self-healing rather than silently routing the daemon into
    PASSPHRASE_REQUIRED for a missing file.

    A missing keystore file → flag goes False (the wizard will recreate
    it without a passphrase).
    A plaintext-hex keystore → flag goes False.
    An encrypted keystore JSON → flag goes True.
    """
    try:
        from app.identity import _is_keystore_json
    except Exception:  # noqa: BLE001
        # Identity module not importable for some reason — leave the
        # cached flag alone rather than blocking startup.
        return False

    key_path = Path(s.wallet.settlement_key_path).expanduser()
    actual = False
    if key_path.is_file():
        try:
            actual = _is_keystore_json(key_path.read_text())
        except OSError:
            # Unreadable file: don't change the flag.
            return False

    if s.wallet.identity_passphrase_set != actual:
        logger.info(
            "Reconciled identity_passphrase_set: %s → %s (keystore at %s)",
            s.wallet.identity_passphrase_set, actual, key_path,
        )
        s.wallet.identity_passphrase_set = actual
        return True
    return False


def _backfill_test_escrow_in_place(s: Settings) -> bool:
    """Backfill testnet escrow defaults onto an unconfigured test variant.

    Returns True if any field was modified — caller saves only on True.

    The discriminator is ``escrow.contract_address``: if that's None on
    a test build, the section was clearly never populated (or was
    wiped by the test.95 reset bug). We can safely fill in the testnet
    addresses + flip ``enabled=true``. We do NOT touch a build that
    already has a contract address (operator-set or future mainnet
    configuration), nor non-test variants.

    NOTE: ``leg2_rate_per_gb`` is INTENTIONALLY not seeded here. The
    daemon's TOFU sync (PR #76 + #94 fix) fetches the canonical rate
    from the coord's /config endpoint at boot and overwrites whatever
    is on disk. Pre-seeding a placeholder caused test.101's
    SIGN_REJECTED_UNKNOWN_REQUEST regression — the bootstrap value
    (1e15) was 500,000× too low vs the canonical 5e20 wei/GB. The
    receipt submitter init gate (``NODE_RATE_PER_GB > 0``) will keep
    the submitter idle until TOFU sync populates the rate, which is
    the safe behavior when the coord is unreachable.
    """
    bv = (s.build_variant or "").lower()
    if bv != "test":
        return False
    if s.escrow.contract_address:
        # Operator already configured an escrow contract — respect it.
        return False

    s.escrow.enabled = True
    s.escrow.contract_address = _TEST_ESCROW_CONTRACT_ADDRESS
    s.escrow.chain_rpc = _TEST_ESCROW_CHAIN_RPC
    s.escrow.chain_id = _TEST_ESCROW_CHAIN_ID
    return True
