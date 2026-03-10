"""Version information for Space Router Home Node.

At build time, the CI writes _build_version.py with the frozen version.
For local development, falls back to the SR_BUILD_VERSION env var or 'dev'.
"""

import os

try:
    from app._build_version import BUILD_VERSION

    __version__: str = BUILD_VERSION
except ImportError:
    __version__ = os.environ.get("SR_BUILD_VERSION", "dev")
