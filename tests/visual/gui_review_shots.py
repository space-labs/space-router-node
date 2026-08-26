"""Capture every GUI screen in the states that matter for a UI/UX review.

Builds on tests/visual/test_gui_screens.py (same technique: real shipped assets,
mocked pywebview bridge) but fixes two things that made the original corpus
misleading:

- the mock status omitted `staking_status` and `identity_address` entirely, so
  every "running" screen rendered the not-staked nag modal over the dashboard
  and an empty Identity chip. Neither is what a healthy node looks like.
- it only covered the happy path plus two errors. The states this release
  actually changed (endpoint_unreachable carrying the server's real reason,
  the upgrade-recovered staking address) were never captured.

Usage: .venv/bin/python tests/visual/gui_review_shots.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "gui" / "assets" / "index.html"
OUT = ROOT.parent / "qa-evidence" / "10-ui-review"

STAKING_ADDR = "0x1234567890abcdef1234567890abcdef12345678"
IDENTITY_ADDR = "0xe2107ae2b4c1d9f80a3b5c6d7e8f90a1b2c3d4e5"

BASE = {
    "state": "idle", "detail": "", "running": False, "phase": "stopped",
    "staking_address": "", "collection_address": "", "wallet": "",
    "staking": "", "staking_status": None,
    "error": None, "error_code": None, "error_message": None,
    "is_transient": False, "retry_count": 0, "next_retry_at": None,
    "node_id": None, "identity_address": None, "cert_expiry_warning": False,
    "rpc_status": None, "rpc_status_detail": None,
    "environment": "test",
    "api_url": "https://spacerouter-coordination-api-test.fly.dev",
}

RUNNING_EARNING = {
    **BASE,
    "state": "running", "running": True, "phase": "running",
    "staking_address": STAKING_ADDR, "wallet": STAKING_ADDR,
    "collection_address": "0xabcdef1234567890abcdef1234567890abcdef12",
    "identity_address": IDENTITY_ADDR,
    "node_id": "node-abc-123",
    "staking": "earning", "staking_status": "earning",
}

# The .137 fix: error_message now carries the server's real reason instead of
# falling through to the canned "Coordination server cannot reach this node".
UNREACHABLE = {
    **BASE,
    "state": "error_transient", "running": True, "phase": "registering",
    "staking_address": STAKING_ADDR, "identity_address": IDENTITY_ADDR,
    "staking_status": "earning",
    "error": "Coordination server cannot reach this node.",
    "error_code": "endpoint_unreachable",
    "error_message": "connection_refused: coordination server got ECONNREFUSED "
                     "probing 203.0.113.44:9090",
    "is_transient": True, "retry_count": 3,
    "next_retry_at": None,
    "detail": "Coordination server cannot reach this node. (Attempt 3, retry in 20s)",
}

NOT_STAKED = {
    **BASE,
    "state": "running", "running": True, "phase": "running",
    "staking_address": STAKING_ADDR, "identity_address": IDENTITY_ADDR,
    "node_id": "node-abc-123",
    "staking": "unstaked", "staking_status": "unstaked",
}

PASSPHRASE = {
    **BASE,
    "state": "passphrase_required",
    "detail": "Identity key is encrypted — passphrase required",
}

ENVIRONMENTS = [
    {"key": "production", "label": "Production",
     "url": "https://spacerouter-coordination-api.fly.dev", "active": False},
    {"key": "test", "label": "Test (CC Testnet)",
     "url": "https://spacerouter-coordination-api-test.fly.dev", "active": True},
    {"key": "local", "label": "Local", "url": "http://localhost:8000", "active": False},
]
SETTINGS = {
    "coordination_api_url": "https://spacerouter-coordination-api-test.fly.dev",
    "mtls_enabled": True,
}


def inject(page, *, variant="test", needs_onboarding=True, status=None,
           staking_address=""):
    page.evaluate(f"""() => {{
        window.pywebview = {{ api: {{
            needs_onboarding: () => {str(needs_onboarding).lower()},
            get_build_variant: () => "{variant}",
            get_status: () => ({json.dumps(status or BASE)}),
            get_environments: () => ({json.dumps(ENVIRONMENTS)}),
            set_environment: () => ({{ ok: true }}),
            get_network_mode: () => ({{mode: "upnp", public_host: "", port: ""}}),
            save_network_mode: () => ({{ ok: true }}),
            save_onboarding_and_start: () => ({{ ok: true }}),
            unlock_and_start: () => ({{ ok: true }}),
            start_node: () => ({{ ok: true }}),
            stop_node: () => ({{ ok: true }}),
            retry_node: () => ({{ ok: true }}),
            fresh_restart: () => ({{ ok: true }}),
            get_settings: () => ({json.dumps(SETTINGS)}),
            save_settings: () => ({{ ok: true }}),
            get_staking_address: () => "{staking_address}",
            get_min_staking_amount: () => "1000",
            open_url: () => null,
            get_version_info: () => ({{version: "1.5.2-test.137"}}),
        }} }};
        window.dispatchEvent(new Event("pywebviewready"));
    }}""")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    n = [20]  # continue numbering after the regenerated baseline suite
    log: list[str] = []

    def shot(page, name, note="", wait=700):
        n[0] += 1
        page.wait_for_timeout(wait)
        f = f"{n[0]}_{name}.png"
        page.screenshot(path=str(OUT / f), full_page=True)
        log.append(f"{f}  {note}")
        print(f"  [shot] {f}  {note}", flush=True)

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 480, "height": 700},
                            device_scale_factor=2)

        def fresh(**kw):
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: print(f"    [pageerror] {e}", flush=True))
            pg.goto(f"file://{INDEX_HTML}")
            inject(pg, **kw)
            pg.wait_for_timeout(900)
            return pg

        # Healthy running dashboard — no nag modal, Identity chip populated.
        pg = fresh(needs_onboarding=False, status=RUNNING_EARNING)
        shot(pg, "status_running_earning", "healthy node, staked+earning")
        pg.click("#btn-settings"); shot(pg, "settings_from_running", "settings panel")
        pg.close()

        # Reset Node confirm — the original suite could not reach this because
        # the nag modal sat on top of the button.
        pg = fresh(needs_onboarding=False, status=RUNNING_EARNING)
        pg.click("#btn-fresh-restart")
        shot(pg, "reset_node_confirm", "Reset Node dialog")
        pg.close()

        # endpoint_unreachable now showing the server's real reason.
        pg = fresh(needs_onboarding=False, status=UNREACHABLE)
        shot(pg, "status_endpoint_unreachable", ".137: real reason, not canned text")
        pg.close()

        # Not staked -> the nag modal, captured deliberately this time.
        pg = fresh(needs_onboarding=False, status=NOT_STAKED)
        shot(pg, "staking_nag_modal", "unstaked wallet on the running dashboard")
        pg.close()

        # Passphrase unlock.
        pg = fresh(needs_onboarding=False, status=PASSPHRASE)
        shot(pg, "passphrase_unlock", "encrypted identity key")
        pg.close()

        # Onboarding after an upgrade that DID recover the staking address.
        # This is the finding: the field is empty even though we know it.
        pg = fresh(needs_onboarding=True, staking_address=STAKING_ADDR)
        shot(pg, "onboarding_after_upgrade", "recovered address known but not prefilled")
        pg.close()

        # Onboarding validation states.
        pg = fresh(needs_onboarding=True)
        pg.fill("#staking-input", "not-an-address")
        pg.dispatch_event("#staking-input", "input")
        pg.dispatch_event("#staking-input", "blur")
        shot(pg, "onboarding_invalid_address", "garbage typed")
        pg.fill("#staking-input", "1234567890abcdef1234567890abcdef12345678")
        pg.dispatch_event("#staking-input", "input")
        pg.dispatch_event("#staking-input", "blur")
        shot(pg, "onboarding_bare_hex", "40 hex no 0x — BUG-06 says accept")
        pg.fill("#staking-input", STAKING_ADDR)
        pg.dispatch_event("#staking-input", "input")
        pg.dispatch_event("#staking-input", "blur")
        btn = pg.evaluate("""() => { const b = document.querySelector('#btn-start');
            const c = getComputedStyle(b);
            return {disabled: b.disabled, bg: c.backgroundColor, color: c.color,
                    opacity: c.opacity, cursor: c.cursor}; }""")
        shot(pg, "onboarding_valid_address", f"btn-start={json.dumps(btn)}")
        pg.close()

        b.close()

    (OUT / "SHOTS.txt").write_text("\n".join(log) + "\n")
    print(f"\n  {len(log)} screenshots -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
