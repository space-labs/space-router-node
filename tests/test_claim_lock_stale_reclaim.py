"""BUG-132-03 — failed-gas claim must not wedge the claim lock forever.

Two-part regression for the "Another claim was already running —
skipped" trap that hit 100% of insufficient-gas claims:

1. **Stale-lock reclaim** (``app.payment.claim_lock``). A ``claim.lock``
   stamped with a DEAD holder PID is reclaimed on the next acquire
   instead of raising :class:`ClaimLockHeld` — this auto-recovers the
   field workaround of "stop node → ``rm ~/.spacerouter/claim.lock`` →
   restart" after a claim process crashed/was killed mid-flight. A lock
   genuinely held by a LIVE process must still raise — the single-claim
   concurrency guard is non-negotiable.

2. **Fail-fast on insufficient gas** (``app.payment.settlement``). A
   pre-flight balance check aborts the run BEFORE broadcasting +
   entering the 120s receipt wait, so an under-funded wallet releases
   the lock immediately (and gets an actionable CLAIM_INSUFFICIENT_GAS
   result) instead of holding it for two minutes while the auto-claim
   re-fires every 30s.

The lock tests use a REAL ``acquire_claim_lock`` on a temp settings
path — no mocking of the lock primitive. The settlement test stubs ONLY
the web3 balance/gas boundary (the external RPC edge), not the
lock/claim logic.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.payment import reasons
from app.payment.eip712 import (
    ESCROW_DOMAIN_NAME,
    ESCROW_DOMAIN_VERSION,
    EIP712Domain,
    Receipt,
    sign_receipt,
)
from app.payment.receipt_store import get_store


def _mk_settings(db_path: Path, payer: str | None = None):
    """Minimal duck-typed settings — the code only reads attributes."""
    class S:
        ESCROW_CHAIN_RPC = "http://fake-rpc.invalid"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        RECEIPT_STORE_PATH = str(db_path)
        GATEWAY_PAYER_ADDRESS = payer or ""
        CLAIM_BATCH_SIZE = 50
    return S()


# ── Part 1: stale-lock reclaim ─────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX-only: the dead-PID reclaim path uses os.kill(pid, 0). On "
        "Windows the OS releases msvcrt locks on process exit, so a "
        "contended lock is always a live holder (kept raising)."
    ),
)
def test_dead_pid_lock_is_reclaimed(tmp_path):
    """A claim.lock CONTENDED by a flock but stamped with a DEAD PID is
    reclaimed; acquire succeeds instead of raising ClaimLockHeld.

    Simulates the BUG-132 field state: a previous claim process left the
    lock file behind with its (now stale) PID. We force the contended
    branch by holding a real ``flock`` on the file from a separate fd
    while stamping a dead PID — this exercises the unlink-and-reopen
    reclaim path, not the OS-level auto-release path that the existing
    hardening suite already covers.
    """
    import fcntl
    import os

    from app.payment.claim_lock import acquire_claim_lock, claim_lock_path

    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Find a PID that is definitely dead: walk down from a high value
    # until os.kill(pid, 0) reports ProcessLookupError.
    dead_pid = 999_999
    while dead_pid > 1:
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            break  # confirmed dead
        except OSError:
            pass  # alive (or not ours) — keep searching
        dead_pid -= 1
    assert dead_pid > 1, "could not find a dead PID to stamp"

    # Hold a genuine flock on the file (forces the contention branch),
    # but stamp it with the dead PID. In production the dead holder's
    # flock would already be released by the OS; we hold one here only
    # so the reclaim *branch* is exercised deterministically.
    holder_fd = open(lock_path, "w")
    fcntl.flock(holder_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_path.write_text(f"{dead_pid}\n")
    holder_fd.flush()
    try:
        with acquire_claim_lock(settings) as held:
            assert held == lock_path
            # We now hold it; the stamp is rewritten to OUR pid.
            stamped = claim_lock_path(settings).read_text().strip().splitlines()[0]
            assert stamped == str(os.getpid())
    finally:
        try:
            holder_fd.close()
        except Exception:
            pass


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: exercises the flock contention + reclaim branch.",
)
def test_unreadable_lock_stamp_is_reclaimed(tmp_path):
    """A CONTENDED lock whose stamp is garbage (no usable PID) is
    reclaimed too. A partially-written / hand-edited file shouldn't be
    able to wedge the lock — without a readable live holder there's
    nothing to guard.
    """
    import fcntl

    from app.payment.claim_lock import acquire_claim_lock, claim_lock_path

    settings = _mk_settings(tmp_path / "receipts.db")
    lock_path = claim_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder_fd = open(lock_path, "w")
    fcntl.flock(holder_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_path.write_text("not-a-pid\n")
    holder_fd.flush()
    try:
        with acquire_claim_lock(settings):
            pass  # acquired without raising
    finally:
        try:
            holder_fd.close()
        except Exception:
            pass


def test_live_holder_still_blocks_second_claim(tmp_path):
    """The concurrency guard holds: a lock held by a LIVE process (us,
    in this test) makes the second acquire raise ClaimLockHeld.

    This is the no-regression assertion the reviewer cares about — the
    stale-reclaim must NOT reclaim a lock owned by a live sibling.
    """
    from app.payment.claim_lock import acquire_claim_lock, ClaimLockHeld

    settings = _mk_settings(tmp_path / "receipts.db")

    with acquire_claim_lock(settings):
        # The stamped PID is our own live PID → reclaim must be refused.
        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pytest.fail("nested acquire on a live-held lock must raise")

    # After release the lock is free again.
    with acquire_claim_lock(settings):
        pass


def test_pid_liveness_helpers(tmp_path):
    """Unit-level coverage of the PID probe + stamp reader."""
    import os

    from app.payment.claim_lock import _pid_is_alive, _stamped_pid

    # Our own PID is alive.
    assert _pid_is_alive(os.getpid()) is True
    # Nonsense PIDs are treated as not-alive (can't wedge the lock).
    assert _pid_is_alive(0) is False
    assert _pid_is_alive(-1) is False

    lock = tmp_path / "claim.lock"
    lock.write_text("4321\n")
    assert _stamped_pid(lock) == 4321
    lock.write_text("garbage\n")
    assert _stamped_pid(lock) is None
    lock.write_text("")
    assert _stamped_pid(lock) is None
    assert _stamped_pid(tmp_path / "does-not-exist.lock") is None


# ── Part 2: settlement pre-flight gas check ────────────────────────


_GATEWAY_KEY = "0x" + "11" * 32


def _gateway_address() -> str:
    from eth_account import Account
    return Account.from_key(_GATEWAY_KEY).address


def _mk_receipt(**overrides) -> Receipt:
    base = dict(
        client_address="0x" + "a" * 40,
        node_address="0x" + "b" * 64,
        request_uuid=str(uuid.uuid4()),
        data_amount=1024,
        total_price=1,
    )
    base.update(overrides)
    return Receipt(**base)


def _domain_for(settings) -> EIP712Domain:
    return EIP712Domain(
        name=ESCROW_DOMAIN_NAME,
        version=ESCROW_DOMAIN_VERSION,
        chain_id=int(settings.ESCROW_CHAIN_ID),
        verifying_contract=settings.ESCROW_CONTRACT_ADDRESS,
    )


@pytest.mark.asyncio
async def test_preflight_underfunded_aborts_before_broadcast(tmp_path):
    """An under-funded wallet aborts with CLAIM_INSUFFICIENT_GAS and
    NEVER calls send_raw_transaction / wait_for_transaction_receipt.

    This is the fail-fast that releases the lock promptly. Only the
    web3 balance/gas boundary is stubbed — the claim logic is real.
    """
    from app.payment.settlement import _submit_batch

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    payer_addr = _gateway_address()
    settings = _mk_settings(db, payer=payer_addr)
    domain = _domain_for(settings)

    r = _mk_receipt()
    sig = sign_receipt(_GATEWAY_KEY, r, domain)
    await store.store(r, signature=sig)
    batch = [await store.get_by_uuid(r.request_uuid)]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch(
        "app.payment.settlement.asyncio.to_thread", fake_to_thread,
    ), patch("web3.Web3") as MockWeb3, \
         patch("eth_account.Account") as MockAccount:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        inst.eth.gas_price = 1_000_000_000  # 1 gwei
        # Wallet is broke: balance < gas_limit * gas_price.
        inst.eth.get_balance.return_value = 5
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "ab" * 32
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    assert result.reason_code == reasons.CLAIM_INSUFFICIENT_GAS
    assert "insufficient gas" in (result.error or "").lower()
    # Fail-fast: the doomed broadcast + receipt-wait are never reached.
    inst.eth.send_raw_transaction.assert_not_called()
    inst.eth.wait_for_transaction_receipt.assert_not_called()

    # Receipt stays retryable (transient) — no breadcrumb was set since
    # nothing went in flight, and the budget isn't consumed.
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is None
    assert final.claim_tx_pending is None
    assert final.last_error_code == reasons.CLAIM_INSUFFICIENT_GAS
    assert final.claim_attempts == 0  # transient → no budget consumed


@pytest.mark.asyncio
async def test_preflight_funded_wallet_proceeds_to_broadcast(tmp_path):
    """No regression: a funded wallet (balance >= gas cost) passes the
    pre-flight check and broadcasts + settles normally.
    """
    from app.payment.settlement import _submit_batch

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    payer_addr = _gateway_address()
    settings = _mk_settings(db, payer=payer_addr)
    domain = _domain_for(settings)

    r = _mk_receipt()
    sig = sign_receipt(_GATEWAY_KEY, r, domain)
    await store.store(r, signature=sig)
    batch = [await store.get_by_uuid(r.request_uuid)]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch(
        "app.payment.settlement.asyncio.to_thread", fake_to_thread,
    ), patch("web3.Web3") as MockWeb3, \
         patch("eth_account.Account") as MockAccount:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        inst.eth.gas_price = 1
        # Plenty of gas money.
        inst.eth.get_balance.return_value = 10**18
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 120_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        sent_hash = MagicMock()
        sent_hash.hex.return_value = "0x" + "1" * 64
        inst.eth.send_raw_transaction.return_value = sent_hash
        inst.eth.wait_for_transaction_receipt.return_value = MagicMock(
            status=1, gasUsed=42_000,
        )

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "1" * 64
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    assert result.reason_code is None
    assert result.error is None
    inst.eth.send_raw_transaction.assert_called_once()
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is not None
    assert final.claim_tx_hash == "0x" + "1" * 64
