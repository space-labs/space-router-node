"""Tests for BUG-132-01: transient post-register "inactive" suppression.

A rapid node restart re-registers; coord marks it offline and its first probe
fails, so coord briefly returns ``staking_status == "inactive"``. The self-probe
loop must not flash that transient "inactive" red — it keeps the "—" sentinel
(GUI "Initializing…") for a short grace after (re)registration. A *persistent*
"inactive" past the grace still writes through so a genuinely offline/draining
node goes red (BUG-05a preserved).

These exercise the pure grace decision and the real register-time stamping with
a REAL ``NodeStateMachine`` (no mocking of the decision logic).
"""

import time as _time
from unittest.mock import patch

from app.state import NodeStateMachine, NodeStatus


# ---------------------------------------------------------------------------
# Sentinel agreement — the value the helper keeps must equal the NodeStatus
# default, so a suppressed "inactive" is indistinguishable from a fresh status.
# ---------------------------------------------------------------------------


def test_sentinel_matches_nodestatus_default():
    from app.main import _STAKING_STATUS_SENTINEL

    # Default on a freshly-constructed status object …
    assert _STAKING_STATUS_SENTINEL == NodeStatus().staking_status
    # … and on a real state machine's status (the GUI renders "—" as
    # "Initializing…" while running).
    assert _STAKING_STATUS_SENTINEL == NodeStateMachine().status.staking_status
    assert _STAKING_STATUS_SENTINEL == "—"


# ---------------------------------------------------------------------------
# Pure grace decision: _resolve_staking_status_after_register
# ---------------------------------------------------------------------------


class TestResolveStakingStatusAfterRegister:
    def test_inactive_within_grace_keeps_sentinel(self):
        from app.main import (
            _STAKING_STATUS_SENTINEL,
            _resolve_staking_status_after_register,
        )

        now = 1000.0
        grace_until = now + 60  # still inside the window
        result = _resolve_staking_status_after_register(
            "inactive", grace_until, now=now,
        )
        assert result == _STAKING_STATUS_SENTINEL

    def test_inactive_past_grace_writes_through(self):
        from app.main import _resolve_staking_status_after_register

        now = 1000.0
        grace_until = now - 1  # window already elapsed
        result = _resolve_staking_status_after_register(
            "inactive", grace_until, now=now,
        )
        # Persistent inactive past the grace → real value (BUG-05a preserved).
        assert result == "inactive"

    def test_inactive_at_exact_deadline_writes_through(self):
        from app.main import _resolve_staking_status_after_register

        now = 1000.0
        # Boundary: now == grace_until is NOT "within" (strict <).
        result = _resolve_staking_status_after_register(
            "inactive", grace_until=now, now=now,
        )
        assert result == "inactive"

    def test_no_grace_stamp_writes_through(self):
        from app.main import _resolve_staking_status_after_register

        # grace_until None ⇒ no active grace (e.g. probe before any register).
        result = _resolve_staking_status_after_register(
            "inactive", None, now=1000.0,
        )
        assert result == "inactive"

    def test_non_inactive_within_grace_writes_through_immediately(self):
        from app.main import _resolve_staking_status_after_register

        now = 1000.0
        grace_until = now + 60  # inside the window
        # A real status during the grace must NOT be suppressed.
        for status in ("earning", "qualifying", "online", "—"):
            assert (
                _resolve_staking_status_after_register(status, grace_until, now=now)
                == status
            )


# ---------------------------------------------------------------------------
# Register-time stamping with a REAL NodeStateMachine
# ---------------------------------------------------------------------------


class TestResetStakingStatusStampsGrace:
    def _make_ctx_with_sm(self):
        """A minimal real-ish context: real NodeStateMachine on ``sm`` so
        attribute writes stick, mirroring the production wiring in ``_run``."""
        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.sm = NodeStateMachine()
        return ctx

    def test_reset_blanks_status_and_stamps_grace(self):
        from app.main import (
            _STAKING_INACTIVE_GRACE_SECONDS,
            _STAKING_STATUS_SENTINEL,
            _reset_staking_status,
        )

        ctx = self._make_ctx_with_sm()
        # Simulate a stale value the prior session left behind.
        ctx.sm.status.staking_status = "earning"

        before = _time.time()
        _reset_staking_status(ctx)
        after = _time.time()

        # Status blanked to the sentinel …
        assert ctx.sm.status.staking_status == _STAKING_STATUS_SENTINEL
        # … and a grace deadline stamped ≈ now + grace window.
        assert ctx._staking_grace_until is not None
        assert before + _STAKING_INACTIVE_GRACE_SECONDS <= ctx._staking_grace_until
        assert ctx._staking_grace_until <= after + _STAKING_INACTIVE_GRACE_SECONDS

    def test_reset_without_sm_still_stamps_grace(self):
        """Partial context (no ``sm``) must not raise and must still stamp the
        grace deadline — matches the defensive ``getattr(ctx, "sm", None)``
        pattern a prior fix needed to avoid AttributeError."""
        from app.main import _reset_staking_status

        class _Ctx:
            pass

        ctx = _Ctx()  # no .sm attribute at all
        _reset_staking_status(ctx)  # must not raise
        assert ctx._staking_grace_until is not None


# ---------------------------------------------------------------------------
# End-to-end of the pure decision against a real register stamp: a transient
# inactive right after register is suppressed; the same inactive after the
# window writes through.
# ---------------------------------------------------------------------------


class TestRegisterThenResolveEndToEnd:
    def test_transient_then_persistent(self):
        from app.main import (
            _STAKING_INACTIVE_GRACE_SECONDS,
            _STAKING_STATUS_SENTINEL,
            _reset_staking_status,
            _resolve_staking_status_after_register,
        )

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.sm = NodeStateMachine()

        register_t = 5000.0
        # Stamp the grace deadline at a fixed register time. ``_reset_staking_
        # status`` calls ``time.time()`` (module-cached), so patching it pins
        # the deadline deterministically — same idiom as test_self_probe_loop.
        with patch("time.time", return_value=register_t):
            _reset_staking_status(ctx)

        grace_until = ctx._staking_grace_until
        assert grace_until == register_t + _STAKING_INACTIVE_GRACE_SECONDS

        # 5s after register (rapid-restart flash): coord says inactive → keep "—".
        assert (
            _resolve_staking_status_after_register(
                "inactive", grace_until, now=register_t + 5,
            )
            == _STAKING_STATUS_SENTINEL
        )

        # Well past the grace: still inactive → genuinely offline → write through.
        assert (
            _resolve_staking_status_after_register(
                "inactive", grace_until,
                now=register_t + _STAKING_INACTIVE_GRACE_SECONDS + 30,
            )
            == "inactive"
        )
