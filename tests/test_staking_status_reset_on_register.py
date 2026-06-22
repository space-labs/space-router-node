"""Staking-status reset on (re)registration.

Bug (v1.5.2): when the operator changes the staking address on a RUNNING
daemon to a different/unstaked wallet, re-registration reuses the same node_id
(the identity address), so nothing blanks the stale coord-side
``staking_status`` the self-probe loop last wrote. The GUI kept showing the old
"Earning" value for up to ~60s until the next probe.

Fix: ``_phase_register`` routes both first registration and the RECONNECTING
re-registration through ``_reset_staking_status``, which blanks
``sm.status.staking_status`` back to the sentinel so a fresh probe writes the
real status. These tests exercise that helper against a real NodeStateMachine
(no mocks) at the seam, since full registration needs the coord network.
"""

from app.main import _NodeContext, _STAKING_STATUS_SENTINEL, _reset_staking_status
from app.state import NodeStateMachine, NodeStatus


def test_sentinel_matches_status_default():
    """The reset sentinel mirrors the NodeStatus default (the em-dash)."""
    assert _STAKING_STATUS_SENTINEL == "—"
    assert _STAKING_STATUS_SENTINEL == NodeStatus().staking_status


def test_reset_blanks_stale_earning_value(settings):
    """A prior probe value is blanked back to the sentinel on register."""
    sm = NodeStateMachine()
    # Simulate a prior self-probe having written a real coord-side status.
    sm.status.staking_status = "earning"

    ctx = _NodeContext(settings, http_client=None)
    ctx.sm = sm

    _reset_staking_status(ctx)

    assert sm.status.staking_status == _STAKING_STATUS_SENTINEL


def test_reset_is_idempotent_when_already_sentinel(settings):
    """Resetting a fresh status object is a no-op (stays sentinel)."""
    sm = NodeStateMachine()
    assert sm.status.staking_status == _STAKING_STATUS_SENTINEL  # default

    ctx = _NodeContext(settings, http_client=None)
    ctx.sm = sm

    _reset_staking_status(ctx)

    assert sm.status.staking_status == _STAKING_STATUS_SENTINEL


def test_reset_tolerates_missing_state_machine(settings):
    """No state machine on the context → reset is a safe no-op (no raise)."""
    ctx = _NodeContext(settings, http_client=None)
    # ctx.sm defaults to None until _run() wires it up.
    assert ctx.sm is None

    _reset_staking_status(ctx)  # must not raise


def test_reset_only_touches_staking_status(settings):
    """Reset blanks staking_status without disturbing other coord fields."""
    sm = NodeStateMachine()
    sm.status.staking_status = "earning"
    sm.status.coord_status = "online"
    sm.status.coord_health_score = 0.9

    ctx = _NodeContext(settings, http_client=None)
    ctx.sm = sm

    _reset_staking_status(ctx)

    assert sm.status.staking_status == _STAKING_STATUS_SENTINEL
    # Adjacent coord-side fields are left for the next probe to refresh.
    assert sm.status.coord_status == "online"
    assert sm.status.coord_health_score == 0.9
