"""macOS menu bar (system tray) icon for SpaceRouter."""

import logging
import sys

logger = logging.getLogger(__name__)

_MACOS = sys.platform == "darwin"
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
                t._setup_on_main_thread()

        def shutdownTray_(self, sender):  # noqa: N802
            t = self.tray
            if t:
                t._shutdown_on_main_thread()


class SpaceRouterTray:
    """Menu bar status icon — macOS only, no-op on other platforms."""

    def __init__(self) -> None:
        self._status_item = None
        self._timer = None
        self._on_show = None
        self._on_quit = None
        self._node_manager = None
        self._delegate = None

    def start(self, *, on_show, on_quit, node_manager) -> None:
        """Schedule tray creation on the main thread (required by AppKit)."""
        if not _MACOS:
            return

        self._on_show = on_show
        self._on_quit = on_quit
        self._node_manager = node_manager

        self._delegate = _TrayDelegate.alloc().init()
        self._delegate.tray = self

        self._delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "setupTray:", None, False
        )

    def _setup_on_main_thread(self) -> None:
        """Build the NSStatusItem — called on the main thread."""
        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )

        button = self._status_item.button()
        button.setTitle_("\u25CF")
        button.setToolTip_("SpaceRouter")
        self._set_color(_COLOR_STARTING)

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
        if not self._node_manager or not self._status_item:
            return
        if self._node_manager.is_running:
            self._set_color(_COLOR_RUNNING)
        elif self._node_manager.last_error:
            self._set_color(_COLOR_STOPPED)
        else:
            self._set_color(_COLOR_STARTING)

    def _set_color(self, color) -> None:
        button = self._status_item.button()
        attrs = Foundation.NSDictionary.dictionaryWithObject_forKey_(
            color, AppKit.NSForegroundColorAttributeName
        )
        title = Foundation.NSAttributedString.alloc().initWithString_attributes_(
            "\u25CF", attrs
        )
        button.setAttributedTitle_(title)

    def shutdown(self) -> None:
        """Schedule tray removal on the main thread (required by AppKit)."""
        if not _MACOS or not self._delegate:
            return
        self._delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "shutdownTray:", None, True,  # waitUntilDone=True so we block until removed
        )

    def _shutdown_on_main_thread(self) -> None:
        """Remove the status item — called on the main thread."""
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        if self._status_item:
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            self._status_item = None
        self._delegate = None
