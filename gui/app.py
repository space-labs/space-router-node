"""SpaceRouter Desktop App — pywebview entry point."""

import atexit
import logging
import os
import sys
import threading
import time


def _preload_upnp_before_webview() -> None:
    """Import miniupnpc before the webview backend maps its own DLLs.

    In the frozen Windows onefile build, importing miniupnpc *after* webview
    fails with ``ImportError: DLL load failed while importing miniupnpc:
    Invalid access to memory location`` (ERROR_NOACCESS). The same .pyd and its
    delvewheel side-car import cleanly out of the same bundle in a bare
    interpreter on the same machine, so the conflict comes from what the
    webview backend has already loaded into the process. Importing first leaves
    the module in sys.modules for app.upnp to reuse, and the Windows GUI could
    not map a port without it.
    """
    try:
        import miniupnpc  # noqa: F401
    except Exception:
        logging.getLogger(__name__).warning(
            "miniupnpc preload failed; UPnP will be unavailable", exc_info=True,
        )


_preload_upnp_before_webview()

import webview  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Run legacy macOS / Linux migrations BEFORE anything else touches
# ~/.spacerouter — including the log file handler below, which creates
# ~/.spacerouter/logs/ on first call. The migration's safety check
# (`_is_dir_empty`) refuses to overwrite a non-empty target, so even a
# single log file pre-existing in the new dir is enough to silently
# skip the migration. Running this first guarantees the canonical dir
# is empty when the migration helper looks at it.
def _run_legacy_migrations_early() -> None:
    try:
        from app.paths import config_dir
        from app.legacy_migration import (
            maybe_migrate_legacy_linux,
            maybe_migrate_legacy_macos,
        )
        target = config_dir()
        try:
            moved = maybe_migrate_legacy_macos(target)
            if moved:
                logger.info("legacy macOS migration: copied to %s", target)
        except Exception:
            logger.warning(
                "legacy macOS migration skipped due to error", exc_info=True,
            )
        try:
            moved = maybe_migrate_legacy_linux(target)
            if moved:
                logger.info("legacy Linux XDG migration: copied to %s", target)
        except Exception:
            logger.warning(
                "legacy Linux XDG migration skipped due to error", exc_info=True,
            )
    except Exception:
        # Never let migration glitches block startup.
        logger.warning("legacy migration entry skipped due to error", exc_info=True)


_run_legacy_migrations_early()

from gui.api import Api  # noqa: E402
from gui.config_store import ConfigStore  # noqa: E402
from gui.node_manager import NodeManager  # noqa: E402
from gui.single_instance import SingleInstanceLock  # noqa: E402
from gui.tray import SpaceRouterTray  # noqa: E402

# Set up persistent log file with rotation. Must follow the migration
# above — this call creates ~/.spacerouter/logs/ on first run.
from app.node_logging import setup_gui_file_logging  # noqa: E402

_log_dir = setup_gui_file_logging()


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


def _enable_remote_debugging() -> None:
    """Expose a CDP endpoint when SR_GUI_REMOTE_DEBUGGING_PORT is set.

    The packaged GUI is otherwise undrivable from outside. Setting
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS does not work, because pywebview's
    EdgeChromium backend assigns AdditionalBrowserArguments outright and
    discards whatever the environment asked for; it does however pass the port
    through from its own settings. Off unless the variable is set, so an
    operator build never opens a debugging port.
    """
    port = os.environ.get("SR_GUI_REMOTE_DEBUGGING_PORT", "").strip()
    if not port.isdigit():
        return
    try:
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(port)
        logger.info("WebView2 remote debugging enabled on port %s", port)
    except Exception:
        logger.warning("Could not enable remote debugging on port %s", port, exc_info=True)


def main() -> None:
    from app.variant import BUILD_VARIANT

    smoke_test = "--smoke-test" in sys.argv

    # ---- Single-instance guard (skip during smoke tests) ----------------
    instance_lock = SingleInstanceLock()
    if not smoke_test:
        # We don't know the window reference yet, so wire up the
        # on_show_request callback after the window is created.
        if not instance_lock.try_acquire():
            # Another instance is already running and was signalled.
            logger.info("Another SpaceRouter instance is running — exiting")
            sys.exit(0)

    config = ConfigStore()
    node_manager = NodeManager()
    api = Api(config, node_manager)

    # Apply saved config to environment before anything imports app.config
    config.apply_to_env()

    # Start health-check server in smoke-test mode so external scripts can poll
    if smoke_test:
        from gui.health import start_health_server

        start_health_server(api)

    title = "SpaceRouter Proxy [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter Proxy"
    _enable_remote_debugging()

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

        # Ungate the single-instance accept loop now that the window exists
        instance_lock.set_show_callback(on_show)

        def on_quit() -> None:
            nonlocal _quitting
            logger.info("Quit requested — stopping node…")
            _quitting = True

            def _shutdown() -> None:
                try:
                    node_manager.stop(timeout=15.0)
                except Exception:
                    logger.exception("Error stopping node")
                instance_lock.release()
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
