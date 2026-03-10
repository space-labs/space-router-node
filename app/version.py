"""Version information for Space Router Home Node.

The version is set at build time via the SR_VERSION environment variable.
Defaults to 'dev' for local development.
"""

import os

__version__ = os.environ.get("SR_BUILD_VERSION", "dev")
