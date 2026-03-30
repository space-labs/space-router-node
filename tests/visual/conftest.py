"""Skip visual tests when playwright is not installed (e.g. CI)."""

import sys

try:
    import playwright  # noqa: F401
except ImportError:
    # Prevent pytest from collecting test files in this directory
    collect_ignore_glob = ["test_*.py"]
