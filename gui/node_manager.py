"""Manages the SpaceRouter node lifecycle in a background thread."""

import asyncio
import logging
import threading

from app.errors import NodeError, classify_error
from app.identity import KeystorePassphraseRequired
from app.state import NodeState, NodeStateMachine, NodeStatus

logger = logging.getLogger(__name__)


class NodeManager:
    """Start/stop the Home Node daemon in a background thread with its own event loop."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._sm = NodeStateMachine()

    @property
    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._sm.state in (
                NodeState.INITIALIZING,
                NodeState.BINDING,
                NodeState.REGISTERING,
                NodeState.RUNNING,
                NodeState.RECONNECTING,
                NodeState.ERROR_TRANSIENT,
            )
        )

    @property
    def phase(self) -> str:
        """Backward-compatible phase string."""
        state = self._sm.state
        if state == NodeState.RUNNING:
            return "running"
        if state in (NodeState.REGISTERING, NodeState.RECONNECTING):
            return "registering"
        if state in (NodeState.INITIALIZING, NodeState.BINDING):
            return "starting"
        return "stopped"

    @property
    def status(self) -> NodeStatus:
        return self._sm.status

    @property
    def last_error(self) -> str | None:
        """Backward-compatible error string."""
        return self._sm.status.error_message

    def start(self) -> None:
        """Start the node in a background thread."""
        if self.is_running:
            logger.warning("Node is already running")
            return

        self._sm.reset()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="spacerouter-node")
        self._thread.start()

    def retry(self) -> None:
        """Retry from ERROR_PERMANENT without clearing config."""
        if self._sm.state not in (NodeState.ERROR_PERMANENT, NodeState.IDLE):
            logger.warning("Cannot retry from state %s", self._sm.state.value)
            return
        self._sm.reset()
        self.start()

    def _run_loop(self) -> None:
        """Thread target: create an event loop and run the node."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()

        try:
            from app.config import load_settings
            from app.main import _run

            self._loop.run_until_complete(
                _run(
                    settings_override=load_settings(),
                    stop_event=self._stop_event,
                    on_phase=self._on_phase,
                    state_machine=self._sm,
                )
            )
        except KeystorePassphraseRequired:
            # State machine already transitioned to PASSPHRASE_REQUIRED in _run()
            logger.info("Passphrase required — waiting for user input")
            return
        except NodeError as exc:
            logger.warning("Node error: %s", exc)
            # State machine already has the error info if handle_error was called,
            # but if the error propagated directly (first occurrence), set it now.
            if self._sm.state not in (NodeState.ERROR_PERMANENT, NodeState.ERROR_TRANSIENT):
                delay = self._sm.handle_error(exc, self._sm.state)
                if delay is not None:
                    # Transient error — schedule retry in a new run
                    self._schedule_retry(delay)
                    return
        except SystemExit:
            logger.warning("Node exited with SystemExit")
            if self._sm.state not in (NodeState.ERROR_PERMANENT, NodeState.ERROR_TRANSIENT):
                from app.errors import NodeErrorCode
                self._sm.handle_error(
                    NodeError(NodeErrorCode.UNEXPECTED_ERROR, "Node process exited unexpectedly"),
                    NodeState.IDLE,
                )
        except Exception as exc:
            logger.exception("Node crashed: %s", exc)
            error = classify_error(exc)
            if self._sm.state not in (NodeState.ERROR_PERMANENT, NodeState.ERROR_TRANSIENT):
                delay = self._sm.handle_error(error, self._sm.state)
                if delay is not None:
                    self._schedule_retry(delay)
                    return
        finally:
            if self._sm.state not in (
                NodeState.ERROR_PERMANENT, NodeState.ERROR_TRANSIENT,
                NodeState.PASSPHRASE_REQUIRED, NodeState.IDLE,
            ):
                try:
                    self._sm.transition(NodeState.IDLE)
                except ValueError:
                    pass
            if self._loop and not self._loop.is_closed():
                self._loop.close()
            self._loop = None
            self._stop_event = None

    def _schedule_retry(self, delay: float) -> None:
        """Schedule an automatic retry after a transient error."""
        import time

        def _retry_thread() -> None:
            time.sleep(delay)
            if self._sm.state == NodeState.ERROR_TRANSIENT:
                logger.info("Auto-retrying after %.1fs", delay)
                # Determine which phase to retry
                retry_phase = self._sm.retry_phase
                self._loop = None
                self._stop_event = None
                self._thread = threading.Thread(
                    target=self._run_loop, daemon=True, name="spacerouter-node-retry",
                )
                self._thread.start()

        threading.Thread(target=_retry_thread, daemon=True, name="spacerouter-retry-timer").start()

    def _on_phase(self, phase: str) -> None:
        """Callback from _run() to report lifecycle phase."""
        # The state machine is already updated by _run(), this is just for logging
        pass

    def stop(self, timeout: float = 20.0) -> None:
        """Signal the node to stop and wait for the thread to finish."""
        if not self._thread or not self._thread.is_alive():
            if self._sm.state not in (NodeState.IDLE, NodeState.ERROR_PERMANENT):
                try:
                    self._sm.transition(NodeState.IDLE)
                except ValueError:
                    self._sm.reset()
            return

        try:
            self._sm.transition(NodeState.STOPPING, "Shutting down")
        except ValueError:
            pass

        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass

        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Node thread did not stop within %.1fs — forcing", timeout)
                self._force_cancel_loop(loop)
                self._thread.join(timeout=3.0)
            self._thread = None

    def _force_cancel_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cancel all running tasks and stop the event loop."""
        if not loop or loop.is_closed():
            return
        try:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
