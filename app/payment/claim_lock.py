"""Cross-platform exclusive lock for the on-chain claim path.

The provider runs claim_all() from two surfaces — the CLI ``--claim``
subcommand in :mod:`app.main` and the GUI's "Claim All" background
runner in :mod:`gui.api`. Both ultimately call
:func:`app.payment.settlement.claim_all` which builds raw txs and
broadcasts them. Without serialization, two concurrent claims pull the
same nonces from the receipt store and submit two ``claimBatch`` txs to
the chain — one lands, the second reverts (nonce already used). On a
flaky RPC, that revert can also burn the operator's ``claim_attempts``
budget on receipts that are actually settled.

This module provides one canonical lock acquisition path used from both
surfaces (P3/L3 in the v1.5 plan). The lock file is
``~/.spacerouter/claim.lock`` — ``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows. Stale-lock recovery is automatic: the
OS releases the lock when the holder dies, and anything the OS holds on
to past that point (a delayed Windows release, an orphaned child that
inherited the descriptor, a recycled PID) is resolved by the holder-stamp
check in :func:`acquire_claim_lock`.

Use as a context manager::

    from app.payment.claim_lock import acquire_claim_lock, ClaimLockHeld

    try:
        with acquire_claim_lock(settings):
            await claim_all(...)
    except ClaimLockHeld:
        ...

The settings object only needs to expose ``RECEIPT_STORE_PATH`` — the
lock file lives next to the receipts DB (same directory as
``~/.spacerouter/``) so an operator can ``rm`` it manually if they
ever need to.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_RECLAIM_ATTEMPTS = 8
_RECLAIM_DELAY_S = 0.25
_COMM_MAX_LEN = 15


class ClaimLockHeld(Exception):
    """Raised when ``claim.lock`` is already held by another process.

    Surfaced to the CLI as a non-zero exit; surfaced to the GUI as a
    silent ``noop`` so a double-click doesn't show a scary error.
    """


def claim_lock_path(settings) -> Path:
    """Resolve ``~/.spacerouter/claim.lock`` from a Settings object.

    Both the CLI and GUI build the lock path the same way — next to
    the receipts DB. Centralized here so tests don't have to hardcode
    the layout.
    """
    receipts_db = Path(settings.RECEIPT_STORE_PATH).expanduser()
    return receipts_db.parent / "claim.lock"


def _image_name(name: str | None) -> str:
    """Normalize a process image name so two spellings of it compare equal.

    Linux ``/proc/<pid>/comm`` truncates to 15 bytes and Windows adds an
    ``.exe`` suffix, so both sides are lowercased, de-suffixed and cut to
    that width before comparison.
    """
    if not name:
        return ""
    base = os.path.basename(name.strip()).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base[:_COMM_MAX_LEN]


def _our_image_name() -> str:
    """Image name of this claim process — frozen binary or interpreter."""
    return _image_name(sys.executable)


def _stamped_holder(lock_path: Path) -> tuple[int | None, str | None]:
    """Read the ``(pid, image name)`` stamped into the lock file.

    Line 1 is the holder PID, line 2 its image name. The image line is
    optional: a stamp written by a pre-v1.5.2 build carries the PID
    alone and yields ``(pid, None)``. An empty, partially-written or
    non-integer file yields ``(None, None)``.
    """
    try:
        lines = [ln.strip() for ln in lock_path.read_text().splitlines()]
    except Exception:
        return None, None
    lines = [ln for ln in lines if ln]
    if not lines:
        return None, None
    try:
        pid = int(lines[0].split()[0])
    except (ValueError, IndexError):
        return None, None
    return pid, (_image_name(lines[1]) if len(lines) > 1 else None)


def _stamped_pid(lock_path: Path) -> int | None:
    """Holder PID stamped into the lock file, or ``None``."""
    return _stamped_holder(lock_path)[0]


def _pid_is_alive(pid: int) -> bool:
    """POSIX liveness probe via ``os.kill(pid, 0)``.

    ``signal 0`` performs the permission/existence check without
    delivering a signal: it raises ``ProcessLookupError`` for a dead
    PID, ``PermissionError`` if the PID is alive but owned by another
    user (still alive → True), and returns cleanly for our own live
    PIDs. A nonsensical PID (<= 0) is treated as not-alive so a corrupt
    stamp can't wedge the lock forever.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, just not ours to signal.
        return True
    except OSError:
        # Unexpected errno — err on the side of "still held" so we never
        # reclaim a lock that might have a live owner.
        return True
    return True


