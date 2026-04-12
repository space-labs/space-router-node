"""Tests for health-loop probe gating, self-probe cooldown, and request_probe return values."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from httpx import Response

from app.config import Settings
from app.registration import request_probe

# Reuse identity fixtures from existing tests
from eth_account import Account

_TEST_IDENTITY = Account.from_key("0x" + "ab" * 32)
TEST_IDENTITY_KEY = _TEST_IDENTITY.key.hex()
TEST_WALLET = "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"
TEST_NODE_ID = "node-test-123"
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


# ---------------------------------------------------------------------------
# request_probe return values
# ---------------------------------------------------------------------------


class TestRequestProbeReturnValues:
    """Verify request_probe returns bool indicating acceptance."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_returns_true_on_200(self, probe_settings):
        respx.post(f"{COORDINATION_URL}/nodes/{TEST_NODE_ID}/request-probe").mock(
            return_value=Response(200, json={"ok": True}),
        )
        async with httpx.AsyncClient() as client:
            result = await request_probe(
                client, probe_settings, TEST_NODE_ID, identity_key=TEST_IDENTITY_KEY,
            )
        assert result is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_returns_true_on_400(self, probe_settings):
        respx.post(f"{COORDINATION_URL}/nodes/{TEST_NODE_ID}/request-probe").mock(
            return_value=Response(400, text="already online"),
        )
        async with httpx.AsyncClient() as client:
            result = await request_probe(
                client, probe_settings, TEST_NODE_ID, identity_key=TEST_IDENTITY_KEY,
            )
        assert result is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_returns_false_on_429(self, probe_settings):
        respx.post(f"{COORDINATION_URL}/nodes/{TEST_NODE_ID}/request-probe").mock(
            return_value=Response(429, text="rate limited"),
        )
        async with httpx.AsyncClient() as client:
            result = await request_probe(
                client, probe_settings, TEST_NODE_ID, identity_key=TEST_IDENTITY_KEY,
            )
        assert result is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_returns_false_on_500(self, probe_settings):
        respx.post(f"{COORDINATION_URL}/nodes/{TEST_NODE_ID}/request-probe").mock(
            return_value=Response(500, text="server error"),
        )
        async with httpx.AsyncClient() as client:
            result = await request_probe(
                client, probe_settings, TEST_NODE_ID, identity_key=TEST_IDENTITY_KEY,
            )
        assert result is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_probe_returns_false_on_exception(self, probe_settings):
        respx.post(f"{COORDINATION_URL}/nodes/{TEST_NODE_ID}/request-probe").mock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        async with httpx.AsyncClient() as client:
            result = await request_probe(
                client, probe_settings, TEST_NODE_ID, identity_key=TEST_IDENTITY_KEY,
            )
        assert result is False


# ---------------------------------------------------------------------------
# Helpers for loop tests
# ---------------------------------------------------------------------------


def _make_ctx(settings):
    """Create a minimal _NodeContext-like object for testing."""
    ctx = MagicMock()
    ctx.s = settings
    ctx.http = AsyncMock()
    ctx.node_id = TEST_NODE_ID
    ctx.identity_key = TEST_IDENTITY_KEY
    ctx.ssl_ctx = None
    # Probe coordination timestamp (shared between _health_loop and _self_probe_loop).
    # Use a real attribute so getattr() with a default works as expected.
    ctx._last_probe_request_time = 0
    return ctx


def _make_sm():
    """Create a minimal NodeStateMachine mock."""
    sm = MagicMock()
    sm.transition = MagicMock()
    sm.set_cert_warning = MagicMock()
    sm.status = MagicMock()
    return sm


# ---------------------------------------------------------------------------
# _health_loop probe interval gating
# ---------------------------------------------------------------------------


