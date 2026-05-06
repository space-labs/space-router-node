"""rc.7 MIN-3 — gate the CLI staking-required prompt by ``staking_status``.

Pre-rc.7, every CLI launch displayed the "Staking Required for Rewards"
panel even for wallets that were already ``qualifying``/``earning`` —
a regression noted in QA against rc.6 (the GUI got the gate, the CLI
did not). These tests pin the new behaviour:

- ``qualifying`` / ``earning``    → prompt suppressed.
- ``unstaked`` / ``inactive`` /
  ``unknown`` / fetch-error / no
  STAKING_ADDRESS configured      → prompt shown (fail-safe).

The prompt body itself is rendered via ``rich.Panel`` + ``input()``;
we patch ``sys.stdin.isatty`` to True and ``builtins.input`` to a
no-op so the prompt path executes without blocking the test runner.
"""

from unittest.mock import patch

import pytest

from app import main as main_mod


# ── Helpers ────────────────────────────────────────────────────────


def _run_prompt():
    """Invoke ``_show_staking_prompt`` with TTY + non-blocking input."""
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value=""), \
         patch.object(main_mod, "_fetch_min_staking_amount", return_value=1), \
         patch("rich.console.Console.print") as console_print:
        main_mod._show_staking_prompt()
    return console_print


def _prompt_was_shown(console_print) -> bool:
    """The prompt path always calls Console.print at least twice
    (a blank line + the Panel). The early-return path doesn't print
    anything via Console.
    """
    return console_print.call_count > 0


# ── Gate behaviour ─────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["qualifying", "earning"])
def test_prompt_suppressed_when_already_staking(status):
    with patch.object(
        main_mod, "_fetch_wallet_staking_status", return_value=status,
    ):
        console_print = _run_prompt()
    assert not _prompt_was_shown(console_print), (
        f"prompt should be suppressed for staking_status={status!r}"
    )


@pytest.mark.parametrize(
    "status", ["unstaked", "inactive", "unknown", "—", None],
)
def test_prompt_shown_when_not_yet_staking(status):
    with patch.object(
        main_mod, "_fetch_wallet_staking_status", return_value=status,
    ):
        console_print = _run_prompt()
    assert _prompt_was_shown(console_print), (
        f"prompt should be shown for staking_status={status!r}"
    )


# ── Fetch helper fail-safe ─────────────────────────────────────────
#
# ``_fetch_wallet_staking_status`` is the production helper — it
# swallows all errors internally and returns ``None`` so the prompt
# treats unknown / unreachable / not-yet-onboarded as "not staking"
# and falls through to the existing nag (first-run setup unaffected).


def test_fetch_returns_none_when_no_staking_address(monkeypatch):
    """No STAKING_ADDRESS configured (e.g. pre-onboarding) → None,
    which the gate treats as "not yet staking" so the nag still fires."""

    class _StubSettings:
        STAKING_ADDRESS = ""
        COORDINATION_API_URL = "http://localhost:9999"

    monkeypatch.setattr(main_mod, "load_settings", lambda: _StubSettings())
    assert main_mod._fetch_wallet_staking_status() is None


def test_fetch_returns_none_on_http_error(monkeypatch):
    """Network failure → None (fail-safe). The caller treats None as
    "not staking" so the existing prompt still fires."""

    class _StubSettings:
        STAKING_ADDRESS = "0x" + "ab" * 20
        COORDINATION_API_URL = "http://localhost:9999"

    def _boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main_mod, "load_settings", lambda: _StubSettings())
    import httpx
    monkeypatch.setattr(httpx, "get", _boom)
    assert main_mod._fetch_wallet_staking_status() is None
