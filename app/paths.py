"""Platform-specific config directory for Space Router Node.

Provides a single source of truth for both CLI and GUI entry points.
"""

import os
import sys
from pathlib import Path


def config_dir(variant: str | None = None) -> Path:
    """Return the platform-specific config directory.

    When *variant* is ``"test"``, a ``-Test`` suffix is appended to avoid
    cross-contamination between test and production installs.
    """
    if variant is None:
        from app.variant import BUILD_VARIANT
        variant = BUILD_VARIANT

    is_test = variant == "test"

    if sys.platform == "darwin":
        name = "SpaceRouter-Test" if is_test else "SpaceRouter"
        return Path.home() / "Library" / "Application Support" / name
    elif sys.platform == "win32":
        name = "SpaceRouter-Test" if is_test else "SpaceRouter"
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / name
        return Path.home() / "AppData" / "Local" / name
    else:
        name = "spacerouter-test" if is_test else "spacerouter"
        return Path.home() / ".config" / name