class TestHealthLoopProbeInterval:
    """Verify _health_loop gates request_probe calls on _PROBE_REQUEST_INTERVAL."""

    @pytest.mark.asyncio
    async def test_health_loop_respects_probe_interval(self, probe_settings):
        """request_probe should NOT be called when less than _PROBE_REQUEST_INTERVAL elapsed."""
        from app.main import _health_loop, _PROBE_REQUEST_INTERVAL

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            # Cancel the coroutine we received so it doesn't leak
            coro.close()
            if call_count >= 1:
                stop_event.set()
            raise asyncio.TimeoutError()

        # Time returns a value well within the interval (not enough elapsed)
        # last_probe_request starts at 0.0, time returns 1000.0
        # 1000.0 - 0.0 = 1000.0 < 1800 => probe should NOT be called
        current_time = 1000.0

        mock_activity = MagicMock()
        mock_activity.record_health_check = MagicMock()

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", return_value=current_time), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "online", "health_score": 1.0}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe, \
             patch("app.tls.check_certificate_expiry", return_value=None), \
             patch("app.node_logging.activity", mock_activity):

            await _health_loop(ctx, sm, stop_event)

            assert mock_probe.call_count == 0

    @pytest.mark.asyncio
    async def test_health_loop_calls_probe_after_interval(self, probe_settings):
        """request_probe SHOULD be called once _PROBE_REQUEST_INTERVAL has elapsed."""
        from app.main import _health_loop, _PROBE_REQUEST_INTERVAL

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            coro.close()
            if call_count >= 1:
                stop_event.set()
            raise asyncio.TimeoutError()

        # last_probe_request is initialized to time.time() (first call),
        # so subsequent calls must be >= init + 1800 for the probe to fire.
        init_time = 100.0
        time_seq = iter([init_time, init_time + 1800, init_time + 1800])

        mock_activity = MagicMock()
        mock_activity.record_health_check = MagicMock()

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", side_effect=time_seq), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "online", "health_score": 1.0}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe, \
             patch("app.tls.check_certificate_expiry", return_value=None), \
             patch("app.node_logging.activity", mock_activity):

            await _health_loop(ctx, sm, stop_event)

            assert mock_probe.call_count == 1


# ---------------------------------------------------------------------------
# _self_probe_loop cooldown
# ---------------------------------------------------------------------------


class TestSelfProbeLoopCooldown:
    """Verify _self_probe_loop gates request_probe on _SELF_PROBE_REQUEST_COOLDOWN."""

    @pytest.mark.asyncio
    async def test_self_probe_loop_requests_probe_when_offline(self, probe_settings):
        """When status is 'offline' and cooldown has elapsed, request_probe is called."""
        from app.main import _self_probe_loop, _SELF_PROBE_REQUEST_COOLDOWN

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            coro.close()
            # Let first_run pass (delay=5), then one real iteration, then stop
            if call_count >= 2:
                stop_event.set()
            raise asyncio.TimeoutError()

        # last_probe_request_time is initialized to time.time() (first call),
        # so subsequent calls must be >= init + 300 for the probe to fire.
        init_time = 100.0
        _time_call = [0]
        def _fake_time():
            _time_call[0] += 1
            return init_time if _time_call[0] == 1 else init_time + 300

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", side_effect=_fake_time), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.1, "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe:

            await _self_probe_loop(ctx, sm, stop_event)

            assert mock_probe.call_count == 1

    @pytest.mark.asyncio
    async def test_self_probe_loop_cooldown_prevents_rapid_requests(self, probe_settings):
        """When status is 'offline' but cooldown hasn't elapsed, request_probe is NOT called."""
        from app.main import _self_probe_loop, _SELF_PROBE_REQUEST_COOLDOWN

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        iteration = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal iteration
            iteration += 1
            coro.close()
            if iteration >= 3:
                stop_event.set()
            raise asyncio.TimeoutError()

        # Time is 100.0 — cooldown is 300s
        # last_probe_request_time starts at 0.0
        # 100.0 - 0.0 = 100 < 300 => probe should NOT be called
        current_time = 100.0

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", return_value=current_time), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.1, "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe:

            await _self_probe_loop(ctx, sm, stop_event)

            assert mock_probe.call_count == 0

    @pytest.mark.asyncio
    async def test_self_probe_loop_skips_probe_when_online(self, probe_settings):
        """When status is 'online', no probe is requested regardless of cooldown."""
        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            coro.close()
            if call_count >= 2:
                stop_event.set()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", return_value=9999.0), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "online", "health_score": 1.0, "staking_status": "earning"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe:

            await _self_probe_loop(ctx, sm, stop_event)

            assert mock_probe.call_count == 0


