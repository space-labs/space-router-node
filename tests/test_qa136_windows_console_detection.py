"""Windows reports NUL as a tty, so "no console" looked interactive.

QA v1.5.2-test.136 (Windows CLI): starting without stdin dropped into the
staking prompt and crashed, and the node could not be run in a console-less
configuration at all.

`isatty()` is not sufficient on Windows: NUL is a character device, so a
service, a scheduled task or a `< NUL` launch all report True and fall into the
setup wizard instead of being refused. GetConsoleMode succeeds only for a real
console handle.
"""
from __future__ import annotations

import io
import sys

import pytest

from app import cli_ui
from app.cli_ui import _stdin_is_interactive


class _Stdin(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_a_real_tty_is_interactive_on_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    assert _stdin_is_interactive() is True


def test_a_non_tty_is_never_interactive(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    assert _stdin_is_interactive() is False


def test_missing_stdin_is_not_interactive(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert _stdin_is_interactive() is False


def test_windows_nul_reports_tty_but_is_not_a_console(monkeypatch):
    """The actual defect: isatty() True, no console behind it."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(cli_ui, "_has_windows_console", lambda: False)
    assert _stdin_is_interactive() is False, (
        "NUL reports isatty() True on Windows; without a console check the "
        "node treats a service launch as an interactive terminal"
    )


def test_windows_real_console_is_interactive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    monkeypatch.setattr(cli_ui, "_has_windows_console", lambda: True)
    assert _stdin_is_interactive() is True


def test_console_probe_failure_does_not_lock_the_operator_out(monkeypatch):
    """If ctypes is unavailable, prefer prompting over refusing to run."""
    import builtins

    real_import = builtins.__import__

    def _no_ctypes(name, *args, **kwargs):
        if name == "ctypes":
            raise ImportError("no ctypes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_ctypes)
    assert cli_ui._has_windows_console() is True


def test_setup_guard_uses_the_shared_detection():
    """main.py used a bare sys.stdin.isatty(), which is the Windows trap."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    idx = src.index("--setup requires a TTY")
    window = src[max(0, idx - 400):idx]
    assert "_stdin_is_interactive()" in window
    assert "args.setup and not sys.stdin.isatty()" not in src
