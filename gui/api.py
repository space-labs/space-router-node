"""Python API exposed to the webview frontend via pywebview's js_api."""

import logging
import os

from app.variant import BUILD_VARIANT
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager

logger = logging.getLogger(__name__)


class Api:
    """Methods callable from JavaScript via ``window.pywebview.api.<method>()``."""

    def __init__(self, config: ConfigStore, node_manager: NodeManager) -> None:
        self._config = config
        self._node = node_manager

    def needs_onboarding(self) -> bool:
        return self._config.needs_onboarding()

    def save_onboarding_and_start(
        self,
        passphrase: str = "",
        staking: str = "",
        collection: str = "",
        identity_key_hex: str = "",
    ) -> dict:
        """Persist onboarding choices and start the node."""
        try:
            self._config.save_onboarding(
                passphrase=passphrase,
                staking=staking,
                collection=collection,
                identity_key_hex=identity_key_hex,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        self._config.apply_to_env()

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        return {"ok": True}

    def unlock_and_start(self, passphrase: str) -> dict:
        """Set the identity passphrase in env and (re)start the node.

        Called from the passphrase unlock dialog when the node cannot start
        because the keystore requires a passphrase that is not configured.
        """
        os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase

        if self._node.is_running:
            try:
                self._node.stop()
            except Exception as exc:
                logger.warning("Failed to stop node before unlock restart: %s", exc)

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node after unlock")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        return {"ok": True}

    def start_node(self) -> dict:
        """Start the node (config must already be set)."""
        if self._node.is_running:
            return {"ok": True, "message": "Already running"}

        self._config.apply_to_env()

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": str(exc)}

        return {"ok": True}

    def stop_node(self) -> dict:
        """Gracefully stop the node."""
        try:
            self._node.stop()
        except Exception as exc:
            logger.exception("Failed to stop node")
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_environments(self) -> list:
        """Return available environment presets."""
        from gui.config_store import ENVIRONMENTS
        current = self._config.get_environment()
        return [
            {"key": k, "label": v["label"], "url": v["url"], "active": k == current}
            for k, v in ENVIRONMENTS.items()
        ]

    def set_environment(self, env_key: str) -> dict:
        """Switch environment. Requires node restart to take effect."""
        try:
            url = self._config.save_environment(env_key)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "url": url}

    def retry_node(self) -> dict:
        """Retry from ERROR_PERMANENT without clearing config."""
        self._config.apply_to_env()
        try:
            self._node.retry()
        except Exception as exc:
            logger.exception("Failed to retry node")
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def get_status(self) -> dict:
        """Return current node status for the dashboard."""
        staking = self._config.get("SR_STAKING_ADDRESS")
        collection = self._config.get("SR_COLLECTION_ADDRESS")
        env = self._config.get_environment()
        api_url = self._config.get("SR_COORDINATION_API_URL")
        ns = self._node.status
        return {
            # New state machine fields
            "state": ns.state.value,
            "detail": ns.detail,
            "error_code": ns.error_code,
            "retry_count": ns.retry_count,
            "next_retry_at": ns.next_retry_at,
            "node_id": ns.node_id,
            "cert_expiry_warning": ns.cert_expiry_warning,
            # Backward-compatible fields
            "running": self._node.is_running,
            "phase": self._node.phase,
            "staking_address": staking,
            "collection_address": collection or staking,
            "wallet": staking,
            "staking": staking,
            "error": ns.error_message,
            "environment": env,
            "api_url": api_url,
        }

    def get_build_variant(self) -> str:
        """Return 'test' or 'production'."""
        return BUILD_VARIANT

    def get_settings(self) -> dict:
        """Return current settings for the settings panel."""
        return {
            "coordination_api_url": self._config.get(
                "SR_COORDINATION_API_URL",
                "https://spacerouter-coordination-api.fly.dev",
            ),
            "mtls_enabled": self._config.get("SR_MTLS_ENABLED", "true").lower() == "true",
        }

    def save_settings(self, coordination_api_url: str, mtls_enabled: bool) -> dict:
        """Save advanced settings. Requires node restart to take effect."""
        try:
            self._config.save_settings(coordination_api_url, mtls_enabled)
            return {"ok": True, "restart_required": True}
        except Exception as exc:
            logger.exception("Failed to save settings")
            return {"ok": False, "error": str(exc)}

    def get_network_mode(self) -> dict:
        """Return current network mode (upnp or tunnel)."""
        return self._config.get_network_mode()

    def save_network_mode(self, mode: str, public_host: str = "", port: str = "") -> dict:
        """Save network mode. Requires node restart."""
        try:
            self._config.save_network_mode(mode, public_host, port)
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to save network mode")
            return {"ok": False, "error": str(exc)}

    def fresh_restart(self, keep_addresses: bool = False) -> dict:
        """Stop node, reset config, return to onboarding.

        Uses a short timeout — if the node is stuck (e.g. in a registration
        loop), we force-proceed rather than blocking the UI.

        Args:
            keep_addresses: if True, preserves staking/collection addresses.
        """
        import os
        try:
            self._node.stop(timeout=5.0)
        except Exception:
            logger.warning("Node stop timed out during fresh restart — proceeding anyway")

        try:
            self._config.reset(
                keep_addresses=keep_addresses,
                keep_identity=keep_addresses,  # "Clear Everything" also removes identity key
            )
            # Clear env vars so next start picks up fresh config
            for key in list(os.environ.keys()):
                if key.startswith("SR_"):
                    del os.environ[key]
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to fresh restart")
            return {"ok": False, "error": str(exc)}
