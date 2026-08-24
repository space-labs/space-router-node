#!/usr/bin/env python3
"""Generate the GUI status-poll fixtures for the jsdom tests.

The JS tests must render the *real* payload the backend sends, so the
sequences are produced by driving the actual ``NodeStateMachine`` and dumping
``NodeStatus.to_dict()`` — no hand-written payload shapes that can drift.

The retry loop reproduced here is the real one.  ``NodeManager._schedule_retry``
restarts ``_run_loop`` from scratch after the backoff, and ``_run()`` replays
the whole lifecycle, so a node that cannot register cycles

    initializing -> binding -> registering -> error_transient
                 -> initializing -> binding -> registering -> error_transient -> ...

``NodeStateMachine.transition`` clears ``error_code``/``error_message`` on the
way out of an error state but keeps ``retry_count`` until the node reaches
running/idle, which is exactly what the GUI has to cope with.

``error_report_available`` is not set by the state machine — ``NodeManager``
injects it (``gui/api.py:526``) and it is sticky once ``_mark_reportable`` has
fired for a reportable code, so that is modelled here too.

Output is deterministic (the backoff RNG is seeded and the clock is frozen), so
regenerating on an unchanged backend produces a byte-identical file and CI can
diff it to catch fixture drift.

Regenerate with:

    .venv/bin/python tests/js/fixtures/gen_status_sequence.py
"""

from __future__ import annotations

import json
import pathlib
import random
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))

from app import state as state_mod  # noqa: E402
from app.error_report import is_reportable  # noqa: E402
from app.errors import NodeError, NodeErrorCode  # noqa: E402
from app.state import NodeState, NodeStateMachine  # noqa: E402

OUT = pathlib.Path(__file__).with_name("status_sequence.json")

# Frozen wall clock. NodeStateMachine.handle_error stamps
# ``next_retry_at = time.time() + delay``; the jsdom harness re-bases those onto
# "now" so the GUI countdown is live, and freezing the base here keeps the
# checked-in fixture stable.
BASE_TIME = 1_700_000_000.0


class _FrozenTime:
    """Just enough of the ``time`` module for app.state."""

    @staticmethod
    def time() -> float:
        return BASE_TIME


class Recorder:
    """Drives a real state machine and records every GUI status snapshot."""

    def __init__(self) -> None:
        self.sm = NodeStateMachine()
        self.report_available = False
        self.frames: list[dict] = []

    def _snapshot(self) -> None:
        status = self.sm.status
        # gui/node_manager.py:68 — NodeManager stamps the sticky flag onto the
        # snapshot it hands to gui/api.py:get_status().
        status.error_report_available = self.report_available
        payload = status.to_dict()
        # The few extra keys gui/api.py:get_status() layers on top of
        # NodeStatus.to_dict() that the render path actually reads.
        payload["error"] = payload["error_message"]
        payload["staking_address"] = "0x1111111111111111111111111111111111111111"
        payload["collection_address"] = "0x1111111111111111111111111111111111111111"
        payload["identity_address"] = "0x2222222222222222222222222222222222222222"
        payload["environment"] = "test"
        payload["version_check"] = None
        self.frames.append(payload)

    def transition(self, state: NodeState, detail: str = "") -> None:
        self.sm.transition(state, detail)
        self._snapshot()

    def fail(self, code: NodeErrorCode, phase: NodeState) -> None:
        self.sm.handle_error(NodeError(code, "injected by the fixture generator"), phase)
        if is_reportable(code.value):
            # gui/node_manager.py:_mark_reportable — sticky until the next
            # start()/successful send.
            self.report_available = True
        self._snapshot()

    def lifecycle_attempt(self) -> None:
        """One pass of _run(): initializing -> binding -> registering."""
        self.transition(NodeState.INITIALIZING, "Loading identity and certificates")
        self.transition(NodeState.BINDING, "Binding to port 8443")
        self.transition(NodeState.REGISTERING, "Registering with coordination server")


def _registration_retry_loop(cycles: int, code: NodeErrorCode) -> list[dict]:
    """`cycles` failed registration attempts, from a cold start."""
    rec = Recorder()
    for _ in range(cycles):
        rec.lifecycle_attempt()
        rec.fail(code, NodeState.REGISTERING)
    return rec.frames


def _code_change_after_retries() -> list[dict]:
    """Retry loop on endpoint_unreachable, then the error genuinely changes."""
    rec = Recorder()
    for _ in range(4):
        rec.lifecycle_attempt()
        rec.fail(NodeErrorCode.ENDPOINT_UNREACHABLE, NodeState.REGISTERING)
    # Coordination starts answering but rejects us for a different reason.
    for _ in range(2):
        rec.lifecycle_attempt()
        rec.fail(NodeErrorCode.RATE_LIMITED, NodeState.REGISTERING)
    return rec.frames


def _recover_then_fail_again() -> list[dict]:
    """Retry loop, a genuine recovery to running, then a fresh failure."""
    rec = Recorder()
    for _ in range(4):
        rec.lifecycle_attempt()
        rec.fail(NodeErrorCode.ENDPOINT_UNREACHABLE, NodeState.REGISTERING)
    # Registration finally succeeds — transition() zeroes retry_count here.
    rec.lifecycle_attempt()
    rec.transition(NodeState.RUNNING, "Node ID: 0x2222222222...")
    # Later the link drops: running -> reconnecting -> error_transient -> ...
    rec.transition(NodeState.RECONNECTING, "Lost connection to coordination server")
    for _ in range(4):
        rec.fail(NodeErrorCode.CONNECTION_LOST, NodeState.RECONNECTING)
        rec.transition(NodeState.RECONNECTING, "Retrying registration")
    return rec.frames


def _operator_restart_after_retries() -> list[dict]:
    """Retry loop, then the operator hits Stop and Start again."""
    rec = Recorder()
    for _ in range(4):
        rec.lifecycle_attempt()
        rec.fail(NodeErrorCode.ENDPOINT_UNREACHABLE, NodeState.REGISTERING)
    # Stop: gui/node_manager.py:stop() drives ERROR_TRANSIENT -> IDLE.
    rec.transition(NodeState.IDLE)
    # Start: NodeManager.start() calls _sm.reset(), so retry_count is 0 again.
    rec.sm.reset()
    rec.report_available = False
    for _ in range(4):
        rec.lifecycle_attempt()
        rec.fail(NodeErrorCode.ENDPOINT_UNREACHABLE, NodeState.REGISTERING)
    return rec.frames


def main() -> None:
    # Freeze the clock and seed the backoff jitter so the fixture is stable.
    state_mod.time = _FrozenTime  # type: ignore[assignment]
    state_mod.random = random.Random(20260824)  # type: ignore[assignment]

    data = {
        "generated_at": BASE_TIME,
        "note": (
            "Generated by tests/js/fixtures/gen_status_sequence.py from the real "
            "app.state.NodeStateMachine. Do not hand-edit."
        ),
        "sequences": {
            "registration_retry_loop": _registration_retry_loop(
                6, NodeErrorCode.ENDPOINT_UNREACHABLE
            ),
            "code_change_after_retries": _code_change_after_retries(),
            "recover_then_fail_again": _recover_then_fail_again(),
            "operator_restart_after_retries": _operator_restart_after_retries(),
        },
    }
    OUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    counts = {k: len(v) for k, v in data["sequences"].items()}
    print(f"wrote {OUT} — {counts}")


if __name__ == "__main__":
    main()
