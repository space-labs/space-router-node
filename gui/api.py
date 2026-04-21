"""Python API exposed to the webview frontend via pywebview's js_api."""

import asyncio
import logging
import os
import threading
import time
import uuid as uuid_mod

from dotenv import set_key

from app.variant import BUILD_VARIANT
from app.version import __version__
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager

logger = logging.getLogger(__name__)


class _ClaimTaskRegistry:
    """In-memory registry for background claim/retry tasks.

    The GUI fires ``receipts_claim_all`` / ``receipts_retry`` which
    return immediately with a ``task_id``. The JS side polls
    ``receipts_claim_status(task_id)`` until the task completes. A
    file lock (``~/.spacerouter/claim.lock``) serialises real claim
    work across CLI, GUI, and accidental double-clicks, so only one
    claim tx runs at any moment.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, runner) -> str:
        task_id = uuid_mod.uuid4().hex
        with self._lock:
            self._tasks[task_id] = {
                "state": "queued", "started_at": time.time(),
                "result": None, "error": None,
            }

        def _run():
            try:
                with self._lock:
                    self._tasks[task_id]["state"] = "running"
                result = runner()
                with self._lock:
                    self._tasks[task_id]["state"] = "done"
                    self._tasks[task_id]["result"] = result
            except Exception as exc:
                logger.exception("Claim task %s failed", task_id)
                with self._lock:
                    self._tasks[task_id]["state"] = "error"
                    self._tasks[task_id]["error"] = str(exc)

        threading.Thread(target=_run, daemon=True).start()
        return task_id

    def status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return dict(task)

    def gc(self, max_age_seconds: int = 3600) -> None:
        """Drop tasks older than max_age to keep the map bounded."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            stale = [
                tid for tid, t in self._tasks.items()
                if t.get("started_at", 0) < cutoff
                and t["state"] in ("done", "error")
            ]
            for tid in stale:
                del self._tasks[tid]


_claim_tasks = _ClaimTaskRegistry()


