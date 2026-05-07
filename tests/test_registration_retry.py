"""rc.8 #6 — bounded retry on ENDPOINT_UNREACHABLE for first-launch register.

Repro: full GUI quit → CLI launched immediately → coord probes endpoint while
UPnP teardown is still in flight (5-15s) → connection_refused →
ENDPOINT_UNREACHABLE 422 on attempt #1, but a few seconds later it succeeds.

The fix retries up to 3 total attempts spaced 5s apart, but ONLY on the very
first registration of this process. Runtime re-registrations (the reconnect
loop) keep their own backoff and must not gain a free retry on every reentry.

Other classified codes (REGISTRATION_REJECTED, IP_CONFLICT, version_too_old,
etc.) propagate immediately so the operator sees the real fault.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.main as main_mod
from app.errors import NodeError, NodeErrorCode


class _FakeSettings:
    """Minimal stand-in for app.config.Settings with the attrs _phase_register
    forwards into register_node().  Avoids depending on pydantic + envvars."""

    COORDINATION_API_URL = "http://coordination:8000"
    REGISTRATION_MODE = "auto"
    PAYMENT_ENABLED = False
    NODE_RATE_PER_GB = 0
    MTLS_ENABLED = False
    GATEWAY_CA_CERT_PATH = "/tmp/gw-ca.crt"


def _make_ctx() -> main_mod._NodeContext:
    """Build a _NodeContext with only the fields _phase_register reads."""
    ctx = main_mod._NodeContext.__new__(main_mod._NodeContext)
    ctx.s = _FakeSettings()
    ctx.http = None  # register_node is mocked, so the client is never used
    ctx.public_ip = "1.2.3.4"
    ctx.upnp_endpoint = None
    ctx.identity_key = "0x" + "ab" * 32
    ctx.identity_address = ""
    ctx.staking_address = "0x" + "11" * 20
    ctx.collection_address = ""
    ctx.wallet_address = "0x" + "11" * 20
    ctx.ssl_ctx = None
    ctx.server = None
    ctx.node_id = ""
    ctx.gateway_ca_cert = None
    return ctx


@pytest.fixture(autouse=True)
def _reset_first_launch_flag():
    """Each test must start with the process-level flag cleared so we exercise
    the first-launch retry path. Restore the original after."""
    saved = main_mod._first_register_attempted
    main_mod._first_register_attempted = False
    yield
    main_mod._first_register_attempted = saved


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The retry sleeps 5s between attempts; collapse it to 0 so the test
    suite stays fast. Still proves we awaited it the right number of times."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main_mod.asyncio, "sleep", _fake_sleep)
    return sleeps


def _endpoint_unreachable_error() -> NodeError:
    """Build the same NodeError that errors.classify_error() emits for a
    422 'Endpoint verification failed: connection_refused' response."""
    return NodeError(
        NodeErrorCode.ENDPOINT_UNREACHABLE,
        "Endpoint verification failed: connection_refused",
    )


class TestFirstLaunchRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_third_attempt(self, monkeypatch, _no_real_sleep):
        """First two attempts fail with ENDPOINT_UNREACHABLE, third succeeds."""
        calls = {"n": 0}

        async def _fake_register(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _endpoint_unreachable_error()
            return ("node-id-final", None)

        monkeypatch.setattr(main_mod, "_phase_register", main_mod._phase_register)
        monkeypatch.setattr("app.registration.register_node", _fake_register)
        monkeypatch.setattr("app.registration.save_gateway_ca_cert", lambda *_a, **_kw: None)

        ctx = _make_ctx()
        await main_mod._phase_register(ctx)

        assert calls["n"] == 3
        assert ctx.node_id == "node-id-final"
        # Two sleeps between three attempts — both at the configured 5s.
        assert _no_real_sleep == [5, 5]

    @pytest.mark.asyncio
    async def test_propagates_after_three_unreachable_attempts(
        self, monkeypatch, _no_real_sleep,
    ):
        """All 3 attempts fail with ENDPOINT_UNREACHABLE → error surfaces."""
        calls = {"n": 0}

        async def _fake_register(*args, **kwargs):
            calls["n"] += 1
            raise _endpoint_unreachable_error()

        monkeypatch.setattr("app.registration.register_node", _fake_register)
        monkeypatch.setattr("app.registration.save_gateway_ca_cert", lambda *_a, **_kw: None)

        ctx = _make_ctx()
        with pytest.raises(NodeError) as exc_info:
            await main_mod._phase_register(ctx)

        assert exc_info.value.code is NodeErrorCode.ENDPOINT_UNREACHABLE
        assert calls["n"] == 3
        # No sleep after the final attempt — only between attempts 1→2 and 2→3.
        assert _no_real_sleep == [5, 5]

    @pytest.mark.asyncio
    async def test_no_retry_on_registration_rejected(
        self, monkeypatch, _no_real_sleep,
    ):
        """REGISTRATION_REJECTED must propagate on the first call without any
        retry — only ENDPOINT_UNREACHABLE gets the bounded-retry treatment."""
        calls = {"n": 0}

        async def _fake_register(*args, **kwargs):
            calls["n"] += 1
            raise NodeError(
                NodeErrorCode.REGISTRATION_REJECTED,
                "HTTP 422: validation failed",
            )

        monkeypatch.setattr("app.registration.register_node", _fake_register)
        monkeypatch.setattr("app.registration.save_gateway_ca_cert", lambda *_a, **_kw: None)

        ctx = _make_ctx()
        with pytest.raises(NodeError) as exc_info:
            await main_mod._phase_register(ctx)

        assert exc_info.value.code is NodeErrorCode.REGISTRATION_REJECTED
        assert calls["n"] == 1, "REGISTRATION_REJECTED must not retry"
        assert _no_real_sleep == [], "no sleep on non-retryable error"

    @pytest.mark.asyncio
    async def test_no_retry_on_runtime_reregistration(
        self, monkeypatch, _no_real_sleep,
    ):
        """Once the first launch has already attempted register, subsequent
        re-registrations (the reconnect loop) must NOT retry on
        ENDPOINT_UNREACHABLE — that path has its own outer backoff and
        compounding silent retries here would mask real failures."""
        # Simulate that first-launch register has already happened.
        main_mod._first_register_attempted = True

        calls = {"n": 0}

        async def _fake_register(*args, **kwargs):
            calls["n"] += 1
            raise _endpoint_unreachable_error()

        monkeypatch.setattr("app.registration.register_node", _fake_register)
        monkeypatch.setattr("app.registration.save_gateway_ca_cert", lambda *_a, **_kw: None)

        ctx = _make_ctx()
        with pytest.raises(NodeError) as exc_info:
            await main_mod._phase_register(ctx)

        assert exc_info.value.code is NodeErrorCode.ENDPOINT_UNREACHABLE
        assert calls["n"] == 1, "runtime re-register must not retry"
        assert _no_real_sleep == []

    @pytest.mark.asyncio
    async def test_classifies_raw_httpx_endpoint_unreachable(
        self, monkeypatch, _no_real_sleep,
    ):
        """register_node() may raise a raw httpx.HTTPStatusError before the
        outer phase wrapper classifies it. The retry loop must classify
        unrecognised exceptions itself so this real-world path still retries."""
        calls = {"n": 0}

        def _make_422():
            req = httpx.Request("POST", "http://coordination:8000/nodes/register")
            return httpx.HTTPStatusError(
                "422",
                request=req,
                response=httpx.Response(
                    422,
                    json={"detail": "Endpoint verification failed: connection_refused"},
                    request=req,
                ),
            )

        async def _fake_register(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _make_422()
            return ("node-classified", None)

        monkeypatch.setattr("app.registration.register_node", _fake_register)
        monkeypatch.setattr("app.registration.save_gateway_ca_cert", lambda *_a, **_kw: None)

        ctx = _make_ctx()
        await main_mod._phase_register(ctx)

        assert calls["n"] == 3
        assert ctx.node_id == "node-classified"
