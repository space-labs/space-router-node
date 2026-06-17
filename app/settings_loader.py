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
import sys
from pathlib import Path

from app.settings_v2 import Settings

logger = logging.getLogger(__name__)

# v1.5.0 production footgun: the coord url default was hardcoded to the test
# fly.dev hostname and ``Settings.save()`` persisted it. ``coordination.spacerouter.org``
# and ``spacerouter-coordination-api.fly.dev`` are the same prod backend, so only the
# test hostname below is the wrong-network value we heal on production builds.
_OLD_TEST_COORD_URL = "https://spacerouter-coordination-api-test.fly.dev"
_PROD_COORD_URL = "https://coordination.spacerouter.org"


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
        # rc.6 MIN-4: pre-rc.6 schema had the wrong default
        # settlement_key_path (~/.spacerouter/identity.key vs the actual
        # ~/.spacerouter/certs/node-identity.key). Existing users have
        # the bad value persisted in settings.json; fix it on load so
        # the reconcile call below points at the real keystore.
        if _heal_settlement_key_path_in_place(s):
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
        # A1/A2: recover a staking address dropped by the v1.4→v1.5 migration
        # skip-trap (settings.json present but staking_address blank). Reads
        # the legacy env directly, so it works even when the full-dir
        # migration was skipped because ~/.spacerouter was non-empty.
        if _recover_staking_address_in_place(s, directory):
            dirty = True
        # A3: heal a persisted test coord url on production builds (v1.5.0).
        if _heal_test_coord_url_in_place(s):
            dirty = True
        if dirty:
            try:
                s.save(s_path)
                logger.info(
                    "Reconciled %s with on-disk state (escrow defaults / "
                    "passphrase flag / staking address / coord url).",
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
        # Apply the same testnet-escrow backfill we do for the JSON-load
        # and defaults-only paths. Without this, a test build that
        # cold-starts from env vars (no settings.json, no env file) wakes
        # up with escrow disabled and the receipt submitter dead.
        _backfill_test_escrow_in_place(s)
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
    # A1/A2: a lock-file or stray file in ~/.spacerouter makes the full-dir
    # migration skip, so we land here with bare defaults even though a v1.4
    # config still exists. Recover the staking address from the legacy env.
    _recover_staking_address_in_place(s, directory)
    _heal_test_coord_url_in_place(s)
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


# Test/prod escrow defaults — duplicated from gui/config_store.py on purpose.
# settings_loader is reachable from both the daemon (no GUI deps) and the
# CLI; importing gui.* would pull pywebview into a CLI-only invocation.
# Keep the two lists in lock-step.
_TEST_ESCROW_CONTRACT_ADDRESS = "0xC5740e4e9175301a24FB6d22bA184b8ec0762852"
_TEST_ESCROW_CHAIN_RPC = "https://rpc.cc3-testnet.creditcoin.network"
_TEST_ESCROW_CHAIN_ID = 102031

_PROD_ESCROW_CONTRACT_ADDRESS = "0xC130F5D76f0b4Ce8FE2ceA0D2C2b8f53A39a5cd0"
_PROD_ESCROW_CHAIN_RPC = "https://mainnet3.creditcoin.network"
_PROD_ESCROW_CHAIN_ID = 102030


def reconcile_passphrase_flag_in_place(s: Settings) -> bool:
    """Public wrapper for :py:func:`_reconcile_passphrase_flag_in_place`.

    Exposed so non-daemon callers (notably the GUI's :py:class:`ConfigStore`,
    which has its own settings.json read path that doesn't go through
    :py:func:`load_provider_settings`) can apply the same keystore-vs-flag
    reconciliation. See the underscore-prefixed implementation for the
    full rationale.
    """
    return _reconcile_passphrase_flag_in_place(s)


def recover_staking_address_in_place(
    s: Settings, directory: Path | None = None
) -> bool:
    """Public wrapper for :py:func:`_recover_staking_address_in_place`.

    Exposed for the GUI's :py:class:`gui.config_store.ConfigStore`, whose
    settings.json reads bypass :py:func:`load_provider_settings`. Without
    this the GUI would still show a blank wallet after the migration
    skip-trap even though the daemon self-heals.
    """
    return _recover_staking_address_in_place(s, directory or _spacerouter_dir())


def heal_test_coord_url_in_place(s: Settings) -> bool:
    """Public wrapper for :py:func:`_heal_test_coord_url_in_place` (GUI path)."""
    return _heal_test_coord_url_in_place(s)


def _read_staking_address_from_env(path: Path) -> str | None:
    """Best-effort read of a staking address from a v1.4-style env file.

    Looks for ``SR_STAKING_ADDRESS`` first, then the legacy single-wallet
    ``SR_WALLET_ADDRESS``. Returns a checksummed address when valid, else
    None. Tolerant of quotes, ``export`` prefixes, and ``#`` comments.
    """
    try:
        if not path.is_file():
            return None
        text = path.read_text()
    except OSError:
        return None

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in ("SR_STAKING_ADDRESS", "SR_WALLET_ADDRESS") and val:
            values[key] = val

    candidate = values.get("SR_STAKING_ADDRESS") or values.get("SR_WALLET_ADDRESS")
    if not candidate:
        return None
    try:
        from app.wallet import validate_wallet_address
        return validate_wallet_address(candidate)
    except Exception:  # noqa: BLE001 — malformed address in a legacy file; skip
        return None


def _legacy_env_candidates(directory: Path, variant: str | None) -> list[Path]:
    """Env files that may hold a previously-configured staking address.

    Order: the migrated/renamed env in the canonical dir first (in case the
    main migration ran but the address never reached settings.json), then
    the per-platform v1.4 dirs. On macOS the variant-matching Application
    Support dir comes first so a production build never adopts a test
    wallet (the v1.5.0-test.85 footgun in reverse).

    *variant* is the already-resolved ``Settings.build_variant`` passed in
    by the caller. We deliberately do NOT import ``app.variant`` here: this
    runs *inside* ``load_provider_settings``, and ``app.variant`` resolves
    the build variant by calling ``load_provider_settings`` again — that
    re-entrancy recurses until a swallowed ``RecursionError`` and floods
    the log with migration-skip warnings (caught during real end-to-end
    boot). The settings object already carries the resolved variant, so use
    it directly.
    """
    out = [
        directory / "spacerouter.env",
        directory / "spacerouter.env.migrated.bak",
    ]
    bv = (variant or "").lower()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        names = (
            ["SpaceRouter-Test", "SpaceRouter"]
            if bv == "test"
            else ["SpaceRouter", "SpaceRouter-Test"]
        )
        out.extend(base / n / "spacerouter.env" for n in names)
    elif sys.platform.startswith("linux"):
        # Linux v1.4 used the XDG default and never shipped a -Test variant.
        out.append(Path.home() / ".config" / "spacerouter" / "spacerouter.env")
    # Windows v1.4 already used ~/.spacerouter — covered by the canonical dir.
    return out


def _recover_staking_address_in_place(s: Settings, directory: Path) -> bool:
    """Recover a staking address lost by the v1.4→v1.5 migration skip-trap.

    The macOS migration (:py:mod:`app.legacy_migration`) copies the legacy
    Application Support dir only when ``~/.spacerouter`` is empty. If
    anything (a prior launch, a cold-start settings.json, logs) populated it
    first, the migration silently skips and the operator's staking address
    never reaches settings.json — the node then registers with the identity
    fallback and coord reports "no stake".

    When ``staking_address`` is blank we scan the known legacy env files and
    adopt the first valid address. This is a safe *targeted* recovery: it
    reads only the address (never a full-dir merge) and never touches a
    populated value. Returns True when a value was recovered.
    """
    if (s.wallet.staking_address or "").strip():
        return False
    for candidate in _legacy_env_candidates(directory, s.build_variant):
        addr = _read_staking_address_from_env(candidate)
        if addr:
            logger.info(
                "Recovered staking address from legacy config %s "
                "(settings.json had none after upgrade): %s",
                candidate, addr,
            )
            s.wallet.staking_address = addr
            return True
    return False


def _heal_test_coord_url_in_place(s: Settings) -> bool:
    """Heal a persisted test coord url on production builds (v1.5.0 footgun).

    v1.5.0 shipped ``CoordinationSection.url`` hardcoded to the test fly.dev
    hostname; ``Settings.save()`` persisted it. v1.5.1's variant-aware
    default only fixes *fresh* installs, so an existing settings.json keeps
    pointing at the isolated test coord and a genuinely-staked prod wallet
    reads as unstaked.

    On production builds only, if the persisted url is exactly that old test
    default, rewrite it to the prod url. Test builds (``build_variant=test``)
    legitimately use the test url and are left alone; an operator-set custom
    url never matches the exact known-bad default, so it's preserved too.
    """
    bv = (s.build_variant or "").lower()
    if bv not in ("production", "prod"):
        return False
    if s.coordination.url == _OLD_TEST_COORD_URL:
        logger.info(
            "Healing coordination.url on production build: %s → %s "
            "(v1.5.0 persisted test-url footgun)",
            _OLD_TEST_COORD_URL, _PROD_COORD_URL,
        )
        s.coordination.url = _PROD_COORD_URL
        return True
    return False


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


def _heal_settlement_key_path_in_place(s: Settings) -> bool:
    """rc.6 MIN-4: pre-rc.6 the schema default for
    ``wallet.settlement_key_path`` was wrong
    (``~/.spacerouter/identity.key``); the actual keystore lives at
    ``~/.spacerouter/certs/node-identity.key``. Existing users have the
    bad value persisted in settings.json; correct it on load so
    :py:func:`_reconcile_passphrase_flag_in_place` points at the real
    keystore (otherwise it always concludes the keystore is missing
    and silently flips ``identity_passphrase_set`` to False).

    The fix here only rewrites the documented bad default. Operator-set
    custom paths are preserved.
    """
    bad = "~/.spacerouter/identity.key"
    good = "~/.spacerouter/certs/node-identity.key"
    if s.wallet.settlement_key_path == bad:
        logger.info(
            "Healing settlement_key_path: %s → %s (rc.6 MIN-4)",
            bad, good,
        )
        s.wallet.settlement_key_path = good
        return True
    return False


def _backfill_test_escrow_in_place(s: Settings) -> bool:
    """Backfill escrow defaults onto an unconfigured test or production variant.

    Returns True if any field was modified — caller saves only on True.

    The discriminator is ``escrow.contract_address``: if that's None,
    the section was clearly never populated (or was wiped by the
    test.95 reset bug, or is a fresh production install on v1.5.0+).
    We can safely fill in the appropriate variant's escrow addresses
    and flip ``enabled=true``. We do NOT touch a build that already
    has a contract address (operator-set), nor unknown variants.

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
    if bv == "test":
        contract = _TEST_ESCROW_CONTRACT_ADDRESS
        rpc = _TEST_ESCROW_CHAIN_RPC
        chain_id = _TEST_ESCROW_CHAIN_ID
    elif bv == "production":
        contract = _PROD_ESCROW_CONTRACT_ADDRESS
        rpc = _PROD_ESCROW_CHAIN_RPC
        chain_id = _PROD_ESCROW_CHAIN_ID
    else:
        return False

    if s.escrow.contract_address:
        # Operator already configured an escrow contract — respect it.
        return False

    s.escrow.enabled = True
    s.escrow.contract_address = contract
    s.escrow.chain_rpc = rpc
    s.escrow.chain_id = chain_id
    return True
