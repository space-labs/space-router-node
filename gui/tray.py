"""System tray icon for SpaceRouter (macOS menu bar + Windows notification area)."""

import logging
import sys
import threading

from app.state import NodeState

logger = logging.getLogger(__name__)

_MACOS = sys.platform == "darwin"
_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# macOS — native AppKit NSStatusItem
# ---------------------------------------------------------------------------
if _MACOS:
    import AppKit
    import Foundation
    import objc

    _COLOR_RUNNING = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        0.18, 0.80, 0.44, 1.0
    )
    _COLOR_STOPPED = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        0.91, 0.30, 0.24, 1.0
    )
    _COLOR_STARTING = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        0.95, 0.61, 0.07, 1.0
    )

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
        from app.variant import BUILD_VARIANT

        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )

        tooltip = "SpaceRouter [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter"
        button = self._status_item.button()
        button.setTitle_("\u25CF")
        button.setToolTip_(tooltip)
        self._set_macos_color(_COLOR_STARTING)

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
        """Refresh the macOS status-bar colour to reflect node state."""
        if not self._node_manager or not self._status_item:
            return
        state = self._node_manager.status.state
        if state == NodeState.RUNNING:
            self._set_macos_color(_COLOR_RUNNING)
        elif state in (NodeState.IDLE, NodeState.ERROR_PERMANENT):
            self._set_macos_color(_COLOR_STOPPED)
        else:
            self._set_macos_color(_COLOR_STARTING)

    def _set_macos_color(self, color) -> None:
        button = self._status_item.button()
        attrs = Foundation.NSDictionary.dictionaryWithObject_forKey_(
            color, AppKit.NSForegroundColorAttributeName
        )
        title = Foundation.NSAttributedString.alloc().initWithString_attributes_(
            "\u25CF", attrs
        )
        button.setAttributedTitle_(title)

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

    _WIN_COLOR_RUNNING = (46, 204, 113)    # green
    _WIN_COLOR_STOPPED = (232, 76, 61)     # red
    _WIN_COLOR_STARTING = (242, 156, 18)   # orange

    def _start_windows(self) -> None:
        from app.variant import BUILD_VARIANT

        tooltip = "SpaceRouter [TEST]" if BUILD_VARIANT == "test" else "SpaceRouter"

        icon_image = self._create_win_icon(self._WIN_COLOR_STARTING)

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

    @staticmethod
    def _create_win_icon(color_rgb: tuple[int, int, int]) -> "Image.Image":
        """Generate a 64x64 RGBA image with a filled circle."""
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([4, 4, size - 4, size - 4], fill=(*color_rgb, 255))
        return image

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
            color = self._WIN_COLOR_RUNNING
        elif state in (NodeState.IDLE, NodeState.ERROR_PERMANENT):
            color = self._WIN_COLOR_STOPPED
        else:
            color = self._WIN_COLOR_STARTING
        self._pystray_icon.icon = self._create_win_icon(color)

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
