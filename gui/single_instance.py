"""Single-instance enforcement for SpaceRouter GUI.

Uses a lightweight TCP socket on localhost to both detect an existing instance
and signal it to bring its window to the foreground.  The protocol is a simple
handshake: the second instance sends ``SPACEROUTER_SHOW\\n`` and expects ``OK\\n``
back.  If the port is occupied by a non-SpaceRouter process the handshake will
fail and the new instance will start normally (rare edge case).
"""

import logging
import socket
import sys
import threading

logger = logging.getLogger(__name__)

# Arbitrary high port; unlikely to collide with well-known services.
_IPC_PORT = 47_891


class SingleInstanceLock:
    """Ensures only one SpaceRouter GUI process runs at a time."""

    def __init__(self) -> None:
        self._server: socket.socket | None = None
        self._running = False
        self._on_show: object = None  # callable or None

    def try_acquire(self, *, on_show_request=None) -> bool:
        """Attempt to become the single instance.

        Returns ``True`` if the lock was acquired (we are the first instance).
        Returns ``False`` if another SpaceRouter instance is already running —
        in that case we signal it to show its window before returning.

        *on_show_request* is called (from a background thread) whenever a
        second instance asks this one to show its window.
        """
        self._on_show = on_show_request

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # On Windows SO_EXCLUSIVEADDRUSE prevents another process from
            # binding the same port even with SO_REUSEADDR.
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            sock.bind(("127.0.0.1", _IPC_PORT))
            sock.listen(2)
            self._server = sock
            self._running = True
            threading.Thread(
                target=self._accept_loop, daemon=True, name="single-instance"
            ).start()
            logger.info("Single-instance lock acquired (port %d)", _IPC_PORT)
            return True
        except OSError:
            # Port occupied — possibly another SpaceRouter instance.
            logger.info("Port %d in use — checking for existing instance", _IPC_PORT)
            if self._signal_existing():
                logger.info("Signalled existing instance to show its window")
                return False
            # The port is occupied by something else; start normally.
            logger.warning(
                "Port %d occupied by a foreign process — starting without lock",
                _IPC_PORT,
            )
            return True

    # ------------------------------------------------------------------
    # IPC server (runs in first instance)
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()  # type: ignore[union-attr]
                conn.settimeout(2.0)
                data = conn.recv(64)
                if data.strip() == b"SPACEROUTER_SHOW":
                    conn.sendall(b"OK\n")
                    if self._on_show:
                        self._on_show()
                conn.close()
            except OSError:
                if not self._running:
                    break
            except Exception:
                logger.debug("single-instance accept error", exc_info=True)

    # ------------------------------------------------------------------
    # IPC client (runs in second instance)
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_existing() -> bool:
        """Send SHOW to an existing instance. Returns True on valid response."""
        try:
            with socket.create_connection(("127.0.0.1", _IPC_PORT), timeout=2) as s:
                s.sendall(b"SPACEROUTER_SHOW\n")
                resp = s.recv(32)
                return resp.strip() == b"OK"
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def release(self) -> None:
        """Release the lock (close the server socket)."""
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
