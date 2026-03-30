"""SpaceRouter Desktop App — pywebview entry point."""

import atexit
import logging
import os
import sys
import threading
import time

import webview

from gui.api import Api
from gui.config_store import ConfigStore
from gui.node_manager import NodeManager
from gui.tray import SpaceRouterTray

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _asset_path(filename: str) -> str:
    """Resolve asset path, handling PyInstaller frozen bundles."""
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "gui", "assets")  # type: ignore[attr-defined]
    else:
        base = os.path.join(os.path.dirname(__file__), "assets")
    return os.path.join(base, filename)


def _run_smoke_tests(window, api: Api) -> None:
    """Run automated GUI checks then exit. Called from the 'shown' event."""
    results: list[bool] = []

    def check(name, fn):
        try:
            result = fn()
            if result:
                print(f"  [PASS]  {name}")
                results.append(True)
            else:
                print(f"  [FAIL]  {name}: got {result!r}")
                results.append(False)
        except Exception as e:
            print(f"  [FAIL]  {name}: {e}")
            results.append(False)

    # Wait for the pywebview JS bridge to be injected (pywebviewready event).
    # On slow CI runners (especially Windows WebView2) this can take >3 s, so
    # poll rather than using a fixed sleep.  Give up after 30 s.
    print("  [INFO]  Waiting for pywebview bridge (up to 30 s)...")
    deadline = time.time() + 30
    bridge_ready = False
    while time.time() < deadline:
        try:
            if window.evaluate_js("typeof window.pywebview") == "object":
                bridge_ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not bridge_ready:
        print("  [FAIL]  pywebview bridge did not become available within 30 s")
        print("\n=== Results: 0 passed, 1 failed ===\n")
        window.destroy()
        os._exit(1)

    # Give JS init() a moment to complete the first API round-trip and show a screen
    time.sleep(0.5)

    print("\n=== SpaceRouter GUI — Smoke Tests ===\n")

    check(
        "Document title starts with 'SpaceRouter'",
        lambda: window.evaluate_js("document.title").startswith("SpaceRouter"),
    )

    check(
        "pywebview API bridge exists",
        lambda: window.evaluate_js("typeof window.pywebview") == "object",
    )

    check(
        "pywebview.api object exists",
        lambda: window.evaluate_js("typeof window.pywebview.api") == "object",
    )

    check(
        "get_status() returns dict with 'running' key",
        lambda: "running" in (api.get_status() or {}),
    )

    check(
        "Screen elements in DOM (2 production, 3 test)",
        lambda: window.evaluate_js("document.querySelectorAll('.screen').length") >= 2,
    )

    check(
        "A screen is visible (onboarding or status)",
        lambda: window.evaluate_js(
            "document.getElementById('screen-onboarding').style.display === 'flex' || "
            "document.getElementById('screen-status').style.display === 'flex'"
        )
        is True,
    )

    passed = sum(results)
    failed = len(results) - passed
    print(f"\n=== Results: {passed} passed, {failed} failed ===\n")

    exit_code = 0 if failed == 0 else 1
    window.destroy()
    # Use os._exit to ensure immediate termination (webview.start blocks the main thread)
    os._exit(exit_code)


def main() -> None:
    from app.variant import BUILD_VARIANT

    smoke_test = "--smoke-test" in sys.argv

    config = ConfigStore()
    node_manager = NodeManager()
    api = Api(config, node_manager)

    # Apply saved config to environment before anything imports app.config
    config.apply_to_env()

    # Start health-check server in smoke-test mode so external scripts can poll
    if smoke_test:
        from gui.health import start_health_server

        start_health_server(api)

    title = "SpaceRouter [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter"
    window = webview.create_window(
        title=title,
        url=_asset_path("index.html"),
        js_api=api,
        width=480,
        height=640,
        min_size=(400, 500),
        resizable=True,
    )

    if smoke_test:
        # In smoke-test mode: run checks after window is shown, then exit
        def on_shown_smoke() -> None:
            threading.Thread(
                target=_run_smoke_tests, args=(window, api), daemon=True
            ).start()

        window.events.shown += on_shown_smoke
    else:
        # Normal mode: tray icon and hide-on-close behaviour
        tray = SpaceRouterTray()
        _quitting = False

        def on_closing() -> bool:
            nonlocal _quitting
            if _quitting:
                return True  # Allow the close — we're shutting down
            # Hide to tray instead of closing
            logger.info("Window closing — hiding to tray")
            window.hide()
            return False

        window.events.closing += on_closing

        def on_show() -> None:
            window.show()

        def on_quit() -> None:
            nonlocal _quitting
            logger.info("Quit requested — stopping node…")
            _quitting = True

            def _shutdown() -> None:
                try:
                    node_manager.stop(timeout=15.0)
                except Exception:
                    logger.exception("Error stopping node")
                tray.shutdown()
                logger.info("Shutdown complete — exiting")
                # Use os._exit to terminate immediately. The node and tray
                # are already stopped; calling window.destroy() from a
                # background thread crashes Cocoa's main-thread-only UI.
                os._exit(0)

            threading.Thread(target=_shutdown, daemon=True).start()

        atexit.register(lambda: node_manager.stop(timeout=5.0))

        def on_shown() -> None:
            tray.start(on_show=on_show, on_quit=on_quit, node_manager=node_manager)

        window.events.shown += on_shown

    webview.start(debug=os.environ.get("SR_GUI_DEBUG", "").lower() in ("1", "true"))


if __name__ == "__main__":
    main()
