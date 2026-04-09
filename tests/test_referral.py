"""Tests for the referral-code save/overwrite guard in gui.api.Api."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gui.api import Api


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

    @patch("gui.api.set_key")
    def test_referral_saved_when_none_exists(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="partner-1")

        assert result == {"ok": True}
        mock_set_key.assert_called_once_with(
            str(mock_config.path), "SR_REFERRAL_CODE", "partner-1"
        )

    @patch("gui.api.set_key")
    def test_referral_not_overwritten_when_exists(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = "original-code"

        result = api.save_onboarding_and_start(referral_code="new-code")

        assert result == {"ok": True}
        mock_set_key.assert_not_called()

    @patch("gui.api.set_key")
    def test_empty_referral_does_not_write(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="")

        assert result == {"ok": True}
        mock_set_key.assert_not_called()

    @patch("gui.api.set_key")
    def test_referral_saved_on_fresh_setup(self, mock_set_key, api, mock_config):
        mock_config.get.return_value = ""

        result = api.save_onboarding_and_start(referral_code="first-code")

        assert result == {"ok": True}
        mock_set_key.assert_called_once_with(
            str(mock_config.path), "SR_REFERRAL_CODE", "first-code"
        )
