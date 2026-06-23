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
``msvcrt.locking`` on Windows. Stale-lock recovery is automatic: on
Windows ``LK_NBLCK`` is released by the OS when the holder process
exits; on POSIX ``flock`` likewise releases when the owning fd is
closed (process death drops the fd).

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
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


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


def _stamped_pid(lock_path: Path) -> int | None:
    """Read the holder PID stamped into the lock file, or ``None``.

    The PID is best-effort metadata (see :func:`acquire_claim_lock`), so
    an empty, partially-written, or non-integer file all return ``None``.
    """
    try:
        text = lock_path.read_text().strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return int(text.splitlines()[0])
    except (ValueError, IndexError):
        return None


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


@contextlib.contextmanager
def acquire_claim_lock(settings) -> Iterator[Path]:
    """Acquire ``claim.lock`` exclusively or raise :class:`ClaimLockHeld`.

    Yields the resolved lock path so callers can surface it in error
    messages. Releases the lock on exit. Stale-lock recovery is
    inherited from the OS primitive (POSIX ``flock`` releases on fd
    close → process death; Windows ``msvcrt.locking`` with
    ``LK_NBLCK`` releases on holder process exit).

    Implementation note: the underlying file handle stays open for the
    duration of the ``with`` block; we never close+reopen mid-flight,
    which is what would let another process race in.

    Stale-lock reclaim: when the exclusive lock is contended we read the
    holder PID stamped in the file and probe it with ``os.kill(pid, 0)``.
    If the holder is dead (crashed / killed) — or the stamp is
    unreadable — we reclaim the lock and proceed; only a *live* holder
    raises :class:`ClaimLockHeld`. This recovers the field case (BUG-132)
    where a doomed claim was killed mid-flight and left the file behind,
    which previously forced operators to ``rm`` ``claim.lock`` by hand.
    A live sibling claim still loses the race (the concurrency guard is
    preserved). On Windows ``msvcrt.locking`` is released by the OS on
    process exit, so a contended lock there is always a live holder —
    we keep raising without a PID probe.
    """
    lock_path = claim_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    is_windows = sys.platform == "win32"

    def _try_lock(handle) -> None:
        if is_windows:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # Windows msvcrt.locking() needs read+write; POSIX flock() works on
    # any open fd. Append-mode ("a+") on BOTH platforms so the file
    # exists without truncating any contents on open — we need the
    # stamped holder PID to survive the open() so the contention branch
    # below can probe its liveness (a plain "w" open would truncate the
    # stamp before we ever read it, defeating stale-lock reclaim). The
    # PID is rewritten via explicit seek/truncate once we own the lock.
    fd = open(lock_path, "a+")
    try:
        try:
            _try_lock(fd)
        except (BlockingIOError, OSError) as e:
            # Contended. On POSIX, decide between a live holder (real
            # concurrency — raise) and a stale holder (crashed/killed —
            # reclaim). On Windows the OS guarantees release on exit, so
            # contention always means a live holder.
            reclaimed = False
            if not is_windows:
                pid = _stamped_pid(lock_path)
                if pid is None or not _pid_is_alive(pid):
                    # Holder is dead/unknown. Reclaim by unlinking the
                    # stale file and reopening a FRESH inode — exactly
                    # what the manual ``rm ~/.spacerouter/claim.lock``
                    # workaround did. flock() guards an open file
                    # *description*; a dead holder's flock on the old
                    # inode can't block a flock on the new one. We only
                    # ever reach here when the stamped PID is dead/absent,
                    # so a LIVE sibling (which stamps its live PID) is
                    # never unlinked — the concurrency guard holds.
                    try:
                        fd.close()
                    except Exception:
                        pass
                    try:
                        import os as _os
                        _os.unlink(lock_path)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        # Couldn't remove it (perms?) — fall through and
                        # raise rather than risk an inconsistent state.
                        pass
                    fd = open(lock_path, "a+")
                    try:
                        _try_lock(fd)
                        reclaimed = True
                        logger.warning(
                            "claim.lock at %s was held by a dead/stale "
                            "holder (pid=%s) — reclaiming.",
                            lock_path, pid,
                        )
                    except (BlockingIOError, OSError):
                        # Lost a race: a live sibling grabbed the fresh
                        # inode between our unlink and re-attempt. Fall
                        # through to raise.
                        reclaimed = False
            if not reclaimed:
                try:
                    fd.close()
                except Exception:
                    pass
                logger.info(
                    "claim.lock contended at %s — another claim is running",
                    lock_path,
                )
                raise ClaimLockHeld(str(lock_path)) from e

        # Best-effort: stamp the holder PID so a stuck-lock investigation
        # has something to look at. Failures here are non-fatal — the
        # lock itself is the source of truth.
        try:
            fd.seek(0)
            fd.truncate(0)
            fd.write(f"{os.getpid()}\n")
            fd.flush()
        except Exception:
            pass

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
                # Process death drops the fd anyway; logging here would
                # be noise on shutdown.
                pass
    finally:
        try:
            fd.close()
        except Exception:
            pass
