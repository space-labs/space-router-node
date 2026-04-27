"""CLAIM_INSUFFICIENT_GAS — distinct error code for "wallet has no CTC".

Regression for the test.105 UX bug: every claimBatch broadcast that
failed because the identity wallet was empty got mapped to
CLAIM_RPC_UNREACHABLE, which (a) suggested a transient network issue
to the user when the resolution is operator-actionable funding, and
(b) printed an alarming RPC error in the GUI when the actual root
cause was the auto-derived identity wallet having 0 CTC.

This module pins:
- The new code's classification: claim-side, transient (does NOT count
  against the retry budget so receipts stay queued until funded).
- The detector ``_is_insufficient_funds_error`` matches both common
  shapes the web3 / RPC layers expose ("insufficient funds for gas *
  price + value" plain string and the dict shape ``{'message': '...'}``).
"""

from __future__ import annotations

import pytest

from app.payment import reasons
from app.payment.settlement import _is_insufficient_funds_error


def test_is_claim_code():
    assert reasons.is_claim_code(reasons.CLAIM_INSUFFICIENT_GAS) is True
    assert reasons.is_sign_code(reasons.CLAIM_INSUFFICIENT_GAS) is False


def test_does_not_count_against_retry_budget():
    """Funding a wallet is operator action — receipts must stay
    retryable indefinitely, not get locked at the 2-try cap.
    """
    assert (
        reasons.counts_against_retry_budget(reasons.CLAIM_INSUFFICIENT_GAS)
        is False
    )


def test_message_is_actionable():
    msg = reasons.message_for(reasons.CLAIM_INSUFFICIENT_GAS)
    assert "CTC" in msg
    assert "Earnings" in msg or "wallet" in msg.lower()


@pytest.mark.parametrize("text", [
    "insufficient funds for gas * price + value",
    "Insufficient Funds For Gas * Price + Value",
    "insufficient funds for intrinsic transaction cost",
])
def test_detector_matches_common_phrasing(text):
    assert _is_insufficient_funds_error(Exception(text)) is True


def test_detector_handles_dict_args_shape():
    """web3.py's RPCError sometimes wraps the JSON-RPC error dict as
    its first arg: ``Exception({'code': -32603, 'message': '...'})``.
    """
    err = Exception(
        {"code": -32603, "message": "insufficient funds for gas * price + value"}
    )
    assert _is_insufficient_funds_error(err) is True


def test_detector_does_not_false_positive_on_other_errors():
    assert _is_insufficient_funds_error(Exception("connection refused")) is False
    assert _is_insufficient_funds_error(Exception("timeout")) is False
    assert _is_insufficient_funds_error(Exception("nonce too low")) is False


def test_detector_handles_empty():
    assert _is_insufficient_funds_error(Exception()) is False
    assert _is_insufficient_funds_error(Exception("")) is False
