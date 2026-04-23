"""UPnP renewal loop — backoff + escalation behaviour.

v1.5 QA saw "ENDPOINT_UNREACHABLE at 1h15m" with a default 3600s
lease. Root cause: the old loop only retried at half-lease (1800s)
cadence, so a single transient failure left the mapping expired for
up to a full long interval. The rewritten loop drops to a 60s retry
on failure and escalates log tone after the short window expires.
Lock both properties down so they don't regress silently.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.main import _upnp_renewal_loop


async def _run_loop_with_ticks(
    results: list[bool],
    *,
    long_interval: int = 1,
    short_interval: int = 1,
    escalate_after: int = 3,
    caplog: pytest.LogCaptureFixture | None = None,
) -> tuple[list[float], list[str]]:
    """Drive the loop through ``len(results)`` iterations and capture
    the sleep intervals + log messages it emitted. Uses a monkey-patched
    ``asyncio.sleep`` that records the requested interval without
    actually sleeping, so the test is fast and deterministic."""
    intervals_observed: list[float] = []

    original_sleep = asyncio.sleep

    iteration = {"n": 0}

    async def fake_sleep(delay: float) -> None:
        intervals_observed.append(delay)
        iteration["n"] += 1
        if iteration["n"] > len(results):
            raise _StopLoop()
        # Yield so the event loop can interleave.
        await original_sleep(0)

    async def renew_fn() -> bool:
        idx = iteration["n"] - 1
        return results[idx] if 0 <= idx < len(results) else True

    # Patch
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        try:
            await _upnp_renewal_loop(
                renew_fn,
                long_interval=long_interval,
                short_interval=short_interval,
                escalate_after=escalate_after,
            )
        except _StopLoop:
            pass
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    messages = [
        rec.getMessage() for rec in (caplog.records if caplog is not None else [])
    ]
    return intervals_observed, messages


class _StopLoop(Exception):
    """Private sentinel to break the infinite loop in tests."""


@pytest.mark.asyncio
async def test_success_keeps_long_interval():
    """Three successful renewals in a row should all use the long interval."""
    intervals, _ = await _run_loop_with_ticks(
        [True, True, True], long_interval=1800, short_interval=60,
    )
    assert intervals[:3] == [1800, 1800, 1800]


@pytest.mark.asyncio
async def test_failure_shrinks_to_short_interval():
    """After a single failure the next wake must be ``short_interval``,
    not the original long interval. This is the fix for the 1h15m
    ENDPOINT_UNREACHABLE symptom — previously the code slept another
    full long interval after a failure."""
    intervals, _ = await _run_loop_with_ticks(
        [False, True, True], long_interval=1800, short_interval=60,
    )
    # First call: long (1800), fails.
    # Second call: short (60), succeeds.
    # Third call: back to long (1800).
    assert intervals[:3] == [1800, 60, 1800]


@pytest.mark.asyncio
async def test_repeated_failures_stay_on_short_interval():
    intervals, _ = await _run_loop_with_ticks(
        [False, False, False, False], long_interval=1800, short_interval=60,
    )
    # First call long, every retry after short.
    assert intervals[0] == 1800
    assert intervals[1] == 60
    assert intervals[2] == 60
    assert intervals[3] == 60


@pytest.mark.asyncio
async def test_escalates_log_level_after_threshold(caplog):
    caplog.set_level(logging.DEBUG)
    _intervals, _messages = await _run_loop_with_ticks(
        [False, False, False], long_interval=1800, short_interval=60,
        escalate_after=3,
    )
    # After three consecutive failures the logger must emit an ERROR —
    # operators need a distinct signal that the renewal has gone
    # past the normal grace window.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "UPnP lease renewal has failed" in r.getMessage() for r in error_records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_recovery_logs_info(caplog):
    caplog.set_level(logging.INFO)
    await _run_loop_with_ticks(
        [False, True], long_interval=1800, short_interval=60,
    )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "UPnP lease recovered" in r.getMessage() for r in info_records
    ), [r.getMessage() for r in caplog.records]
