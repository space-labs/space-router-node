"""Tests for the referral-code save/overwrite guard in gui.api.Api.

The gui.api module transitively imports modules using Python 3.10+ syntax
(``int | None``).  On older interpreters we pre-inject lightweight stubs
into ``sys.modules`` so that the import chain succeeds without pulling in
the real heavy dependencies.
"""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out transitive imports that use 3.10+ syntax (int | None)
# ---------------------------------------------------------------------------
_STUBS: dict = {}


def _ensure_stub(name: str, is_package: bool = False) -> ModuleType:
    """Create a stub module and register it in sys.modules."""
    if name not in sys.modules:
        mod = ModuleType(name)
        mod.__dict__.setdefault("__all__", [])
        if is_package:
            mod.__path__ = []  # marks it as a package
        sys.modules[name] = mod
        _STUBS[name] = mod
    return sys.modules[name]


# Ensure parent packages exist first (they need __path__ pointing to real dirs
# so that submodule imports like ``gui.api`` can find the actual source files).
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_ensure_stub("app", is_package=True).__path__ = [str(Path(_PROJECT_ROOT) / "app")]
_ensure_stub("gui", is_package=True).__path__ = [str(Path(_PROJECT_ROOT) / "gui")]

# Stub the problematic leaf modules that use 3.10+ syntax
for _mod_name in (
    "app.identity",
    "app.wallet",
    "app.variant",
    "app.tls",
    "gui.config_store",
    "gui.node_manager",
):
    stub = _ensure_stub(_mod_name)
    if _mod_name == "app.variant":
        stub.BUILD_VARIANT = "test"
    if _mod_name == "gui.config_store":
        stub.ConfigStore = MagicMock
    if _mod_name == "gui.node_manager":
        stub.NodeManager = MagicMock

# NOW we can safely import the module under test
from gui.api import Api  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_config():
    """Return a mock ConfigStore with sensible defaults."""
    cfg = MagicMock()
    cfg.path = Path("/tmp/fake/spacerouter.env")
    cfg.save_onboarding.return_value = None
    cfg.apply_to_env.return_value = None
    return cfg


@pytest.fixture()
def mock_node():
    """Return a mock NodeManager whose start() succeeds."""
    node = MagicMock()
    node.start.return_value = None
    return node


@pytest.fixture()
def api(mock_config, mock_node):
    return Api(config=mock_config, node_manager=mock_node)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReferralSaveGuard:
    """Verify that the referral code is only persisted when none already exists."""

    @patch("dotenv.set_key")
    def test_referral_saved_when_none_exists(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="partner-1")

        assert result == {"ok": True}
        mock_set_key.assert_called_once_with(
            str(mock_config.path), "SR_REFERRAL_CODE", "partner-1"
        )

    @patch("dotenv.set_key")
    def test_referral_not_overwritten_when_exists(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = "original-code"

        result = api.save_onboarding_and_start(referral_code="new-code")

        assert result == {"ok": True}
        mock_set_key.assert_not_called()

    @patch("dotenv.set_key")
    def test_empty_referral_does_not_write(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="")

        assert result == {"ok": True}
        mock_set_key.assert_not_called()

    @patch("dotenv.set_key")
    def test_referral_saved_on_fresh_setup(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="first-code")

        assert result == {"ok": True}
        mock_set_key.assert_called_once_with(
            str(mock_config.path), "SR_REFERRAL_CODE", "first-code"
        )
