"""rc.6 BLK-4 + MAJ-5: Api.unlock_and_start() must reap orphan threads
and only return ok=True after the daemon has actually moved past
PASSPHRASE_REQUIRED.

Pre-rc.6 issues:
- ``is_running`` excluded PASSPHRASE_REQUIRED, so an alive-but-parked
  daemon thread was NOT cleaned up before the new ``start()`` call —
  the old thread was orphaned and the two raced on the listen port.
- ``unlock_and_start`` returned ok=True synchronously the moment
  ``start()`` spawned a new thread; the GUI hid the dialog, briefly
  showed the main screen, then the daemon's identity load failed and
  the dialog reappeared 1-3s later (jarring UX = MAJ-5).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from app.state import NodeState


def _make_api(node):
    """Build an ``Api`` whose node_manager is the given mock."""
    from gui.api import Api

    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    return Api(config=config, node_manager=node)


# ---------------------------------------------------------------------------
# NodeManager.has_live_thread() — helper for cleanup gate
# ---------------------------------------------------------------------------


def test_has_live_thread_true_when_thread_is_alive_in_passphrase_required():
    """Even when state == PASSPHRASE_REQUIRED (excluded by is_running), if
    a daemon thread is still alive ``has_live_thread`` must return True so
    cleanup paths reap it before spawning a replacement."""
    from gui.node_manager import NodeManager

    nm = NodeManager()

    # Build a fake live thread parked on a never-completing event.
    park = threading.Event()
    t = threading.Thread(target=park.wait, daemon=True, name="parked-test")
    t.start()
    try:
        nm._thread = t
        # Drive sm to PASSPHRASE_REQUIRED via legal path.
        nm._sm.transition(NodeState.INITIALIZING, "init")
        nm._sm.transition(NodeState.PASSPHRASE_REQUIRED, "needs unlock")

        assert nm.is_running is False  # PASSPHRASE_REQUIRED excluded
        assert nm.has_live_thread() is True  # but thread is alive
    finally:
        park.set()
        t.join(timeout=2.0)


def test_has_live_thread_false_when_no_thread():
    from gui.node_manager import NodeManager

    nm = NodeManager()
    assert nm.has_live_thread() is False


def test_has_live_thread_false_when_thread_finished():
    from gui.node_manager import NodeManager

    nm = NodeManager()
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    t.join()
    nm._thread = t
    assert nm.has_live_thread() is False


# ---------------------------------------------------------------------------
# Api.unlock_and_start() — reaps orphan thread, awaits state
# ---------------------------------------------------------------------------


def test_unlock_and_start_reaps_live_thread_even_when_not_running():
    """Pre-rc.6 the cleanup gate was ``is_running`` which is False in
    PASSPHRASE_REQUIRED — orphaning the live thread on the next start.
    After rc.6 the gate is ``has_live_thread``."""
    node = MagicMock()
    node.has_live_thread.return_value = True
    node.is_running = False  # excluded by PASSPHRASE_REQUIRED state
    # After start, sm is RUNNING — passphrase accepted.
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING

    api = _make_api(node)
    result = api.unlock_and_start("correct-passphrase")

    node.stop.assert_called_once()
    # Bounded timeout, not the default 20s (rc.6: don't hang the GUI).
    args, kwargs = node.stop.call_args
    timeout = kwargs.get("timeout", args[0] if args else None)
    assert timeout is not None and timeout < 20.0
    node.start.assert_called_once()
    assert result["ok"] is True


def test_unlock_and_start_skips_stop_when_no_live_thread():
    node = MagicMock()
    node.has_live_thread.return_value = False
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING

    api = _make_api(node)
    result = api.unlock_and_start("p")

    node.stop.assert_not_called()
    node.start.assert_called_once()
    assert result["ok"] is True


def test_unlock_and_start_returns_failure_when_state_stays_passphrase_required():
    """Wrong passphrase: daemon flips back to PASSPHRASE_REQUIRED. The
    API must surface ok=False so the GUI dialog can re-prompt instead of
    flashing the main screen for a few seconds (MAJ-5)."""
    node = MagicMock()
    node.has_live_thread.return_value = False
    node._sm = MagicMock()
    node._sm.state = NodeState.PASSPHRASE_REQUIRED

    api = _make_api(node)
    result = api.unlock_and_start("wrong-passphrase")

    assert result["ok"] is False
    assert result.get("error_code") == "PASSPHRASE_REQUIRED"


def test_unlock_and_start_polls_for_state_transition():
    """The poll loop should observe the state machine moving past
    IDLE/INITIALIZING. Simulate a delayed transition to RUNNING."""
    node = MagicMock()
    node.has_live_thread.return_value = False
    sm = MagicMock()
    # First few reads return INITIALIZING, then RUNNING — the poll
    # should detect the transition and return ok=True.
    states_iter = iter([
        NodeState.INITIALIZING,
        NodeState.INITIALIZING,
        NodeState.BINDING,  # past IDLE/INIT — accepted
    ])

    class _StateProp:
        def __get__(self, obj, objtype=None):
            return next(states_iter)

    type(sm).state = _StateProp()
    node._sm = sm

    api = _make_api(node)
    result = api.unlock_and_start("p")
    assert result["ok"] is True
    node.start.assert_called_once()


def test_unlock_and_start_sets_passphrase_env_var(monkeypatch):
    """The passphrase must end up in SR_IDENTITY_PASSPHRASE before start."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

    node = MagicMock()
    node.has_live_thread.return_value = False
    node._sm = MagicMock()
    node._sm.state = NodeState.RUNNING

    api = _make_api(node)
    api.unlock_and_start("hunter2")

    import os
    assert os.environ.get("SR_IDENTITY_PASSPHRASE") == "hunter2"


