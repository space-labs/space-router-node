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


# ── rc.8 #1 — query endpoint, not path endpoint ────────────────────


def test_fetch_uses_query_endpoint_with_staking_address(monkeypatch):
    """rc.8 fix: the helper must call ``GET /nodes?staking_address=…``
    (case-insensitive list lookup), not ``GET /nodes/{addr}`` (path is
    typed as a node UUID and triggers Postgres cast errors → 500)."""

    class _StubSettings:
        STAKING_ADDRESS = "0xAbCdEf" + "00" * 17
        COORDINATION_API_URL = "http://coord.example"

    monkeypatch.setattr(main_mod, "load_settings", lambda: _StubSettings())

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"staking_status": "earning"}]

    def _fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "get", _fake_get)

    assert main_mod._fetch_wallet_staking_status() == "earning"
    # The URL must NOT contain the wallet address as a path segment.
    assert captured["url"].endswith("/nodes")
    assert "/nodes/0x" not in captured["url"]
    # The wallet must travel as a query param, lowercased per the
    # original contract.
    assert captured["params"] == {"staking_address": "0xabcdef" + "00" * 17}


def test_fetch_returns_none_when_node_not_registered(monkeypatch):
    """Empty list response (wallet not yet registered) must return None
    — the operator should still see the "Staking Required" prompt at
    first-run setup time."""

    class _StubSettings:
        STAKING_ADDRESS = "0x" + "11" * 20
        COORDINATION_API_URL = "http://coord.example"

    monkeypatch.setattr(main_mod, "load_settings", lambda: _StubSettings())

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())

    assert main_mod._fetch_wallet_staking_status() is None


def test_fetch_returns_first_node_status(monkeypatch):
    """If multiple nodes share a staking address (multi-node operator),
    the first row's status drives the gate — same behaviour as the
    pre-rc.8 single-object response."""

    class _StubSettings:
        STAKING_ADDRESS = "0x" + "22" * 20
        COORDINATION_API_URL = "http://coord.example"

    monkeypatch.setattr(main_mod, "load_settings", lambda: _StubSettings())

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"staking_status": "qualifying"},
                {"staking_status": "earning"},
            ]

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())

    assert main_mod._fetch_wallet_staking_status() == "qualifying"
