"""Python API exposed to the webview frontend via pywebview's js_api."""

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid as uuid_mod
from pathlib import Path


from app.state import NodeState
from app.variant import BUILD_VARIANT
from app.version import __version__
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Error catalog — daemon-side detection rules → stable error codes.
#
# The frontend (app.js) keeps the human strings; we only emit the code
# and any parameters needed for parameterised messages. This keeps the
# wording editable without a Python redeploy.
#
# Detection order matters: more specific patterns win. Each rule is a
# (regex, code, params_extractor_or_None) triple. Patterns are matched
# against the lowercased exception text plus its type name.
# ────────────────────────────────────────────────────────────────────

ERR_INSUFFICIENT_GAS = "insufficient_gas"
ERR_COORD_UNREACHABLE = "coord_unreachable"
ERR_CHAIN_RPC_UNREACHABLE = "chain_rpc_unreachable"
ERR_IDENTITY_KEY_MISSING = "identity_key_missing"
ERR_RATE_MISMATCH = "rate_mismatch"
ERR_RECEIPT_DB_LOCKED = "receipt_db_locked"
ERR_STAKE_NOT_APPROVED = "stake_not_approved"
ERR_DISK_FULL = "disk_full"
ERR_UPNP_NAT_BLOCKED = "upnp_nat_blocked"
ERR_SLEEP_RESUME = "sleep_resume"
ERR_UNKNOWN = "unknown"


_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Web3.py raises with "insufficient funds for gas" / "intrinsic gas too low"
    (re.compile(r"insufficient funds for gas|intrinsic gas too low|insufficient funds.*intrinsic"), ERR_INSUFFICIENT_GAS),
    # Coordination API connectivity — distinct from the chain RPC. The
    # daemon's reconnect loop logs "coordination api" or the URL fragment.
    (re.compile(r"coordination[- ]api|coord(ination)?\s+(api\s+)?unreachable|spacerouter-coordination-api"), ERR_COORD_UNREACHABLE),
    # Chain RPC connectivity — web3.py + httpx errors mention "rpc" or the
    # creditcoin RPC host.
    (re.compile(r"chain rpc|rpc.*unreachable|rpc endpoint|creditcoin.*rpc|cc3-testnet|max retries.*rpc"), ERR_CHAIN_RPC_UNREACHABLE),
    # Identity key file not found / corrupt
    (re.compile(r"identity key.*(not found|missing|corrupt)|node-identity\.key.*(not found|missing)|cannot load identity"), ERR_IDENTITY_KEY_MISSING),
    # Rate config out of sync (gateway returns rate, we have different)
    (re.compile(r"rate.*(mismatch|out of sync|differs)|node_rate_per_gb.*mismatch"), ERR_RATE_MISMATCH),
    # SQLite locked
    (re.compile(r"database is locked|sqlite.*locked|disk i/o error"), ERR_RECEIPT_DB_LOCKED),
    # Stake not yet approved by gateway
    (re.compile(r"stake.*(not.*approved|awaiting approval|pending approval)|registration.*pending"), ERR_STAKE_NOT_APPROVED),
    # Disk full
    (re.compile(r"no space left on device|disk full|enospc"), ERR_DISK_FULL),
    # UPnP failure with NAT-blocked outcome
    (re.compile(r"upnp.*(failed|unavailable|blocked|nat)|cannot detect public ip"), ERR_UPNP_NAT_BLOCKED),
]


def classify_error_text(text: str) -> str:
    """Map an exception/log text to a stable error code.

    Returns ``ERR_UNKNOWN`` if no pattern matches. The frontend renders
    a generic fallback in that case.
    """
    if not text:
        return ERR_UNKNOWN
    haystack = text.lower()
    for pattern, code in _ERROR_PATTERNS:
        if pattern.search(haystack):
            return code
    return ERR_UNKNOWN


# ────────────────────────────────────────────────────────────────────
# Incident store — persists across GUI restarts.
#
# The auto-claim failure UX (S3-c) needs the banner to survive a
# restart so an operator who closed the GUI mid-failure isn't blind on
# next launch. One file, one JSON list, capped at 50 entries.
# ────────────────────────────────────────────────────────────────────