def _run_async(coro):
    """Run a coroutine to completion from a sync pywebview-API method.

    Uses a fresh event loop per call — these methods are cheap DB queries
    so the overhead is negligible and avoids cross-loop issues with the
    provider's main event loop.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
        referral_code: str = "",
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

        if referral_code and not self._config.get("SR_REFERRAL_CODE"):
            set_key(str(self._config.path), "SR_REFERRAL_CODE", referral_code)

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
        """Return available environment presets (test builds only)."""
        if BUILD_VARIANT != "test":
            return []
        from gui.config_store import ENVIRONMENTS
        current = self._config.get_environment()
        return [
            {"key": k, "label": v["label"], "url": v["url"], "active": k == current}
            for k, v in ENVIRONMENTS.items()
        ]

    def set_environment(self, env_key: str) -> dict:
        """Switch environment. Requires node restart to take effect (test builds only)."""
        if BUILD_VARIANT != "test":
            return {"ok": False, "error": "Environment switching is disabled in production builds."}
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
            "staking_status": ns.staking_status,
            # Error reporting
            "error_report_available": self._node._error_report_available,
            # Version check
            "version_check": self._get_version_check_dict(),
        }

    def _get_version_check_dict(self) -> dict | None:
        """Build version check dict for status payload."""
        vc = self._node.version_check
        if vc is None:
            return None
        return {
            "status": vc.status,
            "latest_version": vc.latest_version,
            "min_version": vc.min_version,
            "download_url": vc.download_url,
            "current_version": vc.current_version,
        }

    def get_build_version(self) -> str:
        """Return the build version string."""
        return __version__

    def get_build_variant(self) -> str:
        """Return 'test' or 'production'."""
        return BUILD_VARIANT

    def send_error_report(self) -> dict:
        """Build, sign, and send the current error report to coordination API."""
        return self._node.send_error_report()

    def get_settings(self) -> dict:
        """Return current settings for the settings panel."""
        from gui.config_store import _default_coordination_url
        return {
            "coordination_api_url": self._config.get(
                "SR_COORDINATION_API_URL",
                _default_coordination_url(),
            ),
            "mtls_enabled": self._config.get("SR_MTLS_ENABLED", "true").lower() == "true",
        }

    def save_settings(self, coordination_api_url: str, mtls_enabled: bool) -> dict:
        """Save advanced settings. Requires node restart to take effect (test builds only)."""
        if BUILD_VARIANT != "test":
            return {"ok": False, "error": "Settings are locked in production builds."}
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

    def open_url(self, url: str):
        """Open a URL in the user's default browser."""
        import webbrowser
        webbrowser.open(url)

    def get_min_staking_amount(self) -> int:
        """Fetch minimum staking amount from coordination API /config endpoint."""
        import httpx
        from gui.config_store import _default_coordination_url
        api_url = self._config.get("SR_COORDINATION_API_URL") or _default_coordination_url()
        try:
            resp = httpx.get(f"{api_url}/config", timeout=5)
            resp.raise_for_status()
            return resp.json().get("minimumStakingAmount", 1)
        except Exception:
            return 1

    # ── Leg 2 receipts / earnings ──────────────────────────────────

    def receipts_summary(self) -> dict:
        """Cheap counts + claimable SPACE total. Called on status poll.

        Returns ``{summary, escrow_configured}`` where ``summary`` is
        the raw per-view counts and ``escrow_configured`` tells the UI
        whether claim actions are available.
        """
        from app.main import load_settings
        from app.payment.receipt_store import get_store

        try:
            settings = load_settings()
        except Exception as exc:
            return {"ok": False, "error": f"config unavailable: {exc}"}

        async def _go():
            store = get_store(settings.RECEIPT_STORE_PATH)
            await store.initialize()
            return await store.summary()

        try:
            summary = _run_async(_go())
        except Exception as exc:
            logger.exception("receipts_summary failed")
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "summary": summary,
            "escrow_configured": bool(
                settings.ESCROW_CHAIN_RPC
                and settings.ESCROW_CONTRACT_ADDRESS
            ),
        }

    def receipts_list(
        self, view: str = "all", limit: int = 100, offset: int = 0,
    ) -> dict:
        from app.main import load_settings, _receipt_to_json
        from app.payment.receipt_store import get_store

        settings = load_settings()

        async def _go():
            store = get_store(settings.RECEIPT_STORE_PATH)
            await store.initialize()
            rows = await store.list_by_view(
                view=view, limit=int(limit), offset=int(offset),
            )
            summary = await store.summary()
            return summary, rows

        try:
            summary, rows = _run_async(_go())
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("receipts_list failed")
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "view": view,
            "summary": summary,
            "receipts": [_receipt_to_json(sr) for sr in rows],
        }

    def receipts_detail(self, request_uuid: str) -> dict:
        from app.main import load_settings, _receipt_to_json
        from app.payment.receipt_store import get_store

        settings = load_settings()

        async def _go():
            store = get_store(settings.RECEIPT_STORE_PATH)
            await store.initialize()
            return await store.get_by_uuid(request_uuid)

        try:
            sr = _run_async(_go())
        except Exception as exc:
            logger.exception("receipts_detail failed")
            return {"ok": False, "error": str(exc)}

        if sr is None:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "receipt": _receipt_to_json(sr)}

    def receipts_claim_all(self) -> dict:
        """Kick off a claim-all task in the background.

        Returns a ``task_id`` the UI polls via ``receipts_claim_status``.
        Serialised across CLI / GUI via a file lock in the runner.
        """
        _claim_tasks.gc()
        try:
            task_id = _claim_tasks.start(lambda: _claim_runner(None, False))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "task_id": task_id}

    def receipts_retry(self, request_uuid: str) -> dict:
        """Retry a single receipt. No-op (``noop=True``) on locked / claimed."""
        from app.main import load_settings
        from app.payment.receipt_store import get_store

        settings = load_settings()

        async def _peek():
            store = get_store(settings.RECEIPT_STORE_PATH)
            await store.initialize()
            return await store.get_by_uuid(request_uuid)

        try:
            sr = _run_async(_peek())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if sr is None:
            return {"ok": False, "error": "not_found"}
        if sr.claimed_at is not None:
            return {"ok": True, "noop": True, "reason": "already_claimed"}
        if sr.locked:
            return {"ok": True, "noop": True, "reason": "locked"}

        task_id = _claim_tasks.start(
            lambda: _claim_runner(request_uuid, True),
        )
        return {"ok": True, "task_id": task_id}

    def receipts_claim_status(self, task_id: str) -> dict:
        task = _claim_tasks.status(task_id)
        if task is None:
            return {"ok": False, "error": "unknown_task"}
        return {"ok": True, **task}

    def receipts_open_explorer(self, tx_hash: str) -> dict:
        """Open blockscout for the active escrow chain at a tx hash."""
        import webbrowser
        from app.main import load_settings

        settings = load_settings()
        chain_id = getattr(settings, "ESCROW_CHAIN_ID", 0)
        # cc3 testnet = 102031. Mainnet creditcoin = 102030. Fall back
        # to the testnet explorer for unknown chains (test env default).
        if chain_id == 102030:
            base = "https://creditcoin.blockscout.com/tx/"
        else:
            base = "https://creditcoin-testnet.blockscout.com/tx/"
        try:
            webbrowser.open(base + tx_hash)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fresh_restart(self) -> dict:
        """Stop node, fully reset config and identity, return to onboarding.

        Uses a short timeout — if the node is stuck (e.g. in a registration
        loop), we force-proceed rather than blocking the UI.
        """
        import os
        try:
            self._node.stop(timeout=5.0)
        except Exception:
            logger.warning("Node stop timed out during fresh restart — proceeding anyway")

        try:
            self._config.reset()
            # Clear env vars so next start picks up fresh config
            for key in list(os.environ.keys()):
                if key.startswith("SR_"):
                    del os.environ[key]
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to fresh restart")
            return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────
