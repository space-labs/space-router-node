"""Single canonical config directory for Space Router Node.

The v1.5 stabilization plan unified the provider config dir to
``~/.spacerouter/`` on every platform — Linux, macOS, and Windows. The
prior macOS-native ``~/Library/Application Support/SpaceRouter[-Test]/``
location and the Windows ``%APPDATA%\\SpaceRouter`` location are both
abandoned. ``Path.home()`` resolves to the user profile on every
platform Python supports, so this works uniformly.

Legacy macOS data is migrated by :py:mod:`app.legacy_migration`; that
module runs before settings.json is loaded so the file ends up in the
right place when we look for it.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def config_dir(variant: str | None = None) -> Path:
    """Return the canonical config directory: ``~/.spacerouter``.

    The *variant* argument is accepted for backward-compatibility with
    callers from before the unification, but it is intentionally
    ignored. There is no longer a per-variant directory — variant lives
    in ``settings.json`` instead.
    """
    # *variant* is kept in the signature for callers that haven't been
    # updated yet; intentionally unused.
    del variant
    return Path.home() / ".spacerouter"


def wipe_operational_state(directory: Path) -> list[str]:
    """Best-effort delete of operational artefacts under *directory*.

    Reset Node / Fresh Restart promises a clean slate, but the v1.5.0-rc.2
    reset only wiped settings.json + identity keys. Receipts queue,
    incident banner state, and rotating logs survived — so a "fresh"
    node still showed stale failed-claim entries and old incidents on
    next start. This helper closes that gap.

    Each artefact is deleted independently so a failure on one (e.g.
    Windows holding a log file open) doesn't block the others. Returns
    a list of human-readable lines describing what happened, for
    callers that want to surface progress.
    """
    notes: list[str] = []

    # Receipt store — pending claims, settled receipts, attempt history.
    receipts_db = directory / "receipts.db"
    if receipts_db.is_file():
        try:
            receipts_db.unlink()
            notes.append(f"Removed {receipts_db}")
        except OSError as e:
            notes.append(f"Could not remove {receipts_db}: {e}")
    # SQLite WAL/SHM siblings — leftovers if a connection was open.
    for sibling in ("receipts.db-wal", "receipts.db-shm"):
        p = directory / sibling
        if p.is_file():
            try:
                p.unlink()
                notes.append(f"Removed {p}")
            except OSError as e:
                notes.append(f"Could not remove {p}: {e}")

    # Incident banner state — the GUI's sticky operator alerts.
    incidents = directory / "incidents.json"
    if incidents.is_file():
        try:
            incidents.unlink()
            notes.append(f"Removed {incidents}")
        except OSError as e:
            notes.append(f"Could not remove {incidents}: {e}")

    # Rotating log directory — tree may be open on Windows; ignore_errors
    # so the rest of the reset still completes.
    logs_dir = directory / "logs"
    if logs_dir.is_dir():
        shutil.rmtree(logs_dir, ignore_errors=True)
        if not logs_dir.exists():
            notes.append(f"Removed {logs_dir}/")
        else:
            notes.append(
                f"Could not fully remove {logs_dir}/ — some files may "
                f"be in use; restart and try again if it persists."
            )

    # Single-instance + claim coordination locks. These are pid/flock-style
    # files; if Reset Node leaves them behind, the next start either
    # refuses to launch (single-instance check sees a stale daemon.lock)
    # or skips a claim cycle (claim.lock looks taken). They're owned by
    # the running process, so deletion here is safe — the next start
    # rewrites them.
    for lock_name in ("daemon.lock", "claim.lock"):
        lock = directory / lock_name
        if lock.is_file():
            try:
                lock.unlink()
                notes.append(f"Removed {lock}")
            except OSError as e:
                notes.append(f"Could not remove {lock}: {e}")

    return notes
