"""Visual GUI tests — render each screen in Chromium and capture screenshots.

Usage:
    .venv312/bin/python tests/visual/test_gui_screens.py

Screenshots are saved to tests/visual/screenshots/.
"""

import json
import os
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    # Allow this script to be imported by pytest without failing collection.
    # The actual test functions won't run — this is a standalone script.
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS = ROOT / "gui" / "assets"
INDEX_HTML = ASSETS / "index.html"
SCREENSHOTS = Path(__file__).resolve().parent / "screenshots"

# Mock API responses
MOCK_STATUS_IDLE = {
    "state": "idle", "detail": "", "running": False, "phase": "stopped",
    "staking_address": "", "collection_address": "", "wallet": "",
    "staking": "", "error": None, "error_code": None, "error_message": None,
    "is_transient": False, "retry_count": 0, "next_retry_at": None,
    "node_id": None, "cert_expiry_warning": False,
    "environment": "test", "api_url": "https://spacerouter-coordination-api-test.fly.dev",
}

MOCK_STATUS_RUNNING = {
    **MOCK_STATUS_IDLE,
    "state": "running", "running": True, "phase": "running",
    "staking_address": "0x1234567890abcdef1234567890abcdef12345678",
    "collection_address": "0xabcdef1234567890abcdef1234567890abcdef12",
    "wallet": "0x1234567890abcdef1234567890abcdef12345678",
    "node_id": "node-abc-123",
    "environment": "test",
}

MOCK_STATUS_ERROR = {
    **MOCK_STATUS_IDLE,
    "state": "error_permanent", "running": False, "phase": "stopped",
    "error": "Registration rejected by server.",
    "error_code": "registration_rejected",
    "error_message": "Registration rejected by server. Check wallet and environment.",
    "staking_address": "0x1234567890abcdef1234567890abcdef12345678",
    "environment": "test",
}

MOCK_STATUS_PASSPHRASE = {
    **MOCK_STATUS_IDLE,
    "state": "passphrase_required",
    "detail": "Identity key is encrypted — passphrase required",
}

MOCK_STATUS_RECONNECTING = {
    **MOCK_STATUS_IDLE,
    "state": "error_transient", "running": True,
    "error": "Cannot reach coordination server. Retrying...",
    "error_code": "network_unreachable",
    "error_message": "Cannot reach coordination server. Retrying...",
    "is_transient": True, "retry_count": 2,
    "next_retry_at": time.time() + 15,
    "detail": "Cannot reach coordination server. Retrying... (Attempt 2, retry in 12s)",
    "staking_address": "0x1234567890abcdef1234567890abcdef12345678",
    "environment": "test",
}

MOCK_ENVIRONMENTS = [
    {"key": "production", "label": "Production",
     "url": "https://spacerouter-coordination-api.fly.dev", "active": False},
    {"key": "test", "label": "Test (CC Testnet)",
     "url": "https://spacerouter-coordination-api-test.fly.dev", "active": True},
    {"key": "staging", "label": "Staging",
     "url": "https://spacerouter-coordination-api-staging.fly.dev", "active": False},
    {"key": "local", "label": "Local", "url": "http://localhost:8000", "active": False},
]

MOCK_NETWORK_MODE = {"mode": "upnp", "public_host": "", "port": ""}

MOCK_SETTINGS = {
    "coordination_api_url": "https://spacerouter-coordination-api-test.fly.dev",
    "mtls_enabled": True,
}


def inject_mock_api(page, *, variant="test", needs_onboarding=True,
                    status_response=None):
    """Inject a mock window.pywebview.api that returns canned responses."""
    status = json.dumps(status_response or MOCK_STATUS_IDLE)
    envs = json.dumps(MOCK_ENVIRONMENTS)
    net = json.dumps(MOCK_NETWORK_MODE)
    settings = json.dumps(MOCK_SETTINGS)

    page.evaluate(f"""() => {{
        window.pywebview = {{
            api: {{
                needs_onboarding: () => {str(needs_onboarding).lower()},
                get_build_variant: () => "{variant}",
                get_status: () => ({status}),
                get_environments: () => ({envs}),
                set_environment: (key) => ({{ ok: true }}),
                get_network_mode: () => ({net}),
                save_network_mode: (mode, host, port) => ({{ ok: true }}),
                save_onboarding_and_start: (p, s, c, k) => ({{ ok: true }}),
                unlock_and_start: (p) => ({{ ok: true }}),
                start_node: () => ({{ ok: true }}),
                stop_node: () => ({{ ok: true }}),
                retry_node: () => ({{ ok: true }}),
                fresh_restart: (keep) => ({{ ok: true }}),
                get_settings: () => ({settings}),
                save_settings: (url, mtls) => ({{ ok: true }}),
            }}
        }};
        // Fire the pywebviewready event to trigger init()
        window.dispatchEvent(new Event("pywebviewready"));
    }}""")


