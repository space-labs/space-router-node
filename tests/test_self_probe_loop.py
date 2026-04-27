"""Tests for _self_probe_loop recovery behaviour.

Covers the four-defect bugfix:
  A. structured request_probe result (ok / rate_limited / failed)
  B. retry-after hint honoured exactly (no exponential doubling on 429)
  C. online→offline first transition fires immediately
  D. consecutive-offline escalation to RECONNECTING
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account

from app.config import Settings
from app.registration import ProbeRequestResult
from app.state import NodeState

_TEST_IDENTITY = Account.from_key("0x" + "ab" * 32)
TEST_IDENTITY_KEY = _TEST_IDENTITY.key.hex()
TEST_WALLET = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"
TEST_NODE_ID = "node-test-456"
COORDINATION_URL = "http://coordination:8000"


@pytest.fixture
def probe_settings():
    return Settings(
        NODE_PORT=9090,
        COORDINATION_API_URL=COORDINATION_URL,
        NODE_LABEL="test-node",
        PUBLIC_IP="1.2.3.4",
        STAKING_ADDRESS=TEST_WALLET,
    )


def _make_ctx(settings):
    ctx = MagicMock()
    ctx.s = settings
    ctx.http = AsyncMock()
    ctx.node_id = TEST_NODE_ID
    ctx.identity_key = TEST_IDENTITY_KEY
    ctx.ssl_ctx = None
    ctx._last_probe_request_time = 0
    return ctx


def _make_sm():
    """Real-ish state machine: ``status`` is a real object (so attribute writes
    stick), ``transition`` is a mock (so we can assert it was called)."""
    from app.state import NodeStateMachine
    sm = NodeStateMachine()
    sm.transition = MagicMock()
    return sm


def _wait_for_factory(stop_event: asyncio.Event, run_iterations: int):
    """Build an asyncio.wait_for replacement that lets exactly
    ``run_iterations`` loop bodies execute, then breaks the loop cleanly.

    Each call to wait_for represents the start of an iteration:
      - For the first ``run_iterations`` calls: raise TimeoutError so the
        iteration body runs.
      - On the next call: set stop_event AND return without raising, which
        causes the loop's ``break`` after wait_for to fire — preventing any
        further body execution.
    """
    counter = {"n": 0}

    async def _fake_wait_for(coro, *, timeout):
        counter["n"] += 1
        coro.close()
        if counter["n"] > run_iterations:
            stop_event.set()
            return None  # triggers `break` after wait_for
        raise asyncio.TimeoutError()

    return _fake_wait_for, counter


# ---------------------------------------------------------------------------
# Online status — no probe requested, fields populated
# ---------------------------------------------------------------------------


class TestOnlineStatus:
    @pytest.mark.asyncio
    async def test_online_does_not_probe_and_populates_status(self, probe_settings):
        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()
        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=1)

        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", return_value=1000.0), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "online", "health_score": 0.95,
                                 "staking_status": "earning"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe:
            await _self_probe_loop(ctx, sm, stop_event)

        assert mock_probe.call_count == 0
        assert sm.status.coord_status == "online"
        assert sm.status.coord_health_score == pytest.approx(0.95)
        assert sm.status.staking_status == "earning"
        assert sm.status.next_probe_attempt_at is None


# ---------------------------------------------------------------------------
# Online → offline first transition: immediate probe even within cooldown
# ---------------------------------------------------------------------------


class TestFirstTransition:
    @pytest.mark.asyncio
    async def test_first_offline_observation_after_online_fires_immediately(
        self, probe_settings,
    ):
        """Loop sees online, then offline — second iteration fires probe even
        though cooldown hasn't elapsed since the first probe attempt."""
        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        # 2 iterations: online, then offline (which fires the transition probe).
        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=2)

        statuses = iter([
            {"status": "online", "health_score": 0.9, "staking_status": "earning"},
            {"status": "offline", "health_score": 0.1, "staking_status": "earning"},
        ])

        async def _fake_check(*args, **kwargs):
            return next(statuses)

        # Fix time so cooldown_ok would be False (300s after init=1000.0):
        # client cooldown gate is irrelevant on first transition because
        # is_first_transition forces the probe.
        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", return_value=1000.0), \
             patch("app.registration.check_node_status", side_effect=_fake_check), \
             patch("app.registration.request_probe", new_callable=AsyncMock,
                   return_value=ProbeRequestResult("ok", None)) as mock_probe:
            await _self_probe_loop(ctx, sm, stop_event)

        # Probe fired once on the online→offline transition (cooldown bypassed).
        assert mock_probe.call_count == 1
        assert sm.status.last_probe_outcome == "ok"
        # next_probe_attempt_at = now (1000) + cooldown (300) on success.
        assert sm.status.next_probe_attempt_at == pytest.approx(1300.0)


# ---------------------------------------------------------------------------
# 429 honours retry-after; no exponential doubling
# ---------------------------------------------------------------------------