# Background claim runner — called from _ClaimTaskRegistry.start()
# ──────────────────────────────────────────────────────────────────


def _claim_runner(only_uuid: str | None, include_retryable: bool) -> dict:
    """Background claim job.

    Serialised across CLI / GUI / double-clicks via a ``fcntl.flock``
    on ``~/.spacerouter/claim.lock``. If the lock is already held,
    returns ``{noop: True}`` so the UI stays calm rather than showing
    an error when a second concurrent click comes in.
    """
    import fcntl
    from pathlib import Path

    from app.main import load_settings
    from app.payment.settlement import claim_all
    from app.identity import load_or_create_identity, KeystorePassphraseRequired

    settings = load_settings()
    lock_path = Path(settings.RECEIPT_STORE_PATH).expanduser().parent / "claim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"noop": True, "reason": "claim_in_progress"}

        # Use identity key as settlement key unless operator overrides.
        settlement_key = os.environ.get("SR_SETTLEMENT_KEY", "")
        if not settlement_key:
            try:
                identity_key, _ = load_or_create_identity(
                    settings.IDENTITY_KEY_PATH, settings.IDENTITY_PASSPHRASE,
                )
                settlement_key = (
                    identity_key if identity_key.startswith("0x")
                    else "0x" + identity_key
                )
            except KeystorePassphraseRequired:
                return {
                    "ok": False,
                    "error": "Identity key is encrypted. Set a passphrase "
                             "and restart before claiming.",
                }

        try:
            results = _run_async(claim_all(
                settings, settlement_key,
                include_retryable=include_retryable,
                only_uuids=[only_uuid] if only_uuid else None,
            ))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        summary = {
            "batches": len(results),
            "submitted": sum(r.submitted for r in results),
            "reconciled": sum(r.skipped_as_already_claimed for r in results),
            "failed_batches": sum(1 for r in results if r.error),
            "locked_after_failure": sum(r.locked_after_failure for r in results),
            "tx_hashes": [r.tx_hash for r in results if r.tx_hash],
            "reasons": [r.reason_code for r in results if r.reason_code],
        }
        return {"ok": True, "summary": summary}
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()
