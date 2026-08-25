"""The GUI status payload must carry every field the dashboard reads.

`gui/api.py:get_status()` hand-rolled its own dict instead of deriving from
`NodeStatus.to_dict()`, and drifted from what `gui/assets/app.js` actually
reads. Three consequences QA hit on v1.5.2-test.136:

- `error_message` was never emitted, so all 16 `status.error_message || "canned
  text"` expressions in app.js silently fell through to the canned string. For
  endpoint_unreachable the operator always saw "Coordination server cannot
  reach this node" and never the server's real reason (connection_refused vs
  timed out), which is the whole diagnostic. QA had to read backend logs to
  learn their port was refused.
- It also caused the duplicated retry text, because app.js falls back to
  `status.detail`, which the state machine has ALREADY suffixed with
  "(Attempt N, retry in Xs)", and then appends its own suffix.
- `identity_address` was set to `ns.node_id`, so the Identity chip showed the
  node UUID instead of an EVM address.
- `rpc_status` / `rpc_status_detail` were never emitted, making the rc.11 #7
  "RPC unreachable" hint unreachable code.

This test pins the contract in the direction that drifted: every key app.js
reads off `status` must be present in the payload.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _payload_keys() -> set[str]:
    """Keys get_status() actually emits, by calling it with stubs.

    Behavioural rather than source-parsing: the payload is assembled partly by
    spreading NodeStatus.to_dict(), which no regex over the source can see.
    """
    from app.state import NodeState, NodeStatus
    from gui.api import Api

    class _Node:
        status = NodeStatus(state=NodeState.IDLE)
        is_running = False
        phase = "idle"
        _error_report_available = False

    class _Config:
        def get(self, *_a, **_k):
            return ""

        def get_environment(self):
            return "test"

    api = Api.__new__(Api)
    api._node = _Node()
    api._config = _Config()
    api._get_version_check_dict = lambda: None
    return set(api.get_status().keys())


def _keys_app_js_reads() -> set[str]:
    js = (ROOT / "gui" / "assets" / "app.js").read_text()
    return set(re.findall(r"\bstatus\.([a-z_0-9]+)\b", js))


def test_payload_contains_every_key_the_dashboard_reads():
    emitted = _payload_keys()
    read = _keys_app_js_reads()
    missing = sorted(read - emitted)
    assert not missing, (
        f"gui/api.py:get_status() does not emit {missing}, but app.js reads "
        f"them off `status`. Every `status.X || 'canned'` for a missing X "
        f"silently renders the canned text."
    )


def test_error_message_carries_the_clean_message_not_the_suffixed_detail():
    """error_message must not be the attempt-suffixed detail string."""
    from app.errors import NodeError, NodeErrorCode
    from app.state import NodeState, NodeStateMachine

    sm = NodeStateMachine()
    sm.transition(NodeState.INITIALIZING)
    sm.transition(NodeState.BINDING)
    sm.transition(NodeState.REGISTERING)
    sm.handle_error(
        NodeError(NodeErrorCode.ENDPOINT_UNREACHABLE, "connection_refused"),
        NodeState.REGISTERING,
    )
    st = sm.status

    assert "Attempt" in (st.detail or ""), "expected detail to carry the attempt suffix"
    assert "Attempt" not in (st.error_message or ""), (
        "error_message now carries the attempt suffix; the dashboard appends "
        "its own, which is what produced the duplicated retry text"
    )


def test_identity_address_is_not_the_node_uuid():
    src = (ROOT / "gui" / "api.py").read_text()
    assert not re.search(r'"identity_address":\s*ns\.node_id', src), (
        "identity_address is assigned the node UUID, so the Identity chip "
        "shows a UUID instead of an EVM address"
    )


def test_dashboard_does_not_fall_back_to_the_uuid_for_identity():
    """A UUID in an 'Identity' chip is worse than showing nothing."""
    js = (ROOT / "gui" / "assets" / "app.js").read_text()
    assert not re.search(r"status\.identity_address\s*\|\|\s*status\.node_id", js), (
        "app.js still falls back to status.node_id for the identity chip, "
        "which renders a UUID where an EVM address belongs"
    )
