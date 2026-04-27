"""receipts_retry_all — batched retry replaces per-row retry.

Regression for the test.105 UX bug where clicking the per-row "Retry"
button on each of N failed receipts fired N separate single-receipt
``claimBatch`` txs (instead of ONE batch tx of N receipts).

The new endpoint kicks ``claim_all(include_retryable=True)`` with no
``only_uuids`` filter — settlement.py chunks the queue into
``CLAIM_BATCH_SIZE`` (default 50) groups internally, so one user click
yields one chain tx for the whole queue.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def api(tmp_path):
    from gui.api import Api
    db = tmp_path / "r.db"

    class FakeSettings:
        RECEIPT_STORE_PATH = str(db)
        ESCROW_CHAIN_RPC = "http://fake"
        ESCROW_CONTRACT_ADDRESS = "0x" + "e" * 40
        ESCROW_CHAIN_ID = 102031
        CLAIM_BATCH_SIZE = 50
        IDENTITY_KEY_PATH = str(tmp_path / "id.key")
        IDENTITY_PASSPHRASE = ""

    patcher = patch("app.main.load_settings", return_value=FakeSettings())
    patcher.start()
    yield Api(config=MagicMock(), node_manager=MagicMock())
    patcher.stop()


def test_retry_all_returns_task_id(api):
    """Endpoint kicks a background task and returns ``{ok: True, task_id: …}``."""
    with patch("gui.api._claim_runner") as mock_runner:
        mock_runner.return_value = {"ok": True}
        resp = api.receipts_retry_all()
    assert resp["ok"] is True
    assert "task_id" in resp


def test_retry_all_invokes_claim_runner_with_include_retryable_true(api):
    """The include_retryable flag must be True so failed_retryable rows
    enter the batch — that's the whole point of "Retry all".
    """
    captured_args = []

    def fake_runner(only_uuid, include_retryable):
        captured_args.append((only_uuid, include_retryable))
        return {"ok": True}

    with patch("gui.api._claim_runner", side_effect=fake_runner):
        api.receipts_retry_all()
        # Drain task registry so the lambda runs.
        # _claim_tasks.start() runs the lambda in a thread; wait for it.
        import time
        for _ in range(20):
            if captured_args:
                break
            time.sleep(0.05)

    assert captured_args, "claim_runner was never invoked"
    only_uuid, include_retryable = captured_args[0]
    assert only_uuid is None, "must NOT pass only_uuids — that would un-batch"
    assert include_retryable is True


def test_retry_all_does_not_take_arguments(api):
    """No per-uuid argument — every retry is whole-queue batched."""
    import inspect
    sig = inspect.signature(api.receipts_retry_all)
    # Only `self`; all params must have defaults (none, in this case).
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert all(p.default is not inspect.Parameter.empty for p in params), (
        "receipts_retry_all should not require arguments"
    )