# ---------------------------------------------------------------------------
# Api.start_node() pre-flight passphrase gate (rc.5 → rc.7)
#
# rc.5 added a pre-flight gate to start_node that returns
# PASSPHRASE_REQUIRED without spawning the daemon thread when the
# keystore is encrypted but no passphrase is in env. This test pins the
# return contract so the rc.7 GUI fix (app.js consumes the early-return
# and pops the unlock dialog) cannot regress without us noticing —
# Woojung's rc.5/rc.6 hang was caused by the GUI not reading this
# response, leaving the spinner stuck on "Starting..." forever.
# ---------------------------------------------------------------------------


def test_start_node_returns_passphrase_required_when_keystore_encrypted_and_env_unset(monkeypatch):
    """Encrypted keystore + no SR_IDENTITY_PASSPHRASE → ok=False with
    error_code=PASSPHRASE_REQUIRED, and the daemon thread is NOT started."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    # Cached flag says keystore is encrypted; pre-flight reads this via
    # _load_settings_v2().wallet.identity_passphrase_set.
    settings_v2 = MagicMock()
    settings_v2.wallet.identity_passphrase_set = True
    config._load_settings_v2.return_value = settings_v2

    node = MagicMock()
    node.is_running = False
    node.has_live_thread.return_value = False

    from gui.api import Api
    api = Api(config=config, node_manager=node)

    result = api.start_node()

    assert result["ok"] is False
    assert result["error_code"] == "PASSPHRASE_REQUIRED"
    # CRITICAL: the daemon must not have been spawned. If it were, the
    # state machine would eventually surface PASSPHRASE_REQUIRED and the
    # GUI poll would fire showUnlockDialog — but with the early-return
    # gate the daemon stays IDLE, so the GUI must consume this response.
    node.start.assert_not_called()


def test_start_node_proceeds_when_keystore_encrypted_but_passphrase_in_env(monkeypatch):
    """If the operator already has SR_IDENTITY_PASSPHRASE set (e.g. via
    unlock_and_start a moment earlier), the pre-flight gate must NOT
    trip — start the daemon as usual."""
    monkeypatch.setenv("SR_IDENTITY_PASSPHRASE", "hunter2")

    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    settings_v2 = MagicMock()
    settings_v2.wallet.identity_passphrase_set = True
    config._load_settings_v2.return_value = settings_v2

    node = MagicMock()
    node.is_running = False
    node.has_live_thread.return_value = False

    from gui.api import Api
    api = Api(config=config, node_manager=node)

    result = api.start_node()

    assert result["ok"] is True
    node.start.assert_called_once()


def test_start_node_proceeds_when_keystore_plaintext(monkeypatch):
    """No encryption flag set → no passphrase gate. Daemon starts."""
    monkeypatch.delenv("SR_IDENTITY_PASSPHRASE", raising=False)

    config = MagicMock()
    config.get.return_value = None
    config.get_environment.return_value = "production"
    settings_v2 = MagicMock()
    settings_v2.wallet.identity_passphrase_set = False
    config._load_settings_v2.return_value = settings_v2

    node = MagicMock()
    node.is_running = False
    node.has_live_thread.return_value = False

    from gui.api import Api
    api = Api(config=config, node_manager=node)

    result = api.start_node()

    assert result["ok"] is True
    node.start.assert_called_once()
