"""Version check service — queries coordination API for update availability.

Provides both async and synchronous entry points.  All functions are
fail-safe: network errors, parse errors, and timeouts return a result
with status ``"unknown"`` so the node always proceeds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from app.version import __version__

logger = logging.getLogger(__name__)

VERSION_CHECK_INTERVAL = 21600  # 6 hours in seconds
DOWNLOAD_URL_FALLBACK = "https://github.com/space-labs/space-router-node/releases/latest"


def _parse_semver(v: str) -> tuple[int, ...]:
    """Parse a semver-ish string into a comparable tuple of ints.

    Strips a leading ``v``, ignores pre-release suffixes after ``-``.
    Returns ``(0,)`` on any parse failure so comparisons never crash.
    """
    try:
        return tuple(int(x) for x in v.lstrip("v").split("-")[0].split("."))
    except (ValueError, TypeError, AttributeError):
        return (0,)


@dataclass(frozen=True)
class VersionCheckResult:
    """Immutable snapshot of a version check."""

    current_version: str
    latest_version: str | None
    min_version: str | None
    download_url: str | None
    status: Literal["up_to_date", "soft_update", "hard_update", "unknown"]
    checked_at: float  # unix timestamp


def _compute_status(
    current: str,
    min_version: str | None,
    latest_version: str | None,
) -> Literal["up_to_date", "soft_update", "hard_update", "unknown"]:
    """Determine update status from version strings."""
    # Dev builds always pass
    if current == "dev":
        return "up_to_date"

    cur = _parse_semver(current)

    # Hard update: current below minimum
    if min_version:
        min_v = _parse_semver(min_version)
        if min_v != (0,) and cur < min_v:
            return "hard_update"

    # Soft update: current below latest
    if latest_version:
        latest_v = _parse_semver(latest_version)
        if latest_v != (0,) and cur < latest_v:
            return "soft_update"

    return "up_to_date"


def _build_result(
    data: dict,
) -> VersionCheckResult:
    """Build a VersionCheckResult from a /config API response dict."""
    latest_version = data.get("latestNodeVersion")
    min_version = data.get("minimumNodeVersion")
    download_url = data.get("downloadUrl") or DOWNLOAD_URL_FALLBACK

    status = _compute_status(__version__, min_version, latest_version)

    return VersionCheckResult(
        current_version=__version__,
        latest_version=latest_version,
        min_version=min_version,
        download_url=download_url,
        status=status,
        checked_at=time.time(),
    )


async def check_version(
    http_client: httpx.AsyncClient,
    coordination_api_url: str,
) -> VersionCheckResult:
    """Check for updates via the coordination API (async).

    Never raises — returns ``status="unknown"`` on any failure.
    """
    try:
        resp = await http_client.get(
            f"{coordination_api_url}/config",
            timeout=5.0,
        )
        resp.raise_for_status()
        return _build_result(resp.json())
    except Exception:
        logger.debug("Version check failed", exc_info=True)
        return VersionCheckResult(
            current_version=__version__,
            latest_version=None,
            min_version=None,
            download_url=DOWNLOAD_URL_FALLBACK,
            status="unknown",
            checked_at=time.time(),
        )


def check_version_sync(coordination_api_url: str) -> VersionCheckResult:
    """Check for updates via the coordination API (blocking).

    Never raises — returns ``status="unknown"`` on any failure.
    """
    try:
        resp = httpx.get(
            f"{coordination_api_url}/config",
            timeout=5.0,
        )
        resp.raise_for_status()
        return _build_result(resp.json())
    except Exception:
        logger.debug("Version check failed (sync)", exc_info=True)
        return VersionCheckResult(
            current_version=__version__,
            latest_version=None,
            min_version=None,
            download_url=DOWNLOAD_URL_FALLBACK,
            status="unknown",
            checked_at=time.time(),
        )
