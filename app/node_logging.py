"""Structured logging for SpaceRouter Node.

Provides:
- CLI console formatter with rich, human-readable output
- File handler with rotation for GUI log persistence
- Shared connection/status tracking for periodic summaries
"""

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from app.paths import config_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Node activity tracker (shared between CLI and GUI)
# ---------------------------------------------------------------------------


class NodeActivityTracker:
    """Tracks node activity for periodic status summaries."""

    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.connections_served: int = 0
        self.connections_active: int = 0
        self.bytes_relayed: int = 0
        self.last_health_check: float | None = None
        self.last_health_status: str = ""
        self.health_check_count: int = 0
        self.health_check_failures: int = 0
        self.last_registration_time: float | None = None
        self.reconnect_count: int = 0

    def record_connection(self) -> int:
        """Record a new incoming connection. Returns the new total."""
        self.connections_served += 1
        self.connections_active += 1
        return self.connections_served

    def record_connection_closed(self) -> None:
        self.connections_active = max(0, self.connections_active - 1)

    def record_health_check(self, status: str) -> None:
        self.last_health_check = time.time()
        self.last_health_status = status
        self.health_check_count += 1
        if status not in ("online", "active"):
            self.health_check_failures += 1

    def record_reconnect(self) -> None:
        self.reconnect_count += 1

    @property
    def uptime_str(self) -> str:
        elapsed = time.time() - self.start_time
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"


# Module-level singleton
activity = NodeActivityTracker()


def reset_activity() -> None:
    """Reset the activity tracker (e.g. on node restart)."""
    global activity
    activity = NodeActivityTracker()


# ---------------------------------------------------------------------------
# CLI console formatter — rich, human-readable output
# ---------------------------------------------------------------------------

# State label mappings for user-friendly display
_STATE_LABELS = {
    "idle": "IDLE",
    "initializing": "INIT",
    "binding": "BIND",
    "registering": "REG ",
    "running": " OK ",
    "reconnecting": "RECONN",
    "passphrase_required": "AUTH",
    "error_transient": "RETRY",
    "error_permanent": "ERROR",
    "stopping": "STOP",
}


class CLIFormatter(logging.Formatter):
    """Compact, colour-free formatter for CLI output.

    Format: ``HH:MM:SS [LEVEL] module: message``
    """

    def format(self, record: logging.LogRecord) -> str:
        # Compact timestamp (just time, not date — date is in the log file)
        ct = self.converter(record.created)
        time_str = time.strftime("%H:%M:%S", ct)

        level = record.levelname
        # Pad level to 5 chars for alignment
        level_padded = level.ljust(5)

        # Use short module name
        name = record.name.rsplit(".", 1)[-1] if record.name else "root"

        msg = record.getMessage()

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            msg = f"{msg}\n{record.exc_text}"

        return f"{time_str} [{level_padded}] {name}: {msg}"


# ---------------------------------------------------------------------------
# Setup functions
# ---------------------------------------------------------------------------

_STATUS_INTERVAL = 300  # 5 minutes


def setup_cli_logging(log_level: str = "INFO") -> None:
    """Configure logging for CLI mode with structured console output.

    Replaces the default basicConfig with our CLIFormatter.
    """
    root = logging.getLogger()
    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)

    level = getattr(logging, log_level.upper(), logging.INFO)
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(CLIFormatter())
    root.addHandler(handler)


def setup_gui_file_logging(log_level: str = "INFO") -> Path | None:
    """Add a rotating file handler for GUI log persistence.

    Logs are stored in the platform-specific data directory:
    - macOS: ~/Library/Application Support/SpaceRouter/logs/
    - Linux: ~/.local/share/SpaceRouter/logs/  (via XDG)
    - Windows: %LOCALAPPDATA%/SpaceRouter/logs/

    Returns the log directory path, or None if setup failed.
    """
    try:
        log_dir = _gui_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "spacerouter-node.log"

        level = getattr(logging, log_level.upper(), logging.INFO)

        handler = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))

        root = logging.getLogger()
        root.addHandler(handler)

        logger.info("Log file: %s", log_file)
        return log_dir
    except Exception:
        logger.warning("Failed to set up log file persistence", exc_info=True)
        return None


def _gui_log_dir() -> Path:
    """Return the log directory path for the GUI."""
    base = config_dir()
    return base / "logs"


def get_log_file_path() -> Path | None:
    """Return the current log file path if file logging is active."""
    try:
        return _gui_log_dir() / "spacerouter-node.log"
    except Exception:
        return None