# ---------------------------------------------------------------------------
# _self_probe_loop backoff on 429
# ---------------------------------------------------------------------------


class TestSelfProbeLoopBackoff:
    """Verify _self_probe_loop applies exponential backoff on rate-limited responses."""

    @pytest.mark.asyncio
    async def test_backoff_on_rejected_probe(self, probe_settings):
        """When request_probe returns False (429), probe_result should be 'rate_limited'."""
        from app.main import _self_probe_loop

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()
        dashboard = MagicMock()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            coro.close()
            # Stop after first real iteration (first_run delay + one check)
            if call_count >= 2:
                stop_event.set()
            raise asyncio.TimeoutError()

        init_time = 100.0
        _time_call = [0]
        def _fake_time():
            _time_call[0] += 1
            return init_time if _time_call[0] == 1 else init_time + 300

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", side_effect=_fake_time), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "offline", "health_score": 0.1, "staking_status": "qualifying"}), \
             patch("app.registration.request_probe", new_callable=AsyncMock, return_value=False):

            await _self_probe_loop(ctx, sm, stop_event, dashboard)

            # The first iteration's dashboard update should show "rate_limited"
            # (second iteration sees doubled cooldown and reports "cooldown")
            probe_results = [
                call.kwargs["last_probe_result"]
                for call in dashboard.update.call_args_list
                if "last_probe_result" in call.kwargs
            ]
            assert "rate_limited" in probe_results


# ---------------------------------------------------------------------------
# _health_loop probe coordination
# ---------------------------------------------------------------------------


class TestHealthLoopProbeCoordination:
    """Verify _health_loop skips probe if _self_probe_loop recently requested one."""

    @pytest.mark.asyncio
    async def test_health_loop_skips_probe_when_self_probe_recent(self, probe_settings):
        """request_probe should NOT be called if ctx._last_probe_request_time is recent."""
        from app.main import _health_loop, _PROBE_REQUEST_INTERVAL

        ctx = _make_ctx(probe_settings)
        sm = _make_sm()
        stop_event = asyncio.Event()

        call_count = 0

        async def _fake_wait_for(coro, *, timeout):
            nonlocal call_count
            call_count += 1
            coro.close()
            if call_count >= 1:
                stop_event.set()
            raise asyncio.TimeoutError()

        # Set up time so _PROBE_REQUEST_INTERVAL has elapsed...
        init_time = 100.0
        check_time = init_time + _PROBE_REQUEST_INTERVAL
        # ...but self-probe loop recently requested a probe
        ctx._last_probe_request_time = check_time - 60  # 60s ago (within cooldown)

        mock_activity = MagicMock()
        mock_activity.record_health_check = MagicMock()

        with patch("asyncio.wait_for", side_effect=_fake_wait_for), \
             patch("time.time", side_effect=iter([init_time, check_time, check_time])), \
             patch("app.registration.check_node_status", new_callable=AsyncMock,
                   return_value={"status": "online", "health_score": 1.0}), \
             patch("app.registration.request_probe", new_callable=AsyncMock) as mock_probe, \
             patch("app.tls.check_certificate_expiry", return_value=None), \
             patch("app.node_logging.activity", mock_activity):

            await _health_loop(ctx, sm, stop_event)

            # Probe should NOT be called because self-probe is recent
            assert mock_probe.call_count == 0
