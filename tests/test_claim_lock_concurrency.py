"""claim.lock must never let two claims run at once.

QA v1.5.2-test.136 (Jin Lee, Linux CLI item 8): after `kill -9` part-way
through `--claim`, the next `--claim` was refused with "claim.lock contended"
against a PID that was already dead.

The important finding is that the reported sequence recovers on its own. POSIX
releases `flock` when the holder dies, so the next acquire simply succeeds and
never reaches the contention branch. What did NOT recover was a lock whose
holder could not be identified — and the fix for that was worse than the bug:
it unlinked the lock file and took a fresh inode, which hands a second claim
the lock while the first is still broadcasting claimBatch on chain.

So the invariant these tests pin is the money guard, not the reclaim: while
another process holds the lock, acquiring must be refused, whatever the stamped
PID happens to say. A stamp that looks dead, is unreadable, or belongs to a
recycled PID is not evidence the holder is gone; the lock itself is.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.payment.claim_lock import ClaimLockHeld, acquire_claim_lock, claim_lock_path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="drives POSIX flock semantics directly"
)


def _mk_settings(db_path: Path):
    class _S:
        RECEIPT_STORE_PATH = str(db_path)
        RECEIPT_DB_PATH = str(db_path)

    return _S()


def _holder_script(lock_path: Path, hold_seconds: float) -> str:
    """A separate process that takes the real lock and holds it."""
    return textwrap.dedent(
        f"""
        import fcntl, os, sys, time
        fh = open({str(lock_path)!r}, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0); fh.truncate()
        fh.write(str(os.getpid()) + chr(10) + "space-router-node" + chr(10))
        fh.flush()
        sys.stdout.write("HELD"); sys.stdout.flush()
        time.sleep({hold_seconds})
        """
    )


def _spawn_holder(lock_path: Path, hold_seconds: float = 30.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _holder_script(lock_path, hold_seconds)],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.read(4) == "HELD", "holder did not take the lock"
    return proc


def _stamp(lock_path: Path, pid: int, image: str = "space-router-node") -> None:
    with open(lock_path, "r+") as fh:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{pid}\n{image}\n")


def test_a_killed_holder_does_not_block_the_next_claim(tmp_path):
    """Jin's reported sequence. The OS frees flock, so this must just work."""
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    holder.kill()
    holder.wait(timeout=10)

    with acquire_claim_lock(settings) as path:
        assert Path(path) == lock_path


def test_a_live_holder_is_refused(tmp_path):
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    try:
        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_live_holder_stamped_with_a_dead_pid_is_still_refused(tmp_path):
    """The money guard, and the inverse of the behaviour that was there.

    A stamp naming a dead PID while the lock is genuinely held is a
    contradiction the code cannot resolve in favour of stealing: the previous
    implementation unlinked the file and took a fresh inode, so both processes
    ended up holding a claim.
    """
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    try:
        dead_pid = 999_999
        while True:
            try:
                os.kill(dead_pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            dead_pid -= 1
        _stamp(lock_path, dead_pid)

        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_live_holder_with_an_unreadable_stamp_is_still_refused(tmp_path):
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    try:
        with open(lock_path, "r+") as fh:
            fh.seek(0)
            fh.truncate()
            fh.write("not-a-pid\n")

        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_an_unstamped_live_holder_is_still_refused(tmp_path):
    """A holder killed before it stamped leaves an empty file, not a free lock."""
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    try:
        with open(lock_path, "r+") as fh:
            fh.seek(0)
            fh.truncate()

        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_file_inode_is_never_replaced_under_a_live_holder(tmp_path):
    """Directly pins the mechanism that allowed two concurrent claims."""
    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    holder = _spawn_holder(lock_path)
    try:
        before = os.stat(lock_path).st_ino
        _stamp(lock_path, 999_998)
        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pass
        assert os.stat(lock_path).st_ino == before, (
            "the lock file was replaced while another process held it, which "
            "lets a second claimBatch broadcast run concurrently"
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_is_released_for_the_next_caller(tmp_path):
    settings = _mk_settings(tmp_path / "receipts.db")
    with acquire_claim_lock(settings):
        pass
    with acquire_claim_lock(settings):
        pass
