"""Regression tests for the v1.5 settlement-hardening track (P3).

Each loophole gets at least one targeted test:

- **L3** — CLI claim acquires the same ``claim.lock`` as the GUI runner;
  contention raises :class:`ClaimLockHeld`; stale-locker death lets the
  next caller through (POSIX path).
- **L4** — reaper stamps a block height when marking a row external;
  re-checks inside the finality soak window revert the mark if
  ``isNonceUsed`` flips back to false; rows past finality stop being
  re-checked.
- **L5** — daemon-startup in-flight reconciler resolves rows where a
  pre-broadcast breadcrumb (``claim_tx_pending``) was set but
  ``mark_claimed`` never ran; resolves both the "tx landed" and "tx
  never broadcast" branches.
- **L6** — settlement drops a corrupt-signature receipt
  (``failed_terminal``/``SIGN_VERIFY_FAILED``) without poisoning the
  rest of the batch.

All tests avoid hitting a real chain — web3 is mocked via
``unittest.mock``. Async tests use ``pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import constants
from app.payment import reasons
from app.payment.eip712 import (
    ESCROW_DOMAIN_NAME,
    ESCROW_DOMAIN_VERSION,
    EIP712Domain,
    Receipt,
    sign_receipt,
)
from app.payment.receipt_store import ReceiptStore, get_store


# ── Shared fixtures ─────────────────────────────────────────────────


# A well-formed gateway payer keypair. Tests that need a *correct*
# EIP-712 signature use this private key; tests that need a *bad* sig
# either flip a byte or sign with a different key.
_GATEWAY_KEY = (
    "0x" + "11" * 32  # 0x111...
)
_OTHER_KEY = (
    "0x" + "22" * 32
)


def _gateway_address() -> str:
    from eth_account import Account
    return Account.from_key(_GATEWAY_KEY).address


def _other_address() -> str:
    from eth_account import Account
    return Account.from_key(_OTHER_KEY).address


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


def _mk_settings(db_path: Path, payer: str | None = None):
    """Minimal settings object used by reaper / settlement code paths.

    Keep it a plain class so we don't drag pydantic into the unit
    layer; the production code only reads attributes.
    """
    class S:
        ESCROW_CHAIN_RPC = "http://fake-rpc.invalid"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        RECEIPT_STORE_PATH = str(db_path)
        GATEWAY_PAYER_ADDRESS = payer or ""
        CLAIM_BATCH_SIZE = 50
    return S()


def _domain_for(settings) -> EIP712Domain:
    return EIP712Domain(
        name=ESCROW_DOMAIN_NAME,
        version=ESCROW_DOMAIN_VERSION,
        chain_id=int(settings.ESCROW_CHAIN_ID),
        verifying_contract=settings.ESCROW_CONTRACT_ADDRESS,
    )


# ── L3: CLI/GUI claim race ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_claim_blocks_when_gui_holds_lock(tmp_path):
    """CLI's acquire_claim_lock raises ClaimLockHeld when something
    else (the GUI's runner, in production) is already inside the lock.

    Direct test of the lock primitive — exercises the same code path
    both surfaces use, so a regression in either surface trips this.
    """
    from app.payment.claim_lock import (
        acquire_claim_lock, ClaimLockHeld, claim_lock_path,
    )

    settings = _mk_settings(tmp_path / "receipts.db")

    with acquire_claim_lock(settings):
        # Second acquisition from the same process must fail-fast
        # rather than block the test forever.
        with pytest.raises(ClaimLockHeld):
            with acquire_claim_lock(settings):
                pytest.fail("nested acquire should have raised")
        # Lock file exists and is stamped with our PID.
        assert claim_lock_path(settings).exists()

    # After release, a fresh acquire works.
    with acquire_claim_lock(settings):
        pass


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX flock semantics — Windows uses msvcrt with the same end-result.",
)
def test_stale_lock_recovered_when_holder_pid_dead(tmp_path):
    """If a previous process held the lock and died, ``flock`` releases
    automatically when the holder fd was closed (process death drops
    the fd). Drive that with a subprocess shim that grabs the lock
    and exits cleanly; the parent must then be able to acquire.
    """
    import subprocess

    from app.payment.claim_lock import acquire_claim_lock

    settings = _mk_settings(tmp_path / "receipts.db")

    # Drive the child via -c so we don't have to make a top-level
    # picklable target. The child grabs the lock then exits; on exit
    # the OS drops the fd → flock releases.
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, %r);\n"
        "from app.payment.claim_lock import acquire_claim_lock\n"
        "class S: RECEIPT_STORE_PATH=%r\n"
        "with acquire_claim_lock(S()): pass\n"
    ) % (str(repo_root), str(tmp_path / "receipts.db"))
    proc = subprocess.run(
        [sys.executable, "-c", code], timeout=10, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    # Holder is gone; lock must be acquirable in this process.
    with acquire_claim_lock(settings):
        pass


# ── L4: stale-fork reconcile ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_fork_reconcile_reverts_when_nonce_flips_back(tmp_path):
    """The reaper marks a row external at block N. On the next tick,
    block N+1 is current (still inside the finality soak window of 6
    blocks), and ``isNonceUsed`` now returns false. The reaper must
    revert the row to ``CLAIM_TX_TIMEOUT`` so the next tick re-resolves.
    """
    from app.payment.reaper import ClaimReaper

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    # Simulate: reaper just marked this row external at block 100.
    await store.mark_external_with_block([r.request_uuid], block_number=100)

    settings = _mk_settings(db)
    reaper = ClaimReaper(settings=settings)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Mock the chain: current block is 102 (only 2 blocks later, well
    # inside finality=6); isNonceUsed returns False (the original RPC
    # was on a stale fork that has since been resolved).
    with patch("app.payment.reaper.asyncio.to_thread", fake_to_thread), \
         patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        inst.eth.block_number = 102
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.isNonceUsed.return_value.call.return_value = False
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        result = await reaper.tick()

    assert result["finality_checked"] == 1
    assert result["finality_reverted"] == 1

    restored = await store.get_by_uuid(r.request_uuid)
    assert restored.claimed_at is None
    assert restored.claim_tx_hash is None
    assert restored.reconcile_block_number is None
    assert restored.last_error_code == reasons.CLAIM_TX_TIMEOUT
    # Counter NOT incremented — stale-fork isn't the operator's fault.
    assert restored.claim_attempts == 0


@pytest.mark.asyncio
async def test_finalized_external_mark_no_longer_revisited(tmp_path):
    """Once ``current_block - reconcile_block_number >= finality``,
    the external mark is trusted permanently — the recheck pass must
    skip the row entirely (no RPC call, no revert).
    """
    from app.payment.reaper import ClaimReaper

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_external_with_block([r.request_uuid], block_number=100)

    settings = _mk_settings(db)
    reaper = ClaimReaper(settings=settings)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Current block is 200 — way past finality. Even if isNonceUsed
    # were to claim "false" now, the recheck pass shouldn't ask.
    isnonceused_call = MagicMock()
    isnonceused_call.return_value.call.return_value = False

    with patch("app.payment.reaper.asyncio.to_thread", fake_to_thread), \
         patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        inst.eth.block_number = 200
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.isNonceUsed = isnonceused_call
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        result = await reaper.tick()

    assert result["finality_checked"] == 0
    assert result["finality_reverted"] == 0

    # Row stays settled.
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is not None
    assert final.claim_tx_hash == "external"
    assert final.reconcile_block_number == 100


@pytest.mark.asyncio
async def test_external_mark_stays_when_recheck_confirms_used(tmp_path):
    """During the soak window, if the recheck still says ``isNonceUsed``,
    the row stays settled. Belt-and-braces — the happy path can't be
    accidentally subsumed by the revert path.
    """
    from app.payment.reaper import ClaimReaper

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    await store.mark_external_with_block([r.request_uuid], block_number=100)

    settings = _mk_settings(db)
    reaper = ClaimReaper(settings=settings)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("app.payment.reaper.asyncio.to_thread", fake_to_thread), \
         patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        inst.eth.block_number = 103  # still inside soak window
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.isNonceUsed.return_value.call.return_value = True
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        result = await reaper.tick()

    assert result["finality_checked"] == 1
    assert result["finality_reverted"] == 0

    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is not None
    assert final.claim_tx_hash == "external"


# ── L5: in-flight reconciler ───────────────────────────────────────


@pytest.mark.asyncio
async def test_inflight_reconcile_marks_claimed_when_nonce_used(tmp_path):
    """A row with ``claim_tx_pending`` set whose nonce is on-chain gets
    marked claimed using the breadcrumbed hash. Models the post-crash
    daemon-startup scenario for a tx that DID land.
    """
    from app.payment.inflight_reconciler import reconcile_inflight

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    breadcrumb = "0x" + "f" * 64
    await store.set_claim_tx_pending([r.request_uuid], breadcrumb)

    settings = _mk_settings(db)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch(
        "app.payment.inflight_reconciler.asyncio.to_thread", fake_to_thread,
    ), patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.isNonceUsed.return_value.call.return_value = True
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        summary = await reconcile_inflight(settings)

    assert summary == {"checked": 1, "marked_claimed": 1, "cleared": 0}

    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is not None
    assert final.claim_tx_hash == breadcrumb
    assert final.claim_tx_pending is None  # breadcrumb cleared


@pytest.mark.asyncio
async def test_inflight_reconcile_clears_pending_when_nonce_unused(tmp_path):
    """A row with ``claim_tx_pending`` set whose nonce is NOT on-chain
    has its breadcrumb cleared and ``claim_attempts`` reset, so the
    next manual claim picks it up again with a fresh budget.
    """
    from app.payment.inflight_reconciler import reconcile_inflight

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    r = _mk_receipt()
    await store.store(r, signature="0xsig")
    # Simulate prior failed attempt that bumped claim_attempts.
    await store.mark_claim_failed([r.request_uuid], reasons.CLAIM_REVERTED)
    await store.set_claim_tx_pending([r.request_uuid], "0x" + "ab" * 32)
    pre = await store.get_by_uuid(r.request_uuid)
    assert pre.claim_attempts == 1
    assert pre.claim_tx_pending is not None

    settings = _mk_settings(db)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch(
        "app.payment.inflight_reconciler.asyncio.to_thread", fake_to_thread,
    ), patch("web3.Web3") as MockWeb3:
        inst = MockWeb3.return_value
        inst.is_connected.return_value = True
        inst.eth = MagicMock()
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.isNonceUsed.return_value.call.return_value = False
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        summary = await reconcile_inflight(settings)

    assert summary == {"checked": 1, "marked_claimed": 0, "cleared": 1}

    final = await store.get_by_uuid(r.request_uuid)
    assert final.claimed_at is None
    assert final.claim_tx_pending is None
    assert final.claim_attempts == 0  # reset for fresh retry budget
    # mark_claim_failed had stamped the error; clear was scoped to
    # claim_tx_pending + counter only — last_error_code remains the
    # source of truth that explains why the row was last attempted.


# ── MAJ-1c: ambiguous-broadcast preserves breadcrumb ──────────────


@pytest.mark.asyncio
async def test_rpc_unreachable_preserves_claim_tx_pending(tmp_path):
    """When ``send_raw_transaction`` raises a non-funds error, we treat
    the tx as potentially leaked to mempool and PRESERVE the breadcrumb
    so the reaper / inflight reconciler can resolve it via
    ``isNonceUsed`` rather than risk a double-claim. Pre-rc.3 the row
    cleared its breadcrumb here, which let a follow-up retry double-
    submit if the leaked tx eventually mined.
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
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 100_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        # Generic transport error — could mean "tx never reached node"
        # or "node received but response was lost". We can't tell.
        inst.eth.send_raw_transaction.side_effect = ConnectionError(
            "connection reset by peer",
        )

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "ab" * 32
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    assert result.reason_code == reasons.CLAIM_RPC_UNREACHABLE
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claim_tx_pending is not None, (
        "Breadcrumb must survive RPC_UNREACHABLE so the reaper can "
        "verify on-chain status before the row is retried."
    )
    assert final.claim_tx_pending == "0x" + "ab" * 32


@pytest.mark.asyncio
async def test_already_known_falls_through_to_receipt_wait(tmp_path):
    """rc.5 MAJ-1: when ``send_raw_transaction`` raises "already known",
    the deterministic tx hash is already in mempool from a prior attempt
    — DON'T classify as CLAIM_RPC_UNREACHABLE; fall through to
    ``wait_for_transaction_receipt`` and resolve the tx properly.

    Pre-rc.5 a retried claim against a leaked-but-not-yet-mined tx stayed
    stuck on CLAIM_RPC_UNREACHABLE forever, even though the tx was about
    to mine. The fix routes "already known" to the success-in-mempool
    path.
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
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 100_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        # First call: the leaked tx is in mempool from a prior attempt.
        # This is a synchronous reject from send_raw_transaction.
        inst.eth.send_raw_transaction.side_effect = ValueError("already known")

        # Receipt wait succeeds — the tx mines while we're waiting.
        inst.eth.wait_for_transaction_receipt.return_value = MagicMock(
            status=1, gasUsed=42_000,
        )

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "ab" * 32
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    # Treated as success: tx_hash set, no error, no reason_code.
    assert result.error is None or result.error == ""
    assert result.reason_code is None or result.reason_code == ""
    assert result.tx_hash == "0x" + "ab" * 32
    assert result.gas_used == 42_000

    # Receipt is marked claimed and the breadcrumb cleared (success path).
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claim_tx_hash == "0x" + "ab" * 32
    assert final.claim_tx_pending is None


@pytest.mark.asyncio
async def test_already_known_then_revert_still_classified_correctly(tmp_path):
    """If the leaked-into-mempool tx mines and then reverts on-chain,
    we classify as CLAIM_REVERTED — same terminal-failure handling as
    the regular broadcast-then-revert path. The "already known" branch
    only changes the broadcast classification; the post-wait logic is
    unchanged.
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
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 100_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        inst.eth.send_raw_transaction.side_effect = ValueError("AlreadyKnown")
        inst.eth.wait_for_transaction_receipt.return_value = MagicMock(
            status=0, gasUsed=42_000,
        )

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "ab" * 32
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    assert result.reason_code == reasons.CLAIM_REVERTED
    assert result.tx_hash == "0x" + "ab" * 32


def test_is_already_known_error_matches_common_variants():
    """Sniffer must recognise the case-insensitive variants we've seen
    across geth, besu, nethermind, and the JSON-RPC dict-on-args
    wrapping web3.py applies to some errors.
    """
    from app.payment.settlement import _is_already_known_error

    assert _is_already_known_error(ValueError("already known"))
    assert _is_already_known_error(ValueError("Already Known"))
    assert _is_already_known_error(ValueError("ALREADY_KNOWN"))
    assert _is_already_known_error(ValueError("AlreadyKnown"))
    assert _is_already_known_error(ValueError("transaction already exists"))
    assert _is_already_known_error(ValueError("ALREADY_EXISTS"))
    # web3.py dict-on-args wrapping — emulate.
    err = ValueError({"code": -32603, "message": "Already known"})
    assert _is_already_known_error(err)

    # Negative cases.
    assert not _is_already_known_error(ValueError("insufficient funds"))
    assert not _is_already_known_error(ValueError("connection reset by peer"))
    assert not _is_already_known_error(ValueError("nonce too low"))


@pytest.mark.asyncio
async def test_reverted_clears_claim_tx_pending(tmp_path):
    """Sanity: the on-chain-confirmed terminal failure (revert) DOES
    clear the breadcrumb — there's nothing left to reconcile, the tx
    landed and was rejected by the contract.
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
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 100_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        sent_hash = MagicMock()
        sent_hash.hex.return_value = "0x" + "c" * 64
        inst.eth.send_raw_transaction.return_value = sent_hash
        inst.eth.wait_for_transaction_receipt.return_value = MagicMock(
            status=0, gasUsed=21_000,  # status=0 => reverted
        )

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "cc" * 32
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(settings, _GATEWAY_KEY, batch, store)

    assert result.reason_code == reasons.CLAIM_REVERTED
    final = await store.get_by_uuid(r.request_uuid)
    assert final.claim_tx_pending is None, (
        "Reverts are confirmed terminal — breadcrumb is no longer useful."
    )


# ── L6: per-receipt local sig verify ───────────────────────────────


@pytest.mark.asyncio
async def test_local_sig_verify_drops_only_corrupt_receipt_from_batch(tmp_path):
    """One bad signature in a 3-row batch is dropped + locked terminal;
    the other two go through to broadcast. Models the failure mode
    where pre-v1.5 would have reverted the entire ``claimBatch`` and
    burnt ``claim_attempts`` on every row.
    """
    from app.payment.settlement import _submit_batch

    db = tmp_path / "r.db"
    store = get_store(str(db))
    await store.initialize()

    payer_addr = _gateway_address()
    settings = _mk_settings(db, payer=payer_addr)
    domain = _domain_for(settings)

    # Build 3 receipts: r1, r2 signed by the gateway; r3 signed by a
    # different key (corrupt from the contract's POV — sig recovery
    # won't match GATEWAY_PAYER_ADDRESS).
    rs: list[Receipt] = []
    sigs: list[str] = []
    for i in range(2):
        r = _mk_receipt()
        rs.append(r)
        sigs.append(sign_receipt(_GATEWAY_KEY, r, domain))
    bad = _mk_receipt()
    rs.append(bad)
    sigs.append(sign_receipt(_OTHER_KEY, bad, domain))

    for r, sig in zip(rs, sigs):
        await store.store(r, signature=sig)

    batch = []
    for r in rs:
        batch.append(await store.get_by_uuid(r.request_uuid))

    # Stub the chain layer so the kept batch "succeeds".
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
        inst.eth.get_transaction_count.return_value = 0
        contract = MagicMock()
        inst.eth.contract.return_value = contract
        contract.functions.claimBatch.return_value.estimate_gas.return_value = 100_000
        # Mirror build_transaction returning a plain dict — web3 normally
        # does this; the mock just needs to be subscriptable enough.
        contract.functions.claimBatch.return_value.build_transaction.return_value = {
            "from": payer_addr, "nonce": 0, "gas": 100_000,
            "gasPrice": 1, "chainId": settings.ESCROW_CHAIN_ID,
        }
        MockWeb3.to_checksum_address.side_effect = lambda x: x
        MockWeb3.HTTPProvider.return_value = MagicMock()

        # send_raw_transaction returns a bytes-like with a .hex() method.
        sent_hash = MagicMock()
        sent_hash.hex.return_value = "0x" + "1" * 64
        inst.eth.send_raw_transaction.return_value = sent_hash
        inst.eth.wait_for_transaction_receipt.return_value = MagicMock(
            status=1, gasUsed=42_000,
        )

        # Account.from_key().sign_transaction(tx) → object with
        # .raw_transaction and .hash
        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"\x01\x02"
        signed_tx.hash.hex.return_value = "1" * 64
        account_inst = MagicMock()
        account_inst.address = payer_addr
        account_inst.sign_transaction.return_value = signed_tx
        MockAccount.from_key.return_value = account_inst

        result = await _submit_batch(
            settings, _GATEWAY_KEY, batch, store,
        )

    # Two were kept, one dropped.
    assert result.submitted == 2
    assert result.sig_verify_dropped == 1
    assert result.error is None
    assert result.tx_hash is not None

    # The corrupt row is failed_terminal / SIGN_VERIFY_FAILED.
    final_bad = await store.get_by_uuid(bad.request_uuid)
    assert final_bad.locked is True
    assert final_bad.last_error_code == reasons.SIGN_VERIFY_FAILED
    assert final_bad.view == "failed_terminal"

    # The two good rows were broadcast and are claimed.
    for r in rs[:2]:
        final = await store.get_by_uuid(r.request_uuid)
        assert final.claimed_at is not None, (
            f"good receipt {r.request_uuid} should be claimed"
        )
        assert final.claim_tx_hash == "0x" + "1" * 64


@pytest.mark.asyncio
async def test_local_sig_verify_skipped_when_payer_unset(tmp_path):
    """When ``GATEWAY_PAYER_ADDRESS`` is not set, the verification
    falls back to "trust the chain" — every row passes through. Same
    behaviour as pre-v1.5; the L6 hardening only applies when we have
    something to verify against. Defensive guard against breaking
    dev/test setups that haven't synced /config yet.
    """
    from app.payment.settlement import _verify_signatures_locally

    settings = _mk_settings(tmp_path / "r.db", payer="")  # not set
    r = _mk_receipt()
    sig = sign_receipt(_OTHER_KEY, r, _domain_for(settings))
    sr = _StoredLike(r, sig)

    kept, dropped = _verify_signatures_locally(settings, [sr])
    assert len(kept) == 1
    assert dropped == []


@pytest.mark.asyncio
async def test_local_sig_verify_drops_when_recovery_fails(tmp_path):
    """A signature that doesn't even decode (bad hex) gets dropped
    rather than crashing the whole batch. Models a corrupt-on-disk
    case (e.g. partial write of ``signature`` column).
    """
    from app.payment.settlement import _verify_signatures_locally

    settings = _mk_settings(tmp_path / "r.db", payer=_gateway_address())
    r = _mk_receipt()
    sr = _StoredLike(r, signature="0xnot-a-real-signature")

    kept, dropped = _verify_signatures_locally(settings, [sr])
    assert kept == []
    assert dropped == [r.request_uuid]


# ── helpers ────────────────────────────────────────────────────────


class _StoredLike:
    """Minimal stand-in for StoredReceipt for the verify helper.

    The verify function only touches ``.receipt`` and ``.signature``;
    use this rather than constructing a full StoredReceipt + going
    through the DB.
    """

    def __init__(self, receipt: Receipt, signature: str):
        self.receipt = receipt
        self.signature = signature


# ── Schema migration v4 → v5 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_v4_to_v5_migration_adds_columns_and_preserves_rows(tmp_path):
    """A v4 DB with a real row migrates to v5 cleanly:

    - ``reconcile_block_number`` and ``claim_tx_pending`` columns exist.
    - The pre-existing row is unchanged.
    - ``user_version`` lands at the current schema version.
    """
    import sqlite3

    db_path = tmp_path / "r.db"

    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE signed_receipts (
                request_uuid       TEXT PRIMARY KEY,
                tunnel_request_id  TEXT,
                client_address     TEXT NOT NULL,
                node_address       TEXT NOT NULL,
                data_amount        INTEGER NOT NULL,
                total_price        INTEGER NOT NULL,
                signature          TEXT,
                created_at         INTEGER NOT NULL,
                claimed_at         INTEGER,
                claim_tx_hash      TEXT,
                sign_attempts      INTEGER NOT NULL DEFAULT 0,
                claim_attempts     INTEGER NOT NULL DEFAULT 0,
                last_error_code    TEXT,
                last_error_detail  TEXT,
                last_attempt_at    INTEGER,
                locked             INTEGER NOT NULL DEFAULT 0,
                transient_attempts INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.execute(
            "INSERT INTO signed_receipts "
            "(request_uuid, client_address, node_address, data_amount, "
            "total_price, signature, created_at) VALUES "
            "('u1', '0xaaaa', '0xbbbb', 100, 1, '0xsigned', 1111)",
        )
        conn.execute("PRAGMA user_version = 4")

    store = ReceiptStore(db_path)
    await store.initialize()

    from app.payment.receipt_store import _SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        cols = {row[1] for row in conn.execute("PRAGMA table_info(signed_receipts)")}
    assert {"reconcile_block_number", "claim_tx_pending"} <= cols

    row = await store.get_by_uuid("u1")
    assert row is not None
    assert row.signature == "0xsigned"
    assert row.reconcile_block_number is None
    assert row.claim_tx_pending is None


@pytest.mark.asyncio
async def test_partial_v4_to_v5_migration_is_self_healing(tmp_path):
    """Concurrent writer scenario — columns added but user_version
    never bumped. Re-running ``initialize()`` must not crash with
    ``duplicate column name``.
    """
    import sqlite3

    db_path = tmp_path / "r.db"

    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE signed_receipts (
                request_uuid           TEXT PRIMARY KEY,
                tunnel_request_id      TEXT,
                client_address         TEXT NOT NULL,
                node_address           TEXT NOT NULL,
                data_amount            INTEGER NOT NULL,
                total_price            INTEGER NOT NULL,
                signature              TEXT,
                created_at             INTEGER NOT NULL,
                claimed_at             INTEGER,
                claim_tx_hash          TEXT,
                sign_attempts          INTEGER NOT NULL DEFAULT 0,
                claim_attempts         INTEGER NOT NULL DEFAULT 0,
                last_error_code        TEXT,
                last_error_detail      TEXT,
                last_attempt_at        INTEGER,
                locked                 INTEGER NOT NULL DEFAULT 0,
                transient_attempts     INTEGER NOT NULL DEFAULT 0,
                reconcile_block_number INTEGER,
                claim_tx_pending       TEXT
            );
        """)
        conn.execute("PRAGMA user_version = 4")  # stuck pre-bump

    store = ReceiptStore(db_path)
    await store.initialize()  # must not raise

    from app.payment.receipt_store import _SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION


# ── Constant ───────────────────────────────────────────────────────


def test_finality_blocks_constant_is_six():
    """30s soak window on 5s blocks — change here is intentional and
    has to be coordinated with the reaper logic."""
    assert constants.FINALITY_BLOCKS_FOR_RECONCILE == 6