def _incidents_path() -> Path:
    from app.paths import config_dir
    return config_dir() / "incidents.json"


def _incidents_load() -> list[dict]:
    try:
        path = _incidents_path()
        if not path.is_file():
            return []
        return json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def _incidents_save(items: list[dict]) -> None:
    try:
        path = _incidents_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Cap to last 50 to keep the file bounded.
        items = items[-50:]
        path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not write incidents.json: %s", e)


def _incident_record(kind: str, *, code: str = "", message: str = "", **extra) -> None:
    """Append one incident entry. Called from the daemon side; the GUI
    polls via ``get_incidents`` and shows a banner for the most recent
    unacknowledged entry.
    """
    items = _incidents_load()
    items.append({
        "id": uuid_mod.uuid4().hex,
        "kind": kind,
        "code": code,
        "message": message,
        "at": int(time.time()),
        "acknowledged": False,
        **extra,
    })
    _incidents_save(items)


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


def _claim_wallet_address(settings) -> str | None:
    """Best-effort lookup of the identity wallet that broadcasts claimBatch.

    Returns a checksummed 0x... address, or None if the keystore is
    encrypted (and we don't have the passphrase) or the file is missing.
    Never raises — the receipts_summary status poll fires every ~10s
    and any noise here would dominate the log.

    The identity wallet is auto-derived from
    ``~/.spacerouter/certs/node-identity.key``; users have no obvious
    way to discover it without us surfacing it. test.105's
    "Claim All silently failed" was caused by that wallet having 0 CTC
    for gas and the user not knowing where to send funds.
    """
    try:
        from app.identity import (
            load_or_create_identity,
            KeystorePassphraseRequired,
        )
        _, address = load_or_create_identity(
            settings.IDENTITY_KEY_PATH,
            settings.IDENTITY_PASSPHRASE,
        )
        return address
    except KeystorePassphraseRequired:
        return None
    except Exception:  # noqa: BLE001
        logger.debug("claim wallet address lookup failed", exc_info=True)
        return None


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
            self._config._set_field("SR_REFERRAL_CODE", referral_code)

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

        rc.6 BLK-4 + MAJ-5: after a wrong passphrase the daemon thread is
        still alive but the state machine is parked in PASSPHRASE_REQUIRED.
        ``is_running`` reports False (PASSPHRASE_REQUIRED is not in the
        operational set), so the previous implementation skipped ``stop()``
        — orphaning the old thread when it called ``start()``. Two daemon
        threads then raced on the listen port. We use ``has_live_thread``
        to reap orphan threads regardless of state, with a bounded stop
        timeout so the GUI button doesn't hang.

        Also: after kicking the new ``start()``, poll the state machine
        briefly so we only return ok=True once the daemon has actually
        moved past PASSPHRASE_REQUIRED. If it stays put (e.g. wrong
        passphrase again) we surface ok=False so the dialog can re-prompt
        instead of flashing the main screen for a few seconds.
        """
        os.environ["SR_IDENTITY_PASSPHRASE"] = passphrase

        # Cleanup gate uses has_live_thread, not is_running — covers the
        # PASSPHRASE_REQUIRED / ERROR_TRANSIENT orphan-thread paths.
        if self._node.has_live_thread():
            try:
                # Bounded timeout — the stuck thread is the common path
                # here, so don't make the GUI wait the default 20s.
                self._node.stop(timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to stop node before unlock restart: %s", exc,
                )

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node after unlock")
            return {"ok": False, "error": f"Failed to start node: {exc}"}

        # Wait for the state machine to confirm it's past
        # IDLE/PASSPHRASE_REQUIRED. If the wrong passphrase is supplied,
        # the daemon will flip back to PASSPHRASE_REQUIRED — return
        # ok=False so the GUI can re-enable the unlock button.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = self._node._sm.state
            if state == NodeState.PASSPHRASE_REQUIRED:
                return {
                    "ok": False,
                    "error_code": "PASSPHRASE_REQUIRED",
                    "error": "Passphrase still required — wrong passphrase?",
                }
            if state not in (NodeState.IDLE, NodeState.INITIALIZING):
                # Daemon advanced beyond identity load — passphrase accepted.
                return {"ok": True}
            time.sleep(0.05)

        # Timed out before a definitive transition. If we're still in
        # PASSPHRASE_REQUIRED at this point, treat that as failure.
        if self._node._sm.state == NodeState.PASSPHRASE_REQUIRED:
            return {
                "ok": False,
                "error_code": "PASSPHRASE_REQUIRED",
                "error": "Passphrase still required — wrong passphrase?",
            }
        return {"ok": True}

    def start_node(self) -> dict:
        """Start the node (config must already be set).

        rc.5 MAJ — passphrase prompt order: if the keystore is encrypted
        (``identity_passphrase_set=True``) but no passphrase is in the
        environment yet, refuse to start and tell the GUI to render the
        unlock dialog. Pre-rc.5 the start kicked off, the daemon's
        identity load failed inside the node thread, the state machine
        eventually surfaced PASSPHRASE_REQUIRED — but in the meantime
        the GUI had already displayed the staking-required modal,
        confusing operators about the actual order of operations.
        """
        if self._node.is_running:
            return {"ok": True, "message": "Already running"}

        self._config.apply_to_env()

        # Pre-flight passphrase gate. The cached ``identity_passphrase_set``
        # boolean was reconciled against the on-disk keystore by
        # ``ConfigStore.__init__`` so we can trust it here.
        try:
            settings_v2 = self._config._load_settings_v2()
            needs_passphrase = bool(
                settings_v2.wallet.identity_passphrase_set
            ) and not os.environ.get("SR_IDENTITY_PASSPHRASE")
        except Exception:  # noqa: BLE001
            needs_passphrase = False

        if needs_passphrase:
            return {
                "ok": False,
                "error": "passphrase required",
                "error_code": "PASSPHRASE_REQUIRED",
            }

        # rc.8 MAJ-6: belt-and-braces. The macOS GUI runs Stop→Start in
        # the same pywebview Python process, so the receipt_store
        # singleton (and its async-sqlite connection bound to the prior
        # event loop's thread) can survive across the restart. Drop it
        # here so the next ``initialize()`` rebinds to the new loop.
        # See PR #108 diagnostic.
        try:
            from app.payment import receipt_store as _rs
            _rs.clear_singleton()
        except Exception:  # noqa: BLE001
            pass

        try:
            self._node.start()
        except Exception as exc:
            logger.exception("Failed to start node")
            return {"ok": False, "error": str(exc)}

        return {"ok": True}

    def stop_node(self) -> dict:
        """Gracefully stop the node.

        G5 fix: when the user requests a stop, the staking_status must
        flip to ``"—"`` immediately rather than continuing to read
        "earning" until the next coordination poll. The state machine
        already sets ``state=stopping`` synchronously inside ``stop()``,
        but the staking_status field is only refreshed by the periodic
        registration poll. We blank it here so the GUI shows a coherent
        "stopped" view as soon as the click is processed.
        """
        try:
            # Blank the cached staking_status synchronously — the
            # subsequent get_status() call from the GUI poll will see
            # this value rather than the stale "earning" left over from
            # the running state.
            try:
                self._node.status.staking_status = "—"
            except Exception:  # noqa: BLE001
                pass
            self._node.stop()
        except Exception as exc:
            logger.exception("Failed to stop node")
            return {"ok": False, "error": str(exc)}

        # rc.8 MAJ-6: clear the receipt_store singleton so a subsequent
        # Save & Restart in the same pywebview process gets a fresh
        # ReceiptStore. Without this, the cached singleton's async-sqlite
        # connection — opened on the previous loop's thread — survives
        # into the next start and triggers a thread-affinity error,
        # parking the daemon in ERROR before reaching EARNING. Same
        # singleton-survives-the-cycle bug class as rc.6 BLK-2 (Reset
        # path); different trigger. See PR #108 diagnostic.
        try:
            from app.payment import receipt_store as _rs
            _rs.clear_singleton()
        except Exception:  # noqa: BLE001
            # Idempotent helper — best-effort. A failure to import
            # receipt_store here would be surprising but must not
            # propagate out of stop_node().
            logger.debug("receipt_store.clear_singleton failed", exc_info=True)
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
            # rc.5 F2: surface the identity address explicitly so the
            # GUI's Wallet panel doesn't have to know that node_id IS
            # the identity address (a coincidence of the architecture).
            "identity_address": ns.node_id,
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
            # Coord-side probe state (populated by daemon's self-probe loop)
            "coord_status": ns.coord_status,
            "coord_health_score": ns.coord_health_score,
            "last_probe_attempt_at": ns.last_probe_attempt_at,
            "last_probe_outcome": ns.last_probe_outcome,
            "next_probe_attempt_at": ns.next_probe_attempt_at,
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

    def get_staking_address(self) -> str:
        """Return the currently configured staking address (or empty string)."""
        return self._config.get("SR_STAKING_ADDRESS") or ""

    def save_staking_address(self, address: str) -> dict:
        """Validate and persist the staking-wallet address.

        Unlike ``save_settings``, this is allowed in production builds —
        the staking-address field is the remediation path for operators
        whose node sits in ``missing_wallet`` state, so it must be
        editable everywhere. Format + on-chain stake validation are
        re-run via ``validate_staking_address`` so a 0-stake wallet
        never makes it to disk.
        """
        check = self.validate_staking_address(address)
        if not check.get("ok"):
            return {"ok": False, "error": check.get("message", "Invalid staking address.")}
        try:
            # Preserve any explicit collection_address the operator
            # configured at onboarding. Passing "" would default it to
            # the staking address, silently overwriting a custom value.
            current_collection = self._config.get("SR_COLLECTION_ADDRESS") or ""
            normalised, _ = self._config.save_wallets(address.strip(), current_collection)
            return {"ok": True, "restart_required": True, "staking_address": normalised}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("Failed to save staking address")
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

    def validate_staking_address(self, address: str) -> dict:
        """Validate a staking-wallet address against coord-side stake state.

        Called from the onboarding wizard so the user sees an immediate
        actionable error (zero address, unstaked wallet, lookup failure)
        instead of starting the node and discovering "Insufficient stake"
        in the status panel.

        Returns ``{ok, status, message}`` where:

        - ``status`` is ``"unstaked" | "qualifying" | "earning" | "unknown" | "lookup_failed" | "invalid"``
        - ``ok`` is True only when the wallet is in a state that can run
          (i.e. ``qualifying``/``earning``).  ``unknown`` (not yet known
          to coord, e.g. brand-new staked wallet) also returns ``ok=True``
          so a user who just staked but hasn't yet shown up in coord
          isn't blocked.
        - ``lookup_failed`` returns ``ok=True`` so a transient coord
          outage doesn't block onboarding entirely.

        Staking address is required — empty input returns ``ok=False`` so
        the wizard's submit gate (and the Settings save bridge) refuse to
        advance without a real wallet.
        """
        addr = (address or "").strip()
        if not addr:
            return {
                "ok": False,
                "status": "required",
                "message": "Required — enter your staking wallet address.",
            }

        # Format gate first — keeps the round-trip cost down on typos.
        if not re.match(r"^0x[0-9a-fA-F]{40}$", addr):
            return {
                "ok": False,
                "status": "invalid",
                "message": "Invalid address — expected 0x followed by 40 hex characters",
            }
        # Zero address never has stake; reject up front.
        if addr.lower() == "0x" + "0" * 40:
            return {
                "ok": False,
                "status": "invalid",
                "message": "Zero address cannot stake. Enter your real wallet address.",
            }

        import httpx
        from gui.config_store import _default_coordination_url
        api_url = self._config.get("SR_COORDINATION_API_URL") or _default_coordination_url()
        try:
            resp = httpx.get(
                f"{api_url}/nodes",
                params={"staking_address": addr.lower()},
                timeout=5,
            )
            resp.raise_for_status()
            nodes = resp.json()
        except Exception as exc:
            logger.info("validate_staking_address lookup failed: %s", exc)
            return {
                "ok": True,  # don't block onboarding on coord blip
                "status": "lookup_failed",
                "message": "Could not verify stake right now (coord unreachable). "
                            "Continuing — the daemon will re-check after start.",
            }

        if not isinstance(nodes, list) or not nodes:
            # Wallet isn't tracked by coord yet. Could be a brand-new
            # staked wallet (probe race) or a totally unstaked one. We
            # let the user proceed but flag the state so the wizard can
            # show a soft warning rather than a hard block.
            return {
                "ok": True,
                "status": "unknown",
                "message": "Wallet not yet seen by the coordination API. "
                            "If you just staked, this is fine — start the node and it will verify.",
            }

        ss = (nodes[0].get("staking_status") or "").lower()
        if ss in ("qualifying", "earning"):
            return {"ok": True, "status": ss, "message": ""}
        if ss == "unstaked":
            return {
                "ok": False,
                "status": "unstaked",
                "message": "This wallet has no SPACE staked. "
                            "Stake at least 1 SPACE before starting your node.",
            }
        # Anything else (probing, draining, etc) — let the user proceed.
        return {"ok": True, "status": ss or "unknown", "message": ""}

    # ── Leg 2 receipts / earnings ──────────────────────────────────

    _ZERO_SUMMARY = {
        "claimed": 0,
        "failed_terminal": 0,
        "claimable": 0,
        "failed_retryable": 0,
        "pending_sign": 0,
        "claimable_total_price": 0,
    }

    def receipts_summary(self) -> dict:
        """Cheap counts + claimable SPACE total. Called on status poll.

        Returns ``{summary, escrow_configured}`` where ``summary`` is
        the raw per-view counts and ``escrow_configured`` tells the UI
        whether claim actions are available.
        """
        import sqlite3
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
        except sqlite3.OperationalError as exc:
            # Receipt store hasn't been bootstrapped yet (e.g. escrow
            # disabled at startup, fresh-install pre-first-receipt).
            # Status poll fires every ~10s — quietly return zeros instead
            # of spamming the log with stack traces. test.95 shipped
            # without lazy-init and the GUI Earnings card emitted a
            # full traceback per probe tick. PR 2 lazy-inits the store;
            # this is the defense-in-depth catch for any residual case.
            logger.debug("receipts_summary: store not initialized (%s) — returning zeros", exc)
            summary = dict(self._ZERO_SUMMARY)
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
            # Claim transactions are broadcast from the identity key's
            # address. Surface it here so the GUI can render a "send
            # CTC here for gas" hint — the test.105 flow surfaced that
            # users have no obvious way to discover this address.
            "claim_wallet_address": _claim_wallet_address(settings),
        }

    def receipts_list(
        self, view: str = "all", limit: int = 100, offset: int = 0,
    ) -> dict:
        import sqlite3
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
        except sqlite3.OperationalError as exc:
            logger.debug("receipts_list: store not initialized (%s) — returning empty", exc)
            return {
                "ok": True,
                "view": view,
                "summary": dict(self._ZERO_SUMMARY),
                "receipts": [],
                "claim_wallet_address": _claim_wallet_address(settings),
            }
        except Exception as exc:
            logger.exception("receipts_list failed")
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "view": view,
            "summary": summary,
            "receipts": [_receipt_to_json(sr) for sr in rows],
            "claim_wallet_address": _claim_wallet_address(settings),
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

    def receipts_retry_all(self) -> dict:
        """Retry every failed_retryable + claimable receipt in one batched run.

        Replaces the per-row ``receipts_retry(uuid)`` flow that used to
        emit one ``claimBatch`` tx per receipt — wasteful and the cause
        of the test.105 "5 separate retry txs" UX bug. The settlement
        layer chunks the queue into ``CLAIM_BATCH_SIZE`` (default 50)
        groups internally, so a single user click here results in one
        chain tx for up to 50 receipts at a time.
        """
        _claim_tasks.gc()
        try:
            task_id = _claim_tasks.start(
                lambda: _claim_runner(None, True),
            )
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

    # ── Auto-claim configuration & status ─────────────────────────

    def get_auto_claim_config(self) -> dict:
        """Return the persisted auto-claim configuration.

        Read directly from settings.json (the v1.5 canonical store) so
        we don't depend on the daemon being up. Threshold values are
        returned as **strings** because wei amounts can exceed
        JS Number.MAX_SAFE_INTEGER.
        """
        try:
            from app.settings_v2 import Settings as _SettingsV2
            from app.paths import config_dir
            path = config_dir() / "settings.json"
            if path.exists():
                s = _SettingsV2.load(path)
            else:
                s = _SettingsV2()
            claim = s.claim
            return {
                "ok": True,
                "enabled": bool(claim.auto_claim_enabled),
                "threshold_space_wei": str(claim.auto_claim_threshold_space_wei),
                "threshold_count": int(claim.auto_claim_threshold_count),
            }
        except Exception as exc:
            logger.exception("get_auto_claim_config failed")
            return {"ok": False, "error": str(exc)}

    def set_auto_claim_config(
        self, enabled: bool, threshold_space_wei: str = "", threshold_count: int = 0,
    ) -> dict:
        """Persist auto-claim configuration to settings.json.

        Note: the running ``AutoClaimMonitor`` reads its config at
        construction time and does **not** hot-reload (S8 is deferred).
        The UI tells the operator they must restart the node for the
        change to take effect.
        """
        try:
            from app.settings_v2 import Settings as _SettingsV2
            from app.paths import config_dir
            path = config_dir() / "settings.json"
            if path.exists():
                s = _SettingsV2.load(path)
            else:
                s = _SettingsV2()
            s.claim.auto_claim_enabled = bool(enabled)
            if threshold_space_wei != "":
                # Validate it parses as int-string (wei).
                try:
                    int(threshold_space_wei)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "threshold_space_wei must be an integer wei amount"}
                s.claim.auto_claim_threshold_space_wei = str(threshold_space_wei)
            if threshold_count is not None:
                try:
                    s.claim.auto_claim_threshold_count = int(threshold_count)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "threshold_count must be an integer"}
            s.save(path)
            return {"ok": True, "restart_required": True}
        except Exception as exc:
            logger.exception("set_auto_claim_config failed")
            return {"ok": False, "error": str(exc)}

    def get_auto_claim_status(self) -> dict:
        """Return current auto-claim monitor status.

        Side-effect: when the live monitor reports a fresh failure
        (a different ``last_attempt_at`` than we previously recorded),
        we append an entry to ``incidents.json`` so the sticky banner
        survives a GUI restart. Acknowledgement clears the banner.

        Falls back to a log-scan when the live monitor reference isn't
        available (the daemon thread doesn't currently expose it back
        through ``NodeManager``). The log-scan is best-effort.
        """
        # Try to find the live monitor via the node manager. We check
        # against the real class explicitly — getattr() on a MagicMock
        # would happily synthesise a fake ref otherwise.
        ctx_monitor = self._node.__dict__.get("_auto_claim_monitor_ref")
        live = None
        if ctx_monitor is not None:
            try:
                got = ctx_monitor.get_status()
                # Accept only a dict-shaped status payload.
                if isinstance(got, dict):
                    live = got
            except Exception:  # noqa: BLE001
                live = None

        if live is None:
            # Best-effort log scan for auto-claim failures so the
            # sticky banner can still surface them even when the live
            # monitor reference isn't plumbed back to the GUI thread.
            self._scan_logs_for_auto_claim_failure()
        if live is None:
            # Fallback: enabled flag from settings.json + last
            # persisted incident. This is what the UI sees when the
            # daemon isn't running.
            cfg = self.get_auto_claim_config()
            items = _incidents_load()
            last = next(
                (it for it in reversed(items)
                 if it.get("kind") == "auto_claim_failed"),
                None,
            )
            return {
                "ok": True,
                "enabled": cfg.get("enabled", False),
                "next_threshold_space_wei": cfg.get("threshold_space_wei", "0"),
                "next_threshold_count": cfg.get("threshold_count", 0),
                "current_claimable_wei": "0",
                "current_claimable_count": 0,
                "last_attempt_at": last.get("at_iso") if last else None,
                "last_attempt_outcome": "failed" if last else "none",
                "last_error": last.get("message") if last else None,
            }

        # Live path — record an incident on a fresh failure transition.
        outcome = live.get("last_attempt_outcome")
        last_at = live.get("last_attempt_at")
        if outcome == "failed" and last_at:
            items = _incidents_load()
            already = any(
                it.get("kind") == "auto_claim_failed" and it.get("at_iso") == last_at
                for it in items
            )
            if not already:
                _incident_record(
                    "auto_claim_failed",
                    code="auto_claim_failed",
                    message=live.get("last_error") or "Auto-claim attempt failed",
                    at_iso=last_at,
                )
        elif outcome == "success":
            # On success, auto-acknowledge any open auto-claim incidents so
            # the banner clears without manual user dismissal.
            items = _incidents_load()
            changed = False
            for it in items:
                if it.get("kind") == "auto_claim_failed" and not it.get("acknowledged"):
                    it["acknowledged"] = True
                    changed = True
            if changed:
                _incidents_save(items)

        return {"ok": True, **live}

    # ── Incident banner (sticky; persists across restarts) ────────

    def get_incidents(self) -> dict:
        """Return all incidents; the GUI shows the most recent
        unacknowledged one as a sticky banner."""
        try:
            return {"ok": True, "incidents": _incidents_load()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def acknowledge_incident(self, incident_id: str = "") -> dict:
        """Mark one (or all, when id is empty) incidents as acknowledged."""
        try:
            items = _incidents_load()
            for it in items:
                if not incident_id or it.get("id") == incident_id:
                    it["acknowledged"] = True
            _incidents_save(items)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _scan_logs_for_auto_claim_failure(self) -> None:
        """Best-effort scan of the daemon log for auto-claim failure
        lines. Records an incident on the first new occurrence so the
        sticky banner can surface it without the live monitor ref.

        Idempotent — incidents are de-duplicated by the log line text
        we hash into ``at_iso``. Called from ``get_auto_claim_status``.
        """
        try:
            from app.paths import config_dir
            log_path = config_dir() / "spacerouter.log"
            if not log_path.is_file():
                return
            # Read the tail (last ~100 KB) so this stays cheap on long-lived logs.
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 100_000))
                tail = f.read().decode("utf-8", errors="replace")
            # Match lines like:  "Auto-claim: claim_all() raised — RuntimeError: ..."
            pattern = re.compile(
                r"Auto-claim:.*claim_all\(\)\s*raised[^\n]*", re.IGNORECASE,
            )
            matches = pattern.findall(tail)
            if not matches:
                return
            latest = matches[-1].strip()
            items = _incidents_load()
            already = any(
                it.get("kind") == "auto_claim_failed"
                and it.get("at_iso") == latest
                for it in items
            )
            if already:
                return
            _incident_record(
                "auto_claim_failed",
                code="auto_claim_failed",
                message=latest,
                at_iso=latest,  # use the line itself as a dedup key
            )
        except Exception:  # noqa: BLE001
            return

    def get_recent_logs(self, limit: int = 50) -> dict:
        """Return the last ``limit`` log lines from the daemon log file.

        Used by the "Show log" button on the auto-claim failure banner.
        Best-effort — if the log file isn't found, returns an empty list.
        """
        try:
            from app.paths import config_dir
            log_path = config_dir() / "spacerouter.log"
            if not log_path.is_file():
                # Some platforms log to stdout only.
                return {"ok": True, "lines": []}
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return {"ok": True, "lines": lines[-int(limit):]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Clock skew (P4 already exposed it; plumb to GUI) ──────────

    def get_clock_skew_state(self) -> dict:
        """Return the latest measured clock-skew snapshot.

        P4 published the helper at ``app.clock_skew``. The GUI polls
        this so it can warn the operator when their machine clock is
        drifting badly enough to break EIP-712 timestamps.
        """
        try:
            from app.clock_skew import get_state  # type: ignore[attr-defined]
            return {"ok": True, **get_state()}
        except Exception:
            # Module may not exist yet on older branches; return a
            # benign default so the UI just hides the warning.
            return {"ok": True, "skew_seconds": 0, "is_significant": False}

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

        # rc.6 MAJ-3: tell the coord we're going away BEFORE we delete
        # the identity key + settings — otherwise the dashboard sees the
        # node as online for the full health-check window after reset.
        # Best-effort; do NOT block reset on coord failure.
        try:
            from app.config import load_settings
            from app.registration import deregister_best_effort_sync
            deregister_best_effort_sync(load_settings())
        except Exception:
            logger.warning(
                "Coord deregister failed during fresh restart; continuing",
                exc_info=True,
            )

        try:
            self._config.reset()
            # Clear env vars so next start picks up fresh config
            for key in list(os.environ.keys()):
                if key.startswith("SR_"):
                    del os.environ[key]
            # Re-init the GUI file logger. ``wipe_operational_state`` just
            # removed the ``logs/`` directory; without re-init the existing
            # RotatingFileHandler points at a non-existent file and some
            # platforms drop subsequent messages until restart. Calling
            # setup_gui_file_logging again attaches a fresh handler.
            try:
                from app.node_logging import setup_gui_file_logging
                setup_gui_file_logging()
            except Exception:
                logger.warning(
                    "GUI file logging re-init failed after reset",
                    exc_info=True,
                )
            return {"ok": True}
        except Exception as exc:
            logger.exception("Failed to fresh restart")
            return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────
# Background claim runner — called from _ClaimTaskRegistry.start()
# ──────────────────────────────────────────────────────────────────


def _claim_runner(only_uuid: str | None, include_retryable: bool) -> dict:
    """Background claim job.

    Serialised across CLI / GUI / double-clicks via a cross-platform
    file lock on ``~/.spacerouter/claim.lock`` — see
    :mod:`app.payment.claim_lock`. If the lock is already held,
    returns ``{noop: True}`` so the UI stays calm rather than showing
    an error when a second concurrent click comes in.

    Errors are returned with a stable ``error_code`` (see the
    classifier above) so the JS frontend can render a friendly modal
    rather than a raw exception string. A7 covers "needs CTC for gas"
    in particular; the same plumbing maps any other recognised pattern
    too.
    """
    from app.main import load_settings
    from app.payment.settlement import claim_all
    from app.payment.claim_lock import acquire_claim_lock, ClaimLockHeld
    from app.identity import load_or_create_identity, KeystorePassphraseRequired

    settings = load_settings()

    try:
        with acquire_claim_lock(settings):
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
                        "error_code": ERR_IDENTITY_KEY_MISSING,
                    }
                except FileNotFoundError as exc:
                    return {
                        "ok": False,
                        "error": str(exc),
                        "error_code": ERR_IDENTITY_KEY_MISSING,
                    }

            try:
                results = _run_async(claim_all(
                    settings, settlement_key,
                    include_retryable=include_retryable,
                    only_uuids=[only_uuid] if only_uuid else None,
                ))
            except ValueError as exc:
                return {
                    "ok": False, "error": str(exc),
                    "error_code": classify_error_text(str(exc)),
                }
            except Exception as exc:  # noqa: BLE001
                # Anything web3-side (RPC down, gas low, revert) lands
                # here. Map to a stable code for the UI.
                code = classify_error_text(f"{type(exc).__name__}: {exc}")
                logger.exception("Claim failed (code=%s)", code)
                return {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_code": code,
                }

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
    except ClaimLockHeld:
        return {"noop": True, "reason": "claim_in_progress"}
