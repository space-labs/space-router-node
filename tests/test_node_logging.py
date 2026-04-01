"""Tests for app.node_logging — CLI formatting, GUI file persistence, activity tracker."""

import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.node_logging import (
    CLIFormatter,
    NodeActivityTracker,
    activity,
    get_log_file_path,
    reset_activity,
    setup_cli_logging,
    setup_gui_file_logging,
)


# ---------------------------------------------------------------------------
# NodeActivityTracker
# ---------------------------------------------------------------------------


class TestNodeActivityTracker:
    def test_initial_state(self):
        tracker = NodeActivityTracker()
        assert tracker.connections_served == 0
        assert tracker.connections_active == 0
        assert tracker.health_check_count == 0
        assert tracker.health_check_failures == 0
        assert tracker.reconnect_count == 0

    def test_record_connection(self):
        tracker = NodeActivityTracker()
        total = tracker.record_connection()
        assert total == 1
        assert tracker.connections_served == 1
        assert tracker.connections_active == 1

        total = tracker.record_connection()
        assert total == 2
        assert tracker.connections_active == 2

    def test_record_connection_closed(self):
        tracker = NodeActivityTracker()
        tracker.record_connection()
        tracker.record_connection()
        tracker.record_connection_closed()
        assert tracker.connections_active == 1
        # Never goes below zero
        tracker.record_connection_closed()
        tracker.record_connection_closed()
        assert tracker.connections_active == 0

    def test_record_health_check_success(self):
        tracker = NodeActivityTracker()
        tracker.record_health_check("online")
        assert tracker.health_check_count == 1
        assert tracker.health_check_failures == 0
        assert tracker.last_health_status == "online"
        assert tracker.last_health_check is not None

    def test_record_health_check_failure(self):
        tracker = NodeActivityTracker()
        tracker.record_health_check("offline")
        assert tracker.health_check_count == 1
        assert tracker.health_check_failures == 1

    def test_record_reconnect(self):
        tracker = NodeActivityTracker()
        tracker.record_reconnect()
        tracker.record_reconnect()
        assert tracker.reconnect_count == 2

    def test_uptime_str_seconds(self):
        tracker = NodeActivityTracker()
        tracker.start_time = time.time() - 45
        uptime = tracker.uptime_str
        assert "45s" in uptime

    def test_uptime_str_minutes(self):
        tracker = NodeActivityTracker()
        tracker.start_time = time.time() - 125  # 2m 5s
        uptime = tracker.uptime_str
        assert "2m" in uptime

    def test_uptime_str_hours(self):
        tracker = NodeActivityTracker()
        tracker.start_time = time.time() - 3661  # 1h 1m 1s
        uptime = tracker.uptime_str
        assert "1h" in uptime


class TestResetActivity:
    def test_reset(self):
        from app import node_logging

        node_logging.activity.connections_served = 99
        reset_activity()
        assert node_logging.activity.connections_served == 0


# ---------------------------------------------------------------------------
# CLIFormatter
# ---------------------------------------------------------------------------


class TestCLIFormatter:
    def test_basic_format(self):
        formatter = CLIFormatter()
        record = logging.LogRecord(
            name="app.main",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Node started",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        # Should contain time, level, module name, message
        assert "[INFO ]" in output
        assert "main:" in output
        assert "Node started" in output

    def test_warning_format(self):
        formatter = CLIFormatter()
        record = logging.LogRecord(
            name="app.health",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="Health check failed",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "[WARNI" in output or "[WARNING" in output
        assert "health:" in output

    def test_exception_included(self):
        formatter = CLIFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="app.main",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Something failed",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        assert "Something failed" in output
        assert "ValueError" in output
        assert "test error" in output

    def test_args_interpolation(self):
        formatter = CLIFormatter()
        record = logging.LogRecord(
            name="app.main",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Port %d ready",
            args=(9090,),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "Port 9090 ready" in output


# ---------------------------------------------------------------------------
# setup_cli_logging
# ---------------------------------------------------------------------------


class TestSetupCLILogging:
    def test_replaces_handlers(self):
        # Add a dummy handler first
        root = logging.getLogger()
        dummy = logging.StreamHandler()
        root.addHandler(dummy)
        count_before = len(root.handlers)

        setup_cli_logging("INFO")

        # Should have exactly one handler (CLIFormatter)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, CLIFormatter)

    def test_sets_level(self):
        setup_cli_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

        # Restore
        setup_cli_logging("INFO")


# ---------------------------------------------------------------------------
# GUI file logging
# ---------------------------------------------------------------------------


class TestSetupGUIFileLogging:
    def test_creates_log_file(self, tmp_path):
        """Test that GUI file logging creates a rotating log file."""
        log_dir = tmp_path / "logs"

        with patch("app.node_logging._gui_log_dir", return_value=log_dir):
            result = setup_gui_file_logging("INFO")

        assert result == log_dir
        assert log_dir.exists()

        # Write a test message and verify it goes to the file
        test_logger = logging.getLogger("test.gui_file")
        test_logger.info("Test log message for file")

        # Flush handlers
        for handler in logging.getLogger().handlers:
            handler.flush()

        log_file = log_dir / "spacerouter-node.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Test log message for file" in content

        # Clean up: remove the file handler we added
        root = logging.getLogger()
        for h in root.handlers[:]:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                if str(tmp_path) in str(getattr(h, "baseFilename", "")):
                    root.removeHandler(h)
                    h.close()

    def test_rotation_config(self, tmp_path):
        """Test that the rotating handler has correct max size and backup count."""
        log_dir = tmp_path / "logs2"

        with patch("app.node_logging._gui_log_dir", return_value=log_dir):
            setup_gui_file_logging("DEBUG")

        root = logging.getLogger()
        rotating_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and str(tmp_path) in str(getattr(h, "baseFilename", ""))
        ]
        assert len(rotating_handlers) >= 1
        handler = rotating_handlers[-1]
        assert handler.maxBytes == 10 * 1024 * 1024  # 10 MB
        assert handler.backupCount == 5

        # Clean up
        for h in rotating_handlers:
            root.removeHandler(h)
            h.close()


class TestGetLogFilePath:
    def test_returns_path(self):
        path = get_log_file_path()
        assert path is not None
        assert path.name == "spacerouter-node.log"
        assert "logs" in str(path)
