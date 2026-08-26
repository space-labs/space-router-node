"""Manages the SpaceRouter node lifecycle in a background thread."""

from __future__ import annotations

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
        self._error_report_available: bool = False
        self._last_error: NodeError | None = None
        self._node_context_snapshot: dict | None = None
        self._version_check = None  # VersionCheckResult | None
        self._retry_cancel = threading.Event()

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

    def has_live_thread(self) -> bool:
        """True if there's a daemon thread currently alive, regardless of state.

        Distinct from ``is_running`` which gates on the state machine being in
        an "operational" set; this one is for cleanup paths that must reap
        orphan threads from PASSPHRASE_REQUIRED / ERROR_TRANSIENT (rc.6 BLK-4).
        """
        return self._thread is not None and self._thread.is_alive()

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
        ns = self._sm.status
        ns.error_report_available = self._error_report_available
        return ns

    @property
    def last_error(self) -> str | None:
        """Backward-compatible error string."""
        return self._sm.status.error_message

    @property
    def version_check(self):
        """Latest VersionCheckResult (or None)."""
        return self._version_check

    def start(self) -> None:
        """Start the node in a background thread."""
        if self.is_running:
            logger.warning("Node is already running")
            return

        # Pre-flight version check (sync, fail-safe) — result available
        # immediately for the first GUI status poll.
        try:
            from app.updater import check_version_sync
            from app.config import load_settings
            s = load_settings()
            self._version_check = check_version_sync(s.COORDINATION_API_URL)
        except Exception:
            logger.debug("GUI pre-flight version check failed", exc_info=True)

        self._retry_cancel.set()
        self._retry_cancel = threading.Event()
        self._sm.reset()
        self._error_report_available = False
        self._last_error = None
        self._node_context_snapshot = None
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
                    on_version_check=self._on_version_check,
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
                    # Transient error — schedule retry.
                    # Mark reportable so the JS can show the modal after 3+ retries.
                    self._schedule_retry(delay)
                    self._mark_reportable(exc)
                    return
            self._mark_reportable(exc)
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
                    self._mark_reportable(error)
                    return
            self._mark_reportable(error)
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
        cancel = self._retry_cancel

        def _retry_thread() -> None:
            if cancel.wait(delay):
                return
            if cancel is not self._retry_cancel or self.has_live_thread():
                return
            if self._sm.state == NodeState.ERROR_TRANSIENT:
                logger.info("Auto-retrying after %.1fs", delay)
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

    def _on_version_check(self, result) -> None:  # noqa: ANN001
        """Callback from _run() / _version_check_loop to propagate result."""
        self._version_check = result

    def stop(self, timeout: float = 20.0) -> None:
        """Signal the node to stop and wait for the thread to finish.

        rc.5 MAJ: improved stop path so Quit/Reset doesn't leak the
        node port. The daemon runs as asyncio code in a sibling thread
        of the GUI process — we cannot SIGKILL it without taking down
        the GUI itself, so the recovery sequence is:

        1. ``stop_event.set()`` — graceful shutdown via ``_run``'s
           cancellation point. Bounded by *timeout*.
        2. If the thread is still alive, ``_force_cancel_loop`` cancels
           every running task on the loop and calls ``loop.stop``.
        3. Final 3s join. If it's STILL alive at this point we cannot
           safely escalate — surface a loud ERROR with the live thread's
           name and the configured port so the operator knows the next
           start may collide. We log + return rather than block the GUI
           indefinitely.
        4. After the join completes, poll the configured port for up
           to ~3s with bounded retries to confirm the OS released the
           socket; log a warning if it's still bound (helps diagnose
           "Reset Node failed: address in use" reports).
        """
        self._retry_cancel.set()
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
                logger.warning(
                    "Node thread did not stop within %.1fs — forcing", timeout,
                )
                self._force_cancel_loop(loop)
                self._thread.join(timeout=3.0)
                if self._thread.is_alive():
                    # We can't SIGKILL — that would take down the GUI.
                    # Loudest possible log: this leaks the node port and
                    # the next start will collide with EADDRINUSE.
                    logger.error(
                        "Node thread %r did not exit after force-cancel; "
                        "the asyncio loop may be stuck inside a blocking "
                        "third-party call. Port %s may stay bound until "
                        "the GUI process is restarted.",
                        self._thread.name,
                        self._configured_port_for_log(),
                    )
            if not self._thread.is_alive():
                self._thread = None

        # Best-effort port-release verification. We bind-test the
        # configured port; if it's still bound after a few hundred ms,
        # log a warning so the operator can see the diagnosis.
        self._wait_for_port_release()

    @staticmethod
    def _configured_port_for_log() -> int | None:
        """Best-effort lookup of the daemon's configured listen port."""
        try:
            from app.config import load_settings
            return int(load_settings().NODE_PORT)
        except Exception:
            return None

    def _wait_for_port_release(self) -> None:
        """Poll the configured listen port until it's free (bounded).

        Uses a transient ``socket.socket().bind`` attempt as the probe;
        on success we close immediately so we don't actually take the
        port. Bounded retries (3 attempts at 200ms / 500ms / 1500ms);
        failure logs a warning but does NOT raise — Reset/Quit must
        still proceed even if the port is wedged.
        """
        port = self._configured_port_for_log()
        if not port:
            return
        import socket
        import time

        delays = (0.2, 0.5, 1.5)
        for delay in delays:
            time.sleep(delay)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                # Bound successfully → port is free.
                s.close()
                return
            except OSError:
                s.close()
                continue
        logger.warning(
            "Port %s still bound after node stop — next start may fail "
            "with 'address in use'. Restart the GUI if this persists.",
            port,
        )

    def _mark_reportable(self, error: NodeError) -> None:
        """Flag the error as eligible for user-initiated reporting."""
        from app.error_report import is_reportable

        if is_reportable(error.code.value):
            self._error_report_available = True
            self._last_error = error

    def send_error_report(self) -> dict:
        """Build, sign, and send the current error to coordination API.

        Called from the GUI thread via pywebview API.
        Returns ``{"ok": True}`` on success or ``{"ok": False, "error": "..."}``
        on failure.
        """
        if not self._error_report_available or self._last_error is None:
            return {"ok": False, "error": "No error report available"}

        try:
            from app.config import load_settings
            from app.error_report import build_error_report, send_error_report_sync

            settings = load_settings()
            status_snapshot = self._sm.status

            # Try to get identity info from the settings-level env vars
            import os
            identity_key = os.environ.get("_SR_IDENTITY_KEY", "")
            identity_address = os.environ.get("_SR_IDENTITY_ADDRESS", "")

            if not identity_key or not identity_address:
                # Attempt to load identity from disk
                try:
                    from app.identity import load_or_create_identity
                    identity_key, identity_address = load_or_create_identity(
                        settings.IDENTITY_KEY_PATH,
                        settings.IDENTITY_PASSPHRASE,
                    )
                except Exception:
                    return {"ok": False, "error": "Cannot load identity key for signing"}

            report = build_error_report(
                self._last_error,
                node_id=status_snapshot.node_id,
                identity_address=identity_address,
                staking_address=settings.STAKING_ADDRESS or None,
                collection_address=settings.COLLECTION_ADDRESS or None,
                settings=settings,
                app_type="gui",
                state_snapshot=status_snapshot,
            )

            ok = send_error_report_sync(
                report,
                identity_key,
                identity_address,
                settings.COORDINATION_API_URL,
            )

            if ok:
                self._error_report_available = False
                return {"ok": True}
            else:
                return {"ok": False, "error": "Server rejected the report"}
        except Exception as exc:
            logger.warning("Failed to send error report: %s", exc)
            return {"ok": False, "error": str(exc)}

    def _force_cancel_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cancel all running tasks so the shutdown path can unwind."""
        if not loop or loop.is_closed():
            return
        try:
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass
