"""System tray icon for SpaceRouter (macOS menu bar + Windows notification area)."""

import logging
import os
import sys
import threading

from app.state import NodeState

logger = logging.getLogger(__name__)

_MACOS = sys.platform == "darwin"
_WINDOWS = sys.platform == "win32"


def _tray_asset_path(filename: str) -> str:
    """Resolve path to a tray icon asset, handling PyInstaller frozen bundles."""
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "gui", "assets", "tray")
    else:
        base = os.path.join(os.path.dirname(__file__), "assets", "tray")
    return os.path.join(base, filename)


# ---------------------------------------------------------------------------
# macOS — native AppKit NSStatusItem
# ---------------------------------------------------------------------------
if _MACOS:
    import AppKit
    import Foundation
    import objc

    def _load_macos_tray_image(name: str) -> "AppKit.NSImage":
        """Load a tray PNG as an NSImage sized for the menu bar (18pt)."""
        path_2x = _tray_asset_path(f"{name}@2x.png")
        path_1x = _tray_asset_path(f"{name}.png")
        # Prefer @2x for retina
        path = path_2x if os.path.exists(path_2x) else path_1x
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        if img:
            img.setSize_(Foundation.NSMakeSize(18, 18))
        return img

    _TRAY_IMG_RUNNING = None
    _TRAY_IMG_STOPPED = None
    _TRAY_IMG_STARTING = None

    class _TrayDelegate(AppKit.NSObject):
        """Tiny NSObject that receives menu actions and timer callbacks."""

        tray = objc.ivar("tray")

        def showWindow_(self, sender):  # noqa: N802
            t = self.tray
            if t and t._on_show:
                t._on_show()

        def quitApp_(self, sender):  # noqa: N802
            t = self.tray
            if t and t._on_quit:
                t._on_quit()

        def updateStatus_(self, timer):  # noqa: N802
            t = self.tray
            if t:
                t._update_icon()

        def setupTray_(self, sender):  # noqa: N802
            t = self.tray
            if t:
                t._setup_macos()

        def shutdownTray_(self, sender):  # noqa: N802
            t = self.tray
            if t:
                t._shutdown_macos()

# ---------------------------------------------------------------------------
# Windows — pystray + Pillow
# ---------------------------------------------------------------------------
if _WINDOWS:
    import pystray
    from PIL import Image, ImageDraw


class SpaceRouterTray:
    """System tray status icon — macOS menu bar / Windows notification area.

    No-op on unsupported platforms (Linux, etc.).
    """

    def __init__(self) -> None:
        self._on_show = None
        self._on_quit = None
        self._node_manager = None

        # macOS internals
        self._status_item = None
        self._timer = None
        self._delegate = None

        # Windows internals
        self._pystray_icon = None
        self._win_timer: threading.Timer | None = None
        self._win_running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, *, on_show, on_quit, node_manager) -> None:
        """Create the tray icon. Must be called after the window is shown."""
        self._on_show = on_show
        self._on_quit = on_quit
        self._node_manager = node_manager

        if _MACOS:
            self._start_macos()
        elif _WINDOWS:
            self._start_windows()

    def shutdown(self) -> None:
        """Remove the tray icon and release resources."""
        if _MACOS:
            self._shutdown_macos_safe()
        elif _WINDOWS:
            self._shutdown_windows()

    # ------------------------------------------------------------------
    # macOS implementation
    # ------------------------------------------------------------------

    def _start_macos(self) -> None:
        self._delegate = _TrayDelegate.alloc().init()
        self._delegate.tray = self
        self._delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "setupTray:", None, False
        )

    def _setup_macos(self) -> None:
        """Build the NSStatusItem — called on the main thread."""
        global _TRAY_IMG_RUNNING, _TRAY_IMG_STOPPED, _TRAY_IMG_STARTING
        from app.variant import BUILD_VARIANT

        # Lazy-load tray images (must happen after PyInstaller extracts assets)
        _TRAY_IMG_RUNNING = _load_macos_tray_image("tray-green")
        _TRAY_IMG_STOPPED = _load_macos_tray_image("tray-red")
        _TRAY_IMG_STARTING = _load_macos_tray_image("tray-yellow")

        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )

        tooltip = "SpaceRouter [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter"
        button = self._status_item.button()
        button.setToolTip_(tooltip)
        self._set_macos_icon(_TRAY_IMG_STARTING)

        menu = AppKit.NSMenu.alloc().init()

        show_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show SpaceRouter", "showWindow:", ""
        )
        show_item.setTarget_(self._delegate)
        menu.addItem_(show_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit SpaceRouter", "quitApp:", ""
        )
        quit_item.setTarget_(self._delegate)
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

        self._timer = (
            Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                3.0, self._delegate, "updateStatus:", None, True
            )
        )

    def _update_icon(self) -> None:
        """Refresh the macOS status-bar icon to reflect node state."""
        if not self._node_manager or not self._status_item:
            return
        state = self._node_manager.status.state
        if state == NodeState.RUNNING:
            self._set_macos_icon(_TRAY_IMG_RUNNING)
        elif state in (NodeState.IDLE, NodeState.ERROR_PERMANENT):
            self._set_macos_icon(_TRAY_IMG_STOPPED)
        else:
            self._set_macos_icon(_TRAY_IMG_STARTING)

    def _set_macos_icon(self, image) -> None:
        button = self._status_item.button()
        if image:
            button.setImage_(image)
            button.setTitle_("")
        else:
            # Fallback to colored dot if image failed to load
            button.setTitle_("\u25CF")

    def _shutdown_macos_safe(self) -> None:
        """Schedule tray removal on the main thread (required by AppKit)."""
        if not self._delegate:
            return
        self._delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "shutdownTray:", None, True,
        )

    def _shutdown_macos(self) -> None:
        """Remove the status item — called on the main thread."""
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._status_item:
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            self._status_item = None
        self._delegate = None

    # ------------------------------------------------------------------
    # Windows implementation
    # ------------------------------------------------------------------

    _WIN_TRAY_IMAGES: dict[str, "Image.Image"] = {}

    @classmethod
    def _load_win_tray_images(cls):
        """Load pre-rendered tray PNGs for Windows."""
        for name in ("tray-green", "tray-red", "tray-yellow"):
            path = _tray_asset_path(f"{name}@2x.png")
            if not os.path.exists(path):
                path = _tray_asset_path(f"{name}.png")
            try:
                img = Image.open(path).resize((64, 64), Image.LANCZOS)
                cls._WIN_TRAY_IMAGES[name] = img
            except Exception:
                cls._WIN_TRAY_IMAGES[name] = cls._create_win_fallback((128, 128, 128))

    @staticmethod
    def _create_win_fallback(color_rgb: tuple[int, int, int]) -> "Image.Image":
        """Fallback: generate a 64x64 RGBA image with a filled circle."""
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([4, 4, size - 4, size - 4], fill=(*color_rgb, 255))
        return image

    def _start_windows(self) -> None:
        from app.variant import BUILD_VARIANT

        self._load_win_tray_images()
        tooltip = "SpaceRouter [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter"

        icon_image = self._WIN_TRAY_IMAGES.get("tray-yellow", self._create_win_fallback((242, 156, 18)))

        menu = pystray.Menu(
            pystray.MenuItem("Show SpaceRouter", self._win_on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit SpaceRouter", self._win_on_quit),
        )

        self._pystray_icon = pystray.Icon(
            name="spacerouter",
            icon=icon_image,
            title=tooltip,
            menu=menu,
        )

        self._win_running = True

        # pystray.Icon.run() blocks, so run it in a daemon thread
        threading.Thread(
            target=self._pystray_icon.run, daemon=True, name="pystray"
        ).start()

        # Periodic status-colour refresh (every 3 s, matching macOS)
        self._schedule_win_update()

    def _schedule_win_update(self) -> None:
        if not self._win_running:
            return
        self._win_timer = threading.Timer(3.0, self._win_update_tick)
        self._win_timer.daemon = True
        self._win_timer.start()

    def _win_update_tick(self) -> None:
        self._update_win_icon()
        self._schedule_win_update()

    def _update_win_icon(self) -> None:
        if not self._pystray_icon or not self._node_manager:
            return
        state = self._node_manager.status.state
        if state == NodeState.RUNNING:
            key = "tray-green"
        elif state in (NodeState.IDLE, NodeState.ERROR_PERMANENT):
            key = "tray-red"
        else:
            key = "tray-yellow"
        img = self._WIN_TRAY_IMAGES.get(key)
        if img:
            self._pystray_icon.icon = img

    def _win_on_show(self, icon=None, item=None) -> None:
        if self._on_show:
            self._on_show()

    def _win_on_quit(self, icon=None, item=None) -> None:
        if self._on_quit:
            self._on_quit()

    def _shutdown_windows(self) -> None:
        self._win_running = False
        if self._win_timer:
            self._win_timer.cancel()
            self._win_timer = None
        if self._pystray_icon:
            try:
                self._pystray_icon.stop()
            except Exception:
                pass
            self._pystray_icon = None