def _pid_image_name(pid: int) -> str | None:
    """Image name of a live PID, or ``None`` when it cannot be determined."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/comm") as fh:
                return _image_name(fh.read()) or None
        except Exception:
            return None
    if sys.platform == "win32":
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return _image_name(out.stdout) or None


def _holder_is_live_claim(pid: int | None, image: str | None) -> bool:
    """True only when the stamped holder is a live process that is one of ours.

    Mirrors ``_pid_is_our_daemon`` behind ``daemon.lock`` in
    :mod:`app.main`: a bare liveness probe is not enough, because a
    recycled PID belonging to an unrelated process would otherwise look
    like a running claim and wedge the lock forever (BUG-136). A live
    PID whose image we cannot read, and a legacy stamp with no image
    line at all, both stay "held" — refusing a claim is always safer
    than broadcasting a second ``claimBatch``.
    """
    if pid is None or pid <= 0 or not _pid_is_alive(pid):
        return False
    if image is None:
        return True
    running = _pid_image_name(pid)
    if running is None:
        return True
    return running == image


def _open_lock(lock_path: Path):
    """Open the lock file read/write, creating it without truncating.

    Deliberately not append mode: ``O_APPEND`` forces every write to
    end-of-file, which would defeat the in-place PID stamp. Not ``"w"``
    either — truncating on open would destroy the holder stamp the
    contention path needs to read. Binary/untranslated so the stamp's
    byte length matches its character length on Windows too.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(lock_path, flags, 0o644), "r+", newline="")


def _try_lock(handle, is_windows: bool) -> bool:
    """Take the OS exclusive lock, returning ``False`` when contended."""
    try:
        if is_windows:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _stamp_holder(handle) -> None:
    """Record this process's PID and image name in the locked file.

    The new content is written before the file is truncated, so a
    process killed part-way through the stamp leaves the previous line
    intact rather than an empty file that the next acquirer would read
    as an abandoned lock. Failures are non-fatal — the OS lock, not the
    stamp, is the source of truth.
    """
    payload = f"{os.getpid()}\n{_our_image_name()}\n"
    try:
        handle.seek(0)
        handle.write(payload)
        handle.flush()
        handle.truncate(len(payload))
    except Exception:
        pass


def _reclaim_stale_lock(lock_path: Path, handle, is_windows: bool):
    """Ride out a contended lock whose holder is not a live claim.

    Returns ``(handle, acquired)``. A live sibling claim is refused
    immediately — that is the guard the lock exists for. Anything else
    is retried for a short grace period first: the OS does not always
    release the lock the instant the holder dies (``msvcrt`` after a
    Windows ``TerminateProcess``, an orphaned child that inherited the
    file descriptor from a ``kill -9``'d parent), and a holder killed in
    the microseconds before it stamped itself needs one beat to be told
    apart from a genuine sibling. Only once the grace has elapsed with
    no identifiable owner do we unlink the abandoned inode and take a
    fresh one — the automated form of the ``rm ~/.spacerouter/claim.lock``
    workaround.
    """
    for _ in range(_RECLAIM_ATTEMPTS):
        pid, image = _stamped_holder(lock_path)
        if _holder_is_live_claim(pid, image):
            return handle, False
        time.sleep(_RECLAIM_DELAY_S)
        try:
            handle.close()
        except Exception:
            pass
        handle = _open_lock(lock_path)
        if _try_lock(handle, is_windows):
            logger.warning(
                "claim.lock at %s was held by a stale holder (pid=%s) — "
                "reclaimed once the OS released it.", lock_path, pid,
            )
            return handle, True

    pid, image = _stamped_holder(lock_path)
    if _holder_is_live_claim(pid, image):
        return handle, False
    try:
        handle.close()
    except Exception:
        pass
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    handle = _open_lock(lock_path)
    if _try_lock(handle, is_windows):
        logger.warning(
            "claim.lock at %s was abandoned by pid=%s — reclaiming on a "
            "fresh lock file.", lock_path, pid,
        )
        return handle, True
    return handle, False


@contextlib.contextmanager
def acquire_claim_lock(settings) -> Iterator[Path]:
    """Acquire ``claim.lock`` exclusively or raise :class:`ClaimLockHeld`.

    Yields the resolved lock path so callers can surface it in error
    messages. Releases the lock on exit. The first line of defence is
    the OS primitive (POSIX ``flock`` releases on fd close → process
    death; Windows ``msvcrt.locking`` with ``LK_NBLCK`` releases on
    holder process exit), which covers the common crash.

    Implementation note: once acquired, the underlying file handle stays
    open for the duration of the ``with`` block; we never close+reopen
    mid-flight, which is what would let another process race in.

    Stale-lock reclaim: when the exclusive lock is contended we read the
    holder stamp (PID + process image) and only refuse if that stamp
    resolves to a *live process running one of our binaries*. A dead
    holder, a PID recycled to an unrelated process, or a stamp we cannot
    read is retried for a short grace period and then reclaimed, so a
    ``kill -9`` mid-claim never wedges the next claim (BUG-136). A live
    sibling claim still loses the race — the single-claim concurrency
    guard is what stops two ``claimBatch`` broadcasts, and it is not
    negotiable.
    """
    lock_path = claim_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    is_windows = sys.platform == "win32"

    fd = _open_lock(lock_path)
    try:
        if not _try_lock(fd, is_windows):
            fd, acquired = _reclaim_stale_lock(lock_path, fd, is_windows)
            if not acquired:
                try:
                    fd.close()
                except Exception:
                    pass
                pid, _image = _stamped_holder(lock_path)
                logger.info(
                    "claim.lock contended at %s (holder pid=%s) — another "
                    "claim is running", lock_path, pid,
                )
                raise ClaimLockHeld(str(lock_path))

        _stamp_holder(fd)

        try:
            yield lock_path
        finally:
            try:
                if is_windows:
                    import msvcrt
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            fd.close()
        except Exception:
            pass
