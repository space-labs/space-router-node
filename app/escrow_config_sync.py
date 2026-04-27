"""Trust-on-first-use sync of escrow config from the coordination API.

Track P2 of the v1.5 stabilization plan. Per operator decision (see
``internal-docs/v1.5-provider-plan.md`` Section 6 A1):

> Read /config endpoint at app startup and save it in settings.json for
> later use and not use /config rate value in subsequent restarts or
> starts if value exists in settings.json. and if the value is old or
> wrong in settings.json, coord rejects the receipts anyway with clear
> message including the actual rate expected in the error message.

Implementation notes:

* **Trust-on-first-use.** The first daemon launch where
  ``settings.escrow.synced_from_coord_at`` is unset and
  ``leg2_rate_per_gb`` is unset fetches ``/config`` once. Subsequent
  launches see the timestamp + rate and skip the network call entirely.
* **Stale-config drift handling lives gateway-side.** If the persisted
  rate goes stale, gateway's ``SIGN_REJECTED_PRICE_CAP`` rejection
  message includes the expected rate so the operator can update
  ``settings.json`` (or run ``--reset`` and re-fetch). We do NOT add a
  periodic re-sync, a CLI flag, or any S3-c-style fail-loud retry path.
* **HTTP failures never block startup.** A 5xx, timeout, DNS error, or
  malformed JSON degrades to a WARN log + unchanged settings; the daemon
  keeps starting, just without escrow config. The receipt submitter is
  already gated on rate > 0, so missing rate cleanly disables Leg 2.
* **Sync, not async.** Daemon startup is sequential at this point and
  reads happen well before the asyncio event loop is busy with relay
  traffic; the simpler synchronous httpx call is fine here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from app.settings_v2 import Settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


def sync_escrow_config_from_coord(settings_v2: Settings) -> Settings:
    """Populate ``settings_v2.escrow`` from coord ``/config`` (trust-on-first-use).

    Returns the (possibly mutated) settings object. The caller is
    responsible for persisting the result via ``settings_v2.save(path)``
    so the next launch sees the synced values.

    Behavior:

    * Already-synced (``synced_from_coord_at`` set AND ``leg2_rate_per_gb``
      set) → no-op, returns unchanged. Logged at INFO.
    * Defensive: rate set but timestamp missing → skip too. We never
      overwrite a value that an operator may have manually pinned.
    * Otherwise: GET ``<coord>/config`` with a 5s timeout. On success,
      populate any escrow fields that are currently None/empty and stamp
      ``synced_from_coord_at`` with an ISO8601 UTC timestamp.
    * On any HTTP / parse failure: WARN, return unchanged. Never raises.
    * Defensive on the response: a numeric ``0`` rate or empty payer
      address is treated as "not configured" and is NOT persisted (so a
      coord misconfig doesn't poison the local cache).
    """
    escrow = settings_v2.escrow

    if escrow.synced_from_coord_at and escrow.leg2_rate_per_gb:
        logger.info(
            "escrow config already synced (since %s), skipping",
            escrow.synced_from_coord_at,
        )
        return settings_v2

    # We previously skipped sync when the rate was set without a sync
    # timestamp on the heuristic that the operator (or a v1.4 env-var
    # seed) had pinned it.  In practice that branch trapped both the
    # test.97 backfill (PR #93) and any future install path that
    # bootstraps a placeholder rate before TOFU completes — the local
    # rate stayed at the bootstrap value (1e15) while the gateway's
    # canonical Leg 2 rate is 500 SPACE/GB (5e20), and every receipt
    # got rejected as SIGN_REJECTED_UNKNOWN_REQUEST.  The coord is the
    # canonical source for the rate; if a real operator wants to pin a
    # different value they should also set ``synced_from_coord_at``
    # (e.g. by editing both fields in settings.json).

    coord_url = (settings_v2.coordination.url or "").rstrip("/")
    if not coord_url:
        logger.warning("escrow config sync skipped: coordination.url is empty")
        return settings_v2

    config_url = f"{coord_url}/config"
    try:
        resp = httpx.get(config_url, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.TimeoutException as e:
        logger.warning(
            "escrow config sync: coord %s timed out after %.1fs (%s) — leaving settings unchanged",
            config_url, _TIMEOUT_SECONDS, e,
        )
        return settings_v2
    except httpx.HTTPStatusError as e:
        logger.warning(
            "escrow config sync: coord %s returned HTTP %d — leaving settings unchanged",
            config_url, e.response.status_code,
        )
        return settings_v2
    except httpx.HTTPError as e:
        # Connection refused, DNS failure, transport errors, etc.
        logger.warning(
            "escrow config sync: coord %s unreachable (%s) — leaving settings unchanged",
            config_url, e,
        )
        return settings_v2
    except (ValueError, TypeError) as e:
        # JSON parse failure
        logger.warning(
            "escrow config sync: coord %s returned malformed JSON (%s) — leaving settings unchanged",
            config_url, e,
        )
        return settings_v2

    # ── Apply each field defensively ─────────────────────────────────
    raw_payer = payload.get("gatewayPayerAddress")
    raw_rate = payload.get("gatewayLeg2RatePerGb")

    payer_applied = False
    rate_applied = False

    # gateway_payer_address — only set if currently empty AND coord gave a non-empty value.
    if not escrow.gateway_payer_address:
        if isinstance(raw_payer, str) and raw_payer.strip():
            escrow.gateway_payer_address = raw_payer.strip()
            payer_applied = True
        else:
            logger.warning(
                "escrow config sync: coord returned empty/missing gatewayPayerAddress — skipping",
            )

    # leg2_rate_per_gb — apply coord's value whenever we got past the
    # early-skip (which only fires when BOTH timestamp AND rate are
    # already set). Reaching here means at least one of the two is
    # missing, so the coord's value is authoritative. This also covers
    # the test.97 backfill scenario: rate=1e15 (bootstrap stale value),
    # timestamp=null → must be overridden.
    if raw_rate is None:
        logger.warning(
            "escrow config sync: coord response is missing gatewayLeg2RatePerGb — skipping",
        )
    else:
        try:
            rate_int = int(raw_rate)
        except (TypeError, ValueError):
            logger.warning(
                "escrow config sync: coord returned non-integer "
                "gatewayLeg2RatePerGb=%r — skipping",
                raw_rate,
            )
        else:
            if rate_int <= 0:
                logger.warning(
                    "escrow config sync: coord returned gatewayLeg2RatePerGb=%d "
                    "(treated as not-yet-configured) — skipping",
                    rate_int,
                )
            else:
                old_rate = escrow.leg2_rate_per_gb
                new_rate_str = str(rate_int)
                if old_rate != new_rate_str:
                    if old_rate:
                        logger.info(
                            "escrow config sync: overriding stale leg2_rate_per_gb=%s with coord value %s",
                            old_rate, new_rate_str,
                        )
                    escrow.leg2_rate_per_gb = new_rate_str
                    rate_applied = True

    # Stamp the timestamp only if we actually wrote something. If neither
    # field was applied (coord returned 0/empty for both, or both were
    # already set), there's nothing to remember and no reason to skip
    # future syncs.
    if payer_applied or rate_applied:
        escrow.synced_from_coord_at = datetime.now(tz=UTC).isoformat()
        logger.info(
            "escrow config synced from %s — leg2_rate_per_gb=%s wei, "
            "gateway_payer_address=%s, synced_at=%s",
            config_url,
            escrow.leg2_rate_per_gb,
            escrow.gateway_payer_address,
            escrow.synced_from_coord_at,
        )

    return settings_v2
