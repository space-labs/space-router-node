"""Node state machine with retry scheduling.

Defines the lifecycle states, enforces valid transitions, and provides
a status snapshot for the GUI to poll.
"""

import enum
import logging
import random
import time
from dataclasses import dataclass, field

from app.errors import NodeError, NodeErrorCode

logger = logging.getLogger(__name__)


class NodeState(enum.Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    BINDING = "binding"
    REGISTERING = "registering"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    PASSPHRASE_REQUIRED = "passphrase_required"
    ERROR_TRANSIENT = "error_transient"
    ERROR_PERMANENT = "error_permanent"
    STOPPING = "stopping"


# Which transitions are allowed: { from_state: {to_state, ...} }
_TRANSITIONS: dict[NodeState, set[NodeState]] = {
    NodeState.IDLE: {NodeState.INITIALIZING, NodeState.STOPPING},
    NodeState.INITIALIZING: {
        NodeState.BINDING,
        NodeState.PASSPHRASE_REQUIRED,
        NodeState.ERROR_PERMANENT,
        NodeState.ERROR_TRANSIENT,
        NodeState.STOPPING,
    },
    NodeState.BINDING: {
        NodeState.REGISTERING,
        NodeState.ERROR_TRANSIENT,
        NodeState.ERROR_PERMANENT,
        NodeState.STOPPING,
    },
    NodeState.REGISTERING: {
        NodeState.RUNNING,
        NodeState.ERROR_TRANSIENT,
        NodeState.ERROR_PERMANENT,
        NodeState.STOPPING,
    },
    NodeState.RUNNING: {
        NodeState.RECONNECTING,
        NodeState.STOPPING,
    },
    NodeState.RECONNECTING: {
        NodeState.RUNNING,
        NodeState.ERROR_TRANSIENT,
        NodeState.ERROR_PERMANENT,
        NodeState.STOPPING,
    },
    NodeState.PASSPHRASE_REQUIRED: {
        NodeState.INITIALIZING,
        NodeState.STOPPING,
    },
    NodeState.ERROR_TRANSIENT: {
        NodeState.INITIALIZING,
        NodeState.BINDING,
        NodeState.REGISTERING,
        NodeState.RECONNECTING,
        NodeState.ERROR_PERMANENT,
        NodeState.STOPPING,
        NodeState.IDLE,
    },
    NodeState.ERROR_PERMANENT: {
        NodeState.IDLE,
        NodeState.STOPPING,
    },
    NodeState.STOPPING: {
        NodeState.IDLE,
    },
}

# Backoff parameters
_BACKOFF_INITIAL = 3.0
_BACKOFF_MULTIPLIER = 2.0
_BACKOFF_CAP = 120.0
_BACKOFF_JITTER = 0.2  # ±20%

# Per-phase retry limits (None = unlimited)
_RETRY_LIMITS: dict[NodeState, int | None] = {
    NodeState.BINDING: 5,
    NodeState.REGISTERING: None,
    NodeState.RECONNECTING: None,
}


@dataclass
class NodeStatus:
    """Snapshot of current node state for the GUI."""

    state: NodeState = NodeState.IDLE
    detail: str = ""
    error_message: str | None = None
    error_code: str | None = None
    is_transient: bool = False
    retry_count: int = 0
    next_retry_at: float | None = None  # unix timestamp
    node_id: str | None = None
    cert_expiry_warning: bool = False
    staking_status: str = "—"

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "is_transient": self.is_transient,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "node_id": self.node_id,
            "cert_expiry_warning": self.cert_expiry_warning,
            "staking_status": self.staking_status,
        }


class NodeStateMachine:
    """Manages state transitions and retry scheduling for the node lifecycle."""

    def __init__(self) -> None:
        self._status = NodeStatus()
        self._retry_phase: NodeState | None = None  # phase to retry into

    @property
    def state(self) -> NodeState:
        return self._status.state

    @property
    def status(self) -> NodeStatus:
        return self._status

    def transition(self, to: NodeState, detail: str = "") -> None:
        """Move to a new state. Raises ValueError on invalid transition."""
        allowed = _TRANSITIONS.get(self._status.state, set())
        if to not in allowed:
            raise ValueError(
                f"Invalid transition: {self._status.state.value} -> {to.value}"
            )
        logger.info("State: %s -> %s%s", self._status.state.value, to.value,
                     f" ({detail})" if detail else "")
        self._status.state = to
        self._status.detail = detail

        # Clear error fields when leaving error states
        if to not in (NodeState.ERROR_TRANSIENT, NodeState.ERROR_PERMANENT):
            self._status.error_message = None
            self._status.error_code = None
            self._status.is_transient = False

        # Reset retry state when entering non-error/non-retry states
        if to in (NodeState.IDLE, NodeState.RUNNING, NodeState.PASSPHRASE_REQUIRED):
            self._status.retry_count = 0
            self._status.next_retry_at = None
            self._retry_phase = None

    def set_node_id(self, node_id: str) -> None:
        self._status.node_id = node_id

    def set_cert_warning(self, warning: bool) -> None:
        self._status.cert_expiry_warning = warning

    def handle_error(self, error: NodeError, phase: NodeState) -> float | None:
        """Process an error and return the retry delay (or None if permanent).

        Transitions to ERROR_TRANSIENT or ERROR_PERMANENT based on the error.
        For transient errors, computes the backoff delay and returns it.
        """
        self._status.error_message = error.user_message
        self._status.error_code = error.code.value
        self._status.is_transient = error.is_transient

        if not error.is_transient:
            self.transition(NodeState.ERROR_PERMANENT, error.user_message)
            return None

        # Check retry limit for this phase
        limit = _RETRY_LIMITS.get(phase)
        self._status.retry_count += 1
        if limit is not None and self._status.retry_count > limit:
            logger.warning(
                "Retry limit (%d) exceeded for %s", limit, phase.value
            )
            self.transition(NodeState.ERROR_PERMANENT,
                            f"{error.user_message} (max retries exceeded)")
            return None

        # Compute backoff delay with jitter
        delay = min(
            _BACKOFF_INITIAL * (_BACKOFF_MULTIPLIER ** (self._status.retry_count - 1)),
            _BACKOFF_CAP,
        )
        jitter = delay * _BACKOFF_JITTER
        delay += random.uniform(-jitter, jitter)
        delay = max(delay, 1.0)

        self._status.next_retry_at = time.time() + delay
        self._retry_phase = phase

        attempt_info = f"Attempt {self._status.retry_count}"
        if limit:
            attempt_info += f"/{limit}"
        detail = f"{error.user_message} ({attempt_info}, retry in {delay:.0f}s)"

        self.transition(NodeState.ERROR_TRANSIENT, detail)
        logger.info("Retry scheduled: %s in %.1fs", phase.value, delay)
        return delay

    @property
    def retry_phase(self) -> NodeState | None:
        """The phase to retry into after a transient error."""
        return self._retry_phase

    def reset(self) -> None:
        """Reset to initial state."""
        self._status = NodeStatus()
        self._retry_phase = None
