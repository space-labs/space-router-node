"""Coord-side probe state surfaces through ``Api.get_status()``.

These fields are populated by the daemon's ``_self_probe_loop`` to let the
GUI render a "Coord sees: offline · next probe in 47s" sub-line while the
local state machine stays in ``RUNNING``. Tests confirm the wiring:

- the five new keys are present in the dict ``get_status()`` returns;
- defaults (no probe yet) round-trip cleanly;
- a populated ``NodeStatus`` round-trips with the same values.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock


def _make_api_with_status(status):
    """Build an ``Api`` whose ``node_manager.status`` is the given object."""
    from gui.api import Api

    node = MagicMock()
    node.status = status
    # ``get_status`` reads ``_error_report_available`` directly off the node.
    node._error_report_available = False
    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    return Api(config=config, node_manager=node)


def test_get_status_exposes_coord_probe_fields_with_populated_values():
    """Operator-visible recovery state — populated case."""
    from app.state import NodeStatus

    next_probe = time.time() + 47
    ns = NodeStatus()
    ns.coord_status = "offline"
    ns.coord_health_score = 0.6
    ns.last_probe_outcome = "rate_limited"
    ns.last_probe_attempt_at = time.time() - 5
    ns.next_probe_attempt_at = next_probe

    api = _make_api_with_status(ns)
    payload = api.get_status()

    assert payload["coord_status"] == "offline"
    assert payload["coord_health_score"] == 0.6
    assert payload["last_probe_outcome"] == "rate_limited"
    assert payload["last_probe_attempt_at"] == ns.last_probe_attempt_at
    assert payload["next_probe_attempt_at"] == next_probe


def test_get_status_defaults_for_uninitialised_probe_state():
    """Before the first ``_self_probe_loop`` tick the fields stay at their
    documented defaults so the GUI can hide the sub-line cleanly."""
    from app.state import NodeStatus

    ns = NodeStatus()  # all defaults
    api = _make_api_with_status(ns)
    payload = api.get_status()

    assert payload["coord_status"] == "—"
    assert payload["coord_health_score"] == 0.0
    assert payload["last_probe_outcome"] is None
    assert payload["last_probe_attempt_at"] is None
    assert payload["next_probe_attempt_at"] is None


def test_get_status_includes_all_five_new_keys():
    """Schema check — the exact key names the JS layer reads must exist."""
    from app.state import NodeStatus

    api = _make_api_with_status(NodeStatus())
    payload = api.get_status()

    for key in (
        "coord_status",
        "coord_health_score",
        "last_probe_attempt_at",
        "last_probe_outcome",
        "next_probe_attempt_at",
    ):
        assert key in payload, f"{key} missing from get_status() payload"