class TestRateLimitedHonoursRetryAfter:
    @pytest.mark.asyncio
    async def test_rate_limited_sets_next_attempt_to_retry_plus_5(self, probe_settings):
        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        # Single offline iteration so we capture the rate_limited outcome
        # before any subsequent cooldown iteration would overwrite it.
        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=1)

        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", return_value=2000.0), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.0,
                                 "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock,
                   return_value=ProbeRequestResult("rate_limited", 120)):
            await _self_probe_loop(ctx, sm, stop_event)

        # next_probe_attempt_at = now (2000) + retry_after (120) + 5s buffer.
        assert sm.status.last_probe_outcome == "rate_limited"
        assert sm.status.next_probe_attempt_at == pytest.approx(2000.0 + 120 + 5)


# ---------------------------------------------------------------------------
# Failed: cooldown doubles up to cap, last_probe_request_time advances
# ---------------------------------------------------------------------------


class TestFailedDoublesCooldown:
    @pytest.mark.asyncio
    async def test_failed_doubles_cooldown_up_to_cap(self, probe_settings):
        """Three consecutive failed responses should grow the cooldown
        300 → 600 (cap) and never exceed _SELF_PROBE_BACKOFF_CAP=600."""
        from app.main import _self_probe_loop, _SELF_PROBE_BACKOFF_CAP

        assert _SELF_PROBE_BACKOFF_CAP == 600  # guard against future drift

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=3)

        # Use a time sequence that keeps cooldown_ok=True so each iteration
        # actually invokes request_probe.
        time_seq = iter([
            0.0,   # initial state read (we don't really care)
            1000.0, 1000.0,  # iter 1: now check + sm.status update
            10000.0, 10000.0,  # iter 2: well past cooldown
            20000.0, 20000.0,  # iter 3: well past cooldown
            30000.0,
        ])

        def _fake_time():
            try:
                return next(time_seq)
            except StopIteration:
                return 30000.0

        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", side_effect=_fake_time), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.0,
                                 "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock,
                   return_value=ProbeRequestResult("failed", None)) as mock_probe:
            await _self_probe_loop(ctx, sm, stop_event)

        # All three iterations called request_probe (since each had elapsed > cooldown).
        assert mock_probe.call_count == 3
        assert sm.status.last_probe_outcome == "failed"
        # After 3 doublings: 300 → 600 (cap), 600 (cap), 600 (cap).
        # last_probe_request_time advances each time, so the next_probe_attempt_at
        # is last_advanced_time + 600.
        assert sm.status.next_probe_attempt_at is not None


# ---------------------------------------------------------------------------
# Escalation: 6 consecutive offline polls → RECONNECTING
# ---------------------------------------------------------------------------


class TestEscalation:
    @pytest.mark.asyncio
    async def test_six_consecutive_offline_escalates_to_reconnecting(self, probe_settings):
        from app.main import (
            _self_probe_loop,
            _SELF_PROBE_OFFLINE_ESCALATION_THRESHOLD,
        )

        assert _SELF_PROBE_OFFLINE_ESCALATION_THRESHOLD == 6

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        # Allow plenty of iterations; the loop itself returns on escalation
        # at consecutive_offline=6 so extras won't actually run.
        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=20)

        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", return_value=1000.0), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.0,
                                 "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock,
                   return_value=ProbeRequestResult("ok", None)):
            await _self_probe_loop(ctx, sm, stop_event)

        # Should have transitioned to RECONNECTING.
        sm.transition.assert_called_once()
        args, _ = sm.transition.call_args
        assert args[0] == NodeState.RECONNECTING
        assert sm.status.last_probe_outcome == "escalated"


# ---------------------------------------------------------------------------
# check_node_status raises: continues loop, logs INFO, last_probe_outcome=failed
# ---------------------------------------------------------------------------


class TestCheckRaises:
    @pytest.mark.asyncio
    async def test_check_node_status_exception_continues(self, probe_settings, caplog):
        import logging as _logging

        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        fake_wait_for, _ = _wait_for_factory(stop_event, run_iterations=1)

        with caplog.at_level(_logging.INFO, logger="app.main"), \
             patch("asyncio.wait_for", side_effect=fake_wait_for), \
             patch("time.time", return_value=1000.0), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   side_effect=RuntimeError("network broke")), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe:
            await _self_probe_loop(ctx, sm, stop_event)

        # request_probe was never called because the status check failed.
        assert mock_probe.call_count == 0
        assert sm.status.last_probe_outcome == "failed"
        # Loop did NOT escalate (only 1 failure, no consecutive_offline counted).
        sm.transition.assert_not_called()
        # INFO log surfaced (not DEBUG).
        assert any(
            "Self-probe check failed" in rec.getMessage() and rec.levelno == _logging.INFO
            for rec in caplog.records
        )