def screenshot(page, name: str, wait_ms: int = 500):
    """Take a full-page screenshot and save it."""
    page.wait_for_timeout(wait_ms)
    path = SCREENSHOTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  [SCREENSHOT] {name}.png")


def main():
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    print(f"\n=== SpaceRouter GUI Visual Tests ===")
    print(f"HTML: {INDEX_HTML}")
    print(f"Screenshots: {SCREENSHOTS}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 480, "height": 700},
            device_scale_factor=2,  # Retina
        )

        # ── 1. Onboarding (Test Build) ──
        print("1. Onboarding — Test Build")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=True)
        page.wait_for_timeout(800)
        screenshot(page, "01_onboarding_test")

        # Open advanced section
        page.click("#advanced-toggle")
        page.wait_for_timeout(300)
        screenshot(page, "02_onboarding_advanced_open")

        # Switch to tunnel mode in advanced
        page.click("#onboard-tunnel")
        page.wait_for_timeout(300)
        screenshot(page, "03_onboarding_tunnel_mode")

        # Switch to import key mode
        page.click("#radio-import")
        page.wait_for_timeout(300)
        screenshot(page, "04_onboarding_import_key")
        page.close()

        # ── 2. Onboarding (Production Build) ──
        print("2. Onboarding — Production Build")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="production", needs_onboarding=True)
        page.wait_for_timeout(800)
        screenshot(page, "05_onboarding_production")

        # Open advanced — should NOT show env selector
        page.click("#advanced-toggle")
        page.wait_for_timeout(300)
        screenshot(page, "06_onboarding_production_advanced")
        page.close()

        # ── 3. Status Dashboard — Running ──
        print("3. Status Dashboard — Running")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_RUNNING)
        page.wait_for_timeout(800)
        screenshot(page, "07_status_running")
        page.close()

        # ── 4. Status Dashboard — Error ──
        print("4. Status Dashboard — Permanent Error")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_ERROR)
        page.wait_for_timeout(800)
        screenshot(page, "08_status_error")
        page.close()

        # ── 5. Status Dashboard — Transient Error / Retrying ──
        print("5. Status Dashboard — Retrying")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_RECONNECTING)
        page.wait_for_timeout(800)
        screenshot(page, "09_status_retrying")
        page.close()

        # ── 6. Passphrase Unlock Dialog ──
        print("6. Passphrase Unlock Dialog")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_PASSPHRASE)
        page.wait_for_timeout(800)
        screenshot(page, "10_passphrase_unlock")
        page.close()

        # ── 7. Fresh Restart Dialog ──
        print("7. Fresh Restart Dialog")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_RUNNING)
        page.wait_for_timeout(800)
        page.click("#btn-fresh-restart")
        page.wait_for_timeout(300)
        screenshot(page, "11_fresh_restart")
        page.close()

        # ── 8. Settings Panel (Test Build) ──
        print("8. Settings Panel — Test Build")
        page = context.new_page()
        page.goto(f"file://{INDEX_HTML}")
        inject_mock_api(page, variant="test", needs_onboarding=False,
                        status_response=MOCK_STATUS_RUNNING)
        page.wait_for_timeout(800)
        page.click("#btn-settings")
        page.wait_for_timeout(500)
        screenshot(page, "12_settings_test")
        page.close()

        browser.close()

    print(f"\n=== Done — {len(list(SCREENSHOTS.glob('*.png')))} screenshots saved ===\n")


if __name__ == "__main__":
    main()
