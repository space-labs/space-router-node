"""Build variant for Space Router Home Node.

At build time, the CI writes _build_variant.py with the frozen variant.
For local development, falls back to the SR_BUILD_VARIANT env var or 'production'.

Variants:
  - 'production': Standard release build (settings UI hidden)
  - 'test': Test build with advanced settings (env selection, mTLS toggle)
"""

import os

try:
    from app._build_variant import BUILD_VARIANT
except ImportError:
    BUILD_VARIANT = os.environ.get("SR_BUILD_VARIANT", "production")
