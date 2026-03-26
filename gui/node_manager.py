"""Manages the SpaceRouter node lifecycle in a background thread."""

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class _ErrorCapture(logging.Handler):
    """Captures the last ERROR-level log message for display in the GUI."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.last_message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.last_message = self.format(record)


_error_capture = _ErrorCapture()
_error_capture.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_error_capture)


def _last_error_message() -> str | None:
    """Return the last ERROR log message, then clear it."""
    msg = _error_capture.last_message
    _error_capture.last_message = None
    return msg


class NodeManager:
    """Start/stop the Home Node daemon in a background thread with its own event loop."""

    # Phases: starting → registering → running → stopped
    PHASE_STOPPED = "stopped"
    PHASE_STARTING = "starting"
    PHASE_REGISTERING = "registering"
    PHASE_RUNNING = "running"

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._phase = self.PHASE_STOPPED
        self._error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def phase(self) -> str:
        if not self.is_running and self._phase != self.PHASE_STOPPED:
            return self.PHASE_STOPPED
        return self._phase

    @property
    def last_error(self) -> str | None:
        return self._error

    def start(self) -> None:
        """Start the node in a background thread."""
        if self.is_running:
            logger.warning("Node is already running")
            return

        self._error = None
        self._phase = self.PHASE_STARTING
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="spacerouter-node")
        self._thread.start()

    def _on_phase(self, phase: str) -> None:
        """Callback from _run() to report lifecycle phase."""
        self._phase = phase

    def _run_loop(self) -> None:
        """Thread target: create an event loop and run _run()."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._running = True

        try:
            # Reload config and main so they pick up fresh env vars
            # (critical after fresh_restart which clears and re-applies config).
            import importlib
            import app.config
            importlib.reload(app.config)
            import app.main
            importlib.reload(app.main)
            from app.main import _run

            self._loop.run_until_complete(
                _run(stop_event=self._stop_event, on_phase=self._on_phase)
            )
        except SystemExit:
            # _run() calls sys.exit() on fatal errors; catch it so the GUI stays alive.
            # Capture the last ERROR-level log message for a meaningful error.
            logger.warning("Node exited with SystemExit")
            self._error = _last_error_message() or "Node failed to start. Check logs for details."
        except Exception as exc:
            logger.exception("Node crashed: %s", exc)
            self._error = str(exc)
        finally:
            self._running = False
            self._phase = self.PHASE_STOPPED
            self._loop.close()
            self._loop = None
            self._stop_event = None

    def stop(self, timeout: float = 20.0) -> None:
        """Signal the node to stop and wait for the thread to finish."""
        if not self.is_running:
            return

        # Capture local refs to avoid TOCTOU race with _run_loop's finally block,
        # which can close/null the loop between our check and our use.
        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # Loop already closed — node is shutting down on its own
                pass

        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Node thread did not stop within %.1fs — cancelling tasks", timeout)
                self._force_cancel_loop(loop)
                self._thread.join(timeout=3.0)
            self._thread = None

    def _force_cancel_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cancel all running tasks in the node's event loop to force shutdown."""
        if not loop or loop.is_closed():
            return
        try:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass
