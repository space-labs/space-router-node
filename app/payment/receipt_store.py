"""SQLite-backed store for Leg 2 receipts on the provider side.

A receipt starts life ``unsigned`` right after the provider's relay ends
(``signature IS NULL``) and becomes ``signed`` once the coord API returns
the gateway's EIP-712 signature. ``--claim`` CLI only submits signed
receipts on-chain.

Uses stdlib ``sqlite3`` via ``asyncio.to_thread`` to keep the provider's
dependency surface minimal (providers run on user machines).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from app.payment import reasons
from app.payment.eip712 import Receipt

logger = logging.getLogger(__name__)

# Schema v3 adds failure-tracking columns: sign_attempts, claim_attempts,
# last_error_code, last_error_detail, last_attempt_at, locked. These
# support the non-blocking "Claim outstanding" UX and the 2-try cap with
# a terminal lock after repeated failure.
#
# Schema v4 adds ``transient_attempts`` to the submitter resilience track
# (P4) — counts consecutive 429 / 5xx / timeout / DNS failures so the
# poller can apply per-row exponential backoff and escalate after ~24h.
# Distinct from ``sign_attempts`` (which only counts permanent failures
# against the 2-try retry budget).
#
# Schema v5 adds two columns to the settlement-hardening track (P3):
#
# - ``reconcile_block_number`` (nullable) — block height at which the
#   reaper marked a row as ``claim_tx_hash="external"`` after seeing
#   ``isNonceUsed`` flip to true. Used to defer trusting that decision
#   until ``FINALITY_BLOCKS_FOR_RECONCILE`` blocks pass; otherwise a
#   stale-fork RPC could yield a false positive (L4).
# - ``claim_tx_pending`` (nullable) — the deterministic tx hash we
#   computed locally before broadcasting the claim. Persisted BEFORE
#   ``send_raw_transaction`` so a crash between broadcast and
#   ``mark_claimed`` can be reconciled via ``isNonceUsed`` on restart
#   instead of re-submitting the same nonces (L5).
_SCHEMA_VERSION = 5

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signed_receipts (
    request_uuid            TEXT PRIMARY KEY,
    tunnel_request_id       TEXT,
    client_address          TEXT NOT NULL,
    node_address            TEXT NOT NULL,
    data_amount             INTEGER NOT NULL,
    total_price             INTEGER NOT NULL,
    signature               TEXT,
    created_at              INTEGER NOT NULL,
    claimed_at              INTEGER,
    claim_tx_hash           TEXT,
    sign_attempts           INTEGER NOT NULL DEFAULT 0,
    claim_attempts          INTEGER NOT NULL DEFAULT 0,
    last_error_code         TEXT,
    last_error_detail       TEXT,
    last_attempt_at         INTEGER,
    locked                  INTEGER NOT NULL DEFAULT 0,
    transient_attempts      INTEGER NOT NULL DEFAULT 0,
    reconcile_block_number  INTEGER,
    claim_tx_pending        TEXT
);

CREATE INDEX IF NOT EXISTS idx_signed_receipts_unclaimed
    ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_signed_receipts_unsigned
    ON signed_receipts (created_at) WHERE signature IS NULL;

CREATE INDEX IF NOT EXISTS idx_signed_receipts_failed
    ON signed_receipts (last_attempt_at)
    WHERE last_error_code IS NOT NULL AND claimed_at IS NULL;
"""


@dataclass
class StoredReceipt:
    receipt: Receipt
    signature: str | None
    created_at: int
    claimed_at: int | None
    claim_tx_hash: str | None
    tunnel_request_id: str | None = None
    sign_attempts: int = 0
    claim_attempts: int = 0
    last_error_code: str | None = None
    last_error_detail: str | None = None
    last_attempt_at: int | None = None
    locked: bool = False
    transient_attempts: int = 0
    reconcile_block_number: int | None = None
    claim_tx_pending: str | None = None

    @property
    def view(self) -> str:
        """Derived state for UI/CLI classification.

        Order of precedence: claimed → failed_terminal → claimable →
        failed_retryable → pending_sign. Derived rather than stored so we
        never carry drift between ``status`` and the underlying counters.
        """
        if self.claimed_at is not None:
            return "claimed"
        if self.locked:
            return "failed_terminal"
        if self.signature is not None:
            if self.last_error_code and reasons.is_claim_code(self.last_error_code):
                return "failed_retryable"
            return "claimable"
        if self.last_error_code and reasons.is_sign_code(self.last_error_code):
            return "failed_retryable"
        return "pending_sign"


class ReceiptStore:
    def __init__(self, db_path: str | os.PathLike) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def initialize(self) -> None:
        # Idempotent: reading PRAGMA user_version takes no write lock, so
        # callers in the hot path (submitter, poller) don't serialize on
        # the SQLite writer once the DB is at the current schema version.
        if self._initialized:
            # rc.6 BLK-2: wipe_operational_state may have deleted the
            # file under us. Verify the schema actually exists on disk
            # before short-circuiting; otherwise reset and re-run.
            if self._path.exists():
                return
            self._initialized = False

        # rc.5 minor #4: a fresh receipts.db can hit
        # "sqlite3.OperationalError: database is locked" on the first
        # connection when ``PRAGMA journal_mode=WAL`` runs concurrently
        # with another writer (e.g. the GUI's submitter racing with a
        # CLI `--receipts` invocation, or two daemon worker tasks
        # initialising in parallel). Retry with backoff before giving
        # up — keeps startup self-healing on a cold DB.
        delays = (0.1, 0.5, 2.0)
        attempt = 0

        def _do() -> None:
            with self._connect() as conn:
                cur = conn.execute("PRAGMA user_version")
                current = cur.fetchone()[0]
                if current == _SCHEMA_VERSION:
                    return
                if current < 1:
                    # Fresh DB — build at the current schema directly.
                    conn.executescript(_SCHEMA_SQL)
                else:
                    if current < 2:
                        # v1 → v2: add tunnel_request_id, relax signature NOT NULL.
                        # SQLite can't drop NOT NULL in place — rebuild the table.
                        conn.executescript("""
                            CREATE TABLE signed_receipts_new (
                                request_uuid      TEXT PRIMARY KEY,
                                tunnel_request_id TEXT,
                                client_address    TEXT NOT NULL,
                                node_address      TEXT NOT NULL,
                                data_amount       INTEGER NOT NULL,
                                total_price       INTEGER NOT NULL,
                                signature         TEXT,
                                created_at        INTEGER NOT NULL,
                                claimed_at        INTEGER,
                                claim_tx_hash     TEXT
                            );
                            INSERT INTO signed_receipts_new
                                (request_uuid, client_address, node_address,
                                 data_amount, total_price, signature, created_at,
                                 claimed_at, claim_tx_hash)
                            SELECT request_uuid, client_address, node_address,
                                   data_amount, total_price, signature, created_at,
                                   claimed_at, claim_tx_hash
                              FROM signed_receipts;
                            DROP TABLE signed_receipts;
                            ALTER TABLE signed_receipts_new RENAME TO signed_receipts;
                            DROP INDEX IF EXISTS idx_signed_receipts_unclaimed;
                            DROP INDEX IF EXISTS idx_signed_receipts_unsigned;
                            CREATE INDEX idx_signed_receipts_unclaimed
                                ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;
                            CREATE INDEX idx_signed_receipts_unsigned
                                ON signed_receipts (created_at) WHERE signature IS NULL;
                        """)
                    if current < 3:
                        # v2 → v3: failure tracking columns. Add each column
                        # only if it's missing so a half-finished previous
                        # run (e.g. concurrent writer blocked the version
                        # bump) is self-healing instead of crashing with
                        # "duplicate column name".
                        existing = {
                            row[1] for row in conn.execute(
                                "PRAGMA table_info(signed_receipts)"
                            )
                        }
                        v3_columns = [
                            ("sign_attempts",
                             "INTEGER NOT NULL DEFAULT 0"),
                            ("claim_attempts",
                             "INTEGER NOT NULL DEFAULT 0"),
                            ("last_error_code", "TEXT"),
                            ("last_error_detail", "TEXT"),
                            ("last_attempt_at", "INTEGER"),
                            ("locked",
                             "INTEGER NOT NULL DEFAULT 0"),
                        ]
                        for name, ddl in v3_columns:
                            if name not in existing:
                                conn.execute(
                                    f"ALTER TABLE signed_receipts "
                                    f"ADD COLUMN {name} {ddl}"
                                )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS "
                            "idx_signed_receipts_failed "
                            "ON signed_receipts (last_attempt_at) "
                            "WHERE last_error_code IS NOT NULL "
                            "AND claimed_at IS NULL"
                        )
                    if current < 4:
                        # v3 → v4: per-row transient backoff counter for
                        # the receipt submitter (P4). Idempotent: only
                        # add the column if it isn't already present.
                        existing = {
                            row[1] for row in conn.execute(
                                "PRAGMA table_info(signed_receipts)"
                            )
                        }
                        if "transient_attempts" not in existing:
                            conn.execute(
                                "ALTER TABLE signed_receipts "
                                "ADD COLUMN transient_attempts "
                                "INTEGER NOT NULL DEFAULT 0"
                            )
                    if current < 5:
                        # v4 → v5: settlement-hardening columns (P3).
                        # Idempotent ALTER TABLE ADD COLUMN — mirrors
                        # the v3→v4 leg so a half-finished migration
                        # (concurrent writer scenario) is self-healing
                        # rather than crashing on duplicate-column.
                        existing = {
                            row[1] for row in conn.execute(
                                "PRAGMA table_info(signed_receipts)"
                            )
                        }
                        v5_columns = [
                            ("reconcile_block_number", "INTEGER"),
                            ("claim_tx_pending", "TEXT"),
                        ]
                        for name, ddl in v5_columns:
                            if name not in existing:
                                conn.execute(
                                    f"ALTER TABLE signed_receipts "
                                    f"ADD COLUMN {name} {ddl}"
                                )
                # Persist the version bump. Read it back to verify the
                # write actually stuck — a previous bug surfaced when a
                # concurrent writer caused the pragma to silently fail
                # after the schema changes had already applied.
                conn.execute(
                    f"PRAGMA user_version = {_SCHEMA_VERSION}"
                )
                new_version = conn.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                if new_version != _SCHEMA_VERSION:
                    raise sqlite3.OperationalError(
                        f"schema migration completed but user_version "
                        f"stuck at {new_version} (expected "
                        f"{_SCHEMA_VERSION}) — likely a concurrent "
                        f"writer. Retry after stopping other processes."
                    )

        last_err: Exception | None = None
        for delay in (0.0, *delays):
            if delay:
                await asyncio.sleep(delay)
            attempt += 1
            try:
                await asyncio.to_thread(_do)
                last_err = None
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower():
                    logger.info(
                        "receipt_store init: database is locked "
                        "(attempt %d/%d) — retrying in %.1fs",
                        attempt, len(delays) + 1, delay if delay else 0.1,
                    )
                    last_err = e
                    continue
                # Non-locked OperationalError → propagate immediately.
                raise
        if last_err is not None:
            raise last_err
        self._initialized = True

    async def store_unsigned(self, receipt: Receipt, request_id: str) -> None:
        """Record a receipt that hasn't been signed yet. Idempotent."""
        now = int(time.time())

        def _do() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO signed_receipts
                        (request_uuid, tunnel_request_id, client_address,
                         node_address, data_amount, total_price,
                         signature, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        receipt.request_uuid,
                        request_id,
                        receipt.client_address,
                        receipt.node_address,
                        int(receipt.data_amount),
                        int(receipt.total_price),
                        now,
                    ),
                )

        await asyncio.to_thread(_do)

    async def mark_signed(self, request_uuid: str, signature: str) -> bool:
        """Fill in the signature for an unsigned row. Returns True if updated.

        Also resets ``transient_attempts`` so a row that previously hit
        429/5xx storms but eventually got signed starts fresh on any
        future failures (e.g. claim-side flakes never see leftover
        sign-side counter state).
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET signature = ?,
                           transient_attempts = 0
                     WHERE request_uuid = ?
                       AND signature IS NULL
                    """,
                    (signature, request_uuid),
                )
                return cur.rowcount

        n = await asyncio.to_thread(_do)
        return n > 0

    async def mark_transient_attempt(self, request_uuid: str) -> int:
        """Bump the per-row transient retry counter and stamp last_attempt_at.

        Used by the submitter when the coord API returns 429 / 5xx /
        network-timeout / DNS error. Caller decides backoff via
        :func:`transient_backoff_seconds`. Returns the new
        ``transient_attempts`` value, or 0 if the row was not updated
        (already claimed, locked, or signed).

        Does NOT touch ``last_error_code`` — that field is reserved for
        terminal codes; transient retries should not surface as
        failed_retryable until the budget is exhausted.
        """
        now = int(time.time())

        def _do() -> int | None:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET transient_attempts = transient_attempts + 1,
                           last_attempt_at    = ?
                     WHERE request_uuid = ?
                       AND claimed_at IS NULL
                       AND signature IS NULL
                       AND locked = 0
                    """,
                    (now, request_uuid),
                )
                if cur.rowcount == 0:
                    return None
                row = conn.execute(
                    "SELECT transient_attempts FROM signed_receipts "
                    "WHERE request_uuid = ?",
                    (request_uuid,),
                ).fetchone()
                return int(row[0]) if row else 0

        result = await asyncio.to_thread(_do)
        return int(result or 0)

    async def reset_transient_attempts(self, request_uuid: str) -> bool:
        """Clear ``transient_attempts`` without otherwise modifying the row."""
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET transient_attempts = 0
                     WHERE request_uuid = ?
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def store(self, receipt: Receipt, signature: str) -> None:
        """Backward-compatible: store a receipt that's already signed."""
        now = int(time.time())

        def _do() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO signed_receipts
                        (request_uuid, tunnel_request_id, client_address,
                         node_address, data_amount, total_price, signature, created_at)
                    VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_uuid) DO UPDATE
                        SET signature = excluded.signature
                        WHERE signed_receipts.signature IS NULL
                    """,
                    (
                        receipt.request_uuid,
                        receipt.client_address,
                        receipt.node_address,
                        int(receipt.data_amount),
                        int(receipt.total_price),
                        signature,
                        now,
                    ),
                )

        await asyncio.to_thread(_do)

    # Every SELECT that hydrates a StoredReceipt pulls the same column set so
    # _row_to_stored below stays the single source of truth.
    _STORED_COLUMNS = (
        "request_uuid, tunnel_request_id, client_address, node_address, "
        "data_amount, total_price, signature, created_at, claimed_at, "
        "claim_tx_hash, sign_attempts, claim_attempts, last_error_code, "
        "last_error_detail, last_attempt_at, locked, transient_attempts, "
        "reconcile_block_number, claim_tx_pending"
    )

    @staticmethod
    def _row_to_stored(r: tuple) -> StoredReceipt:
        return StoredReceipt(
            receipt=Receipt(
                client_address=r[2],
                node_address=r[3],
                request_uuid=r[0],
                data_amount=int(r[4]),
                total_price=int(r[5]),
            ),
            signature=r[6],
            created_at=int(r[7]),
            claimed_at=int(r[8]) if r[8] is not None else None,
            claim_tx_hash=r[9],
            tunnel_request_id=r[1],
            sign_attempts=int(r[10] or 0),
            claim_attempts=int(r[11] or 0),
            last_error_code=r[12],
            last_error_detail=r[13],
            last_attempt_at=int(r[14]) if r[14] is not None else None,
            locked=bool(r[15]),
            transient_attempts=int(r[16] or 0),
            reconcile_block_number=int(r[17]) if r[17] is not None else None,
            claim_tx_pending=r[18],
        )

    async def unclaimed(
        self, limit: int = 50, include_retryable: bool = False,
    ) -> list[StoredReceipt]:
        """Return SIGNED, not-yet-claimed, not-locked receipts.

        By default, rows in a ``failed_retryable`` state (last claim reverted,
        under the attempt cap) are excluded — callers must opt in via
        ``include_retryable=True`` so the default ``--claim`` run preserves
        its pre-v1.5 behaviour of only picking up fresh signed receipts.

        Rows currently in ``CLAIM_TX_TIMEOUT`` state are **always**
        excluded regardless of ``include_retryable`` — the tx is
        ambiguous (may still land on-chain), and re-broadcasting before
        the reaper reconciles risks silent double-submit revert loops.
        The reaper clears the error once it resolves via ``isNonceUsed``.
        """
        if include_retryable:
            retryable_clause = (
                "AND (last_error_code IS NULL OR last_error_code != ?)"
            )
            params = [reasons.CLAIM_TX_TIMEOUT, int(limit)]
        else:
            retryable_clause = (
                "AND (last_error_code IS NULL OR last_error_code = '')"
            )
            params = [int(limit)]

        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                    FROM signed_receipts
                    WHERE claimed_at IS NULL
                      AND signature IS NOT NULL
                      AND locked = 0
                      {retryable_clause}
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)

    async def mark_claimed(self, request_uuids: list[str], tx_hash: str) -> int:
        if not request_uuids:
            return 0
        now = int(time.time())

        def _do() -> int:
            placeholders = ",".join("?" * len(request_uuids))
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE signed_receipts
                       SET claimed_at = ?, claim_tx_hash = ?
                     WHERE request_uuid IN ({placeholders})
                       AND claimed_at IS NULL
                    """,
                    [now, tx_hash, *request_uuids],
                )
                return cur.rowcount

        return await asyncio.to_thread(_do)

    async def count_unclaimed(self) -> tuple[int, int]:
        """Return (signed, not-yet-claimed, not-locked count, total_price_sum).

        Excludes locked rows and rows currently in a retryable-failure
        state so this number matches the UX promise of "ready to claim".
        """
        def _do() -> tuple[int, int]:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total_price), 0)
                      FROM signed_receipts
                     WHERE claimed_at IS NULL
                       AND signature IS NOT NULL
                       AND locked = 0
                       AND (last_error_code IS NULL OR last_error_code = '')
                    """
                ).fetchone()
            return int(row[0] or 0), int(row[1] or 0)

        return await asyncio.to_thread(_do)

    async def count_unsigned(self) -> int:
        def _do() -> int:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM signed_receipts WHERE signature IS NULL"
                ).fetchone()
            return int(row[0] or 0)

        return await asyncio.to_thread(_do)

    async def count_unsigned_ready(self, now: int | None = None) -> int:
        """Count rows that are unsigned, not locked, no terminal error, and
        whose transient backoff window has elapsed.

        Used by the receipt poller to gate its outbound GETs — when every
        outstanding row is still in its 429/5xx backoff window, the
        poller skips the tick rather than burning a request the coord
        API will rate-limit again.
        """
        if now is None:
            now = int(time.time())

        def _do() -> int:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                      FROM signed_receipts
                     WHERE signature IS NULL
                       AND claimed_at IS NULL
                       AND locked = 0
                       AND (last_error_code IS NULL OR last_error_code = '')
                       AND (
                           last_attempt_at IS NULL
                        OR transient_attempts = 0
                        OR ? >= last_attempt_at + MIN(
                               60 * (1 << MIN(transient_attempts, 16)),
                               3600
                           )
                       )
                    """,
                    (int(now),),
                ).fetchone()
            return int(row[0] or 0)

        return await asyncio.to_thread(_do)

    async def escalate_transient_budget_exhausted(
        self, threshold: int, code: str, detail: str | None = None,
    ) -> list[str]:
        """Move pending_sign rows past the transient budget into
        ``failed_retryable``.

        Called by the poller every tick. Sets ``last_error_code`` to a
        sign-side code so the row appears in the failed-retryable
        bucket; preserves ``transient_attempts`` for diagnostics.
        Returns the list of UUIDs that escalated this call.
        """
        now = int(time.time())

        def _do() -> list[str]:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT request_uuid FROM signed_receipts
                     WHERE signature IS NULL
                       AND claimed_at IS NULL
                       AND locked = 0
                       AND (last_error_code IS NULL OR last_error_code = '')
                       AND transient_attempts >= ?
                    """,
                    (int(threshold),),
                ).fetchall()
                if not rows:
                    return []
                uuids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(uuids))
                conn.execute(
                    f"""
                    UPDATE signed_receipts
                       SET last_error_code   = ?,
                           last_error_detail = ?,
                           last_attempt_at   = ?
                     WHERE request_uuid IN ({placeholders})
                    """,
                    [code, detail, now, *uuids],
                )
                return uuids

        return await asyncio.to_thread(_do)

    # ------------------------------------------------------------------
    # Failure-tracking API (v3+)
    # ------------------------------------------------------------------

    async def mark_sign_failed(
        self, request_uuid: str, code: str, detail: str | None = None,
    ) -> bool:
        """Record a sign-side failure. Locks the row if it hit the cap.

        ``counts_against_retry_budget`` determines whether attempts
        increment — transient errors (network, timeout) don't count so
        a flaky coord API never terminally locks a row.

        Idempotent: calling twice with the same uuid on an already-locked
        row is a no-op. Never un-claims a claimed row.
        """
        now = int(time.time())
        counts = reasons.counts_against_retry_budget(code)
        cap = reasons.MAX_SIGN_ATTEMPTS

        def _do() -> int:
            with self._connect() as conn:
                if counts:
                    cur = conn.execute(
                        """
                        UPDATE signed_receipts
                           SET sign_attempts     = sign_attempts + 1,
                               last_error_code   = ?,
                               last_error_detail = ?,
                               last_attempt_at   = ?,
                               locked            = CASE
                                   WHEN sign_attempts + 1 >= ? THEN 1
                                   ELSE locked
                               END
                         WHERE request_uuid = ?
                           AND claimed_at IS NULL
                           AND signature IS NULL
                           AND locked = 0
                        """,
                        (code, detail, now, cap, request_uuid),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE signed_receipts
                           SET last_error_code   = ?,
                               last_error_detail = ?,
                               last_attempt_at   = ?
                         WHERE request_uuid = ?
                           AND claimed_at IS NULL
                           AND signature IS NULL
                           AND locked = 0
                        """,
                        (code, detail, now, request_uuid),
                    )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def mark_claim_failed(
        self, request_uuids: list[str], code: str, detail: str | None = None,
    ) -> int:
        """Record a claim-side failure across a batch.

        Locks any row that hits :data:`MAX_CLAIM_ATTEMPTS`. Returns the
        number of rows updated. Skips rows that are already claimed or
        locked so a late-arriving tx success can't get overwritten.
        """
        if not request_uuids:
            return 0
        now = int(time.time())
        counts = reasons.counts_against_retry_budget(code)
        cap = reasons.MAX_CLAIM_ATTEMPTS

        def _do() -> int:
            placeholders = ",".join("?" * len(request_uuids))
            with self._connect() as conn:
                if counts:
                    cur = conn.execute(
                        f"""
                        UPDATE signed_receipts
                           SET claim_attempts    = claim_attempts + 1,
                               last_error_code   = ?,
                               last_error_detail = ?,
                               last_attempt_at   = ?,
                               locked            = CASE
                                   WHEN claim_attempts + 1 >= ? THEN 1
                                   ELSE locked
                               END
                         WHERE request_uuid IN ({placeholders})
                           AND claimed_at IS NULL
                           AND locked = 0
                        """,
                        [code, detail, now, cap, *request_uuids],
                    )
                else:
                    cur = conn.execute(
                        f"""
                        UPDATE signed_receipts
                           SET last_error_code   = ?,
                               last_error_detail = ?,
                               last_attempt_at   = ?
                         WHERE request_uuid IN ({placeholders})
                           AND claimed_at IS NULL
                           AND locked = 0
                        """,
                        [code, detail, now, *request_uuids],
                    )
                return cur.rowcount

        return await asyncio.to_thread(_do)

    async def clear_error(self, request_uuid: str) -> bool:
        """Clear last_error_code without changing counters.

        Used by the reaper after resolving a ``CLAIM_TX_TIMEOUT`` to put
        the row back into the normal claim queue.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET last_error_code = NULL,
                           last_error_detail = NULL
                     WHERE request_uuid = ?
                       AND locked = 0
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def lock(self, request_uuid: str) -> bool:
        """Manually move a row to terminal-failed state."""
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET locked = 1,
                           last_attempt_at = ?
                     WHERE request_uuid = ?
                       AND claimed_at IS NULL
                       AND locked = 0
                    """,
                    (int(time.time()), request_uuid),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def unlock_for_retry(self, request_uuid: str) -> bool:
        """Operator override: clear the lock and reset counters.

        Use when out-of-band action (e.g. support registered the node)
        means a previously-terminal row can actually succeed now.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET locked = 0,
                           sign_attempts = 0,
                           claim_attempts = 0,
                           last_error_code = NULL,
                           last_error_detail = NULL
                     WHERE request_uuid = ?
                       AND claimed_at IS NULL
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    _VIEW_WHERE = {
        "claimed":
            "claimed_at IS NOT NULL",
        "failed_terminal":
            "claimed_at IS NULL AND locked = 1",
        "claimable":
            "claimed_at IS NULL AND locked = 0 AND signature IS NOT NULL "
            "AND (last_error_code IS NULL OR last_error_code = '')",
        "failed_retryable":
            "claimed_at IS NULL AND locked = 0 AND last_error_code IS NOT NULL "
            "AND last_error_code != ''",
        "pending_sign":
            "claimed_at IS NULL AND locked = 0 AND signature IS NULL "
            "AND (last_error_code IS NULL OR last_error_code = '')",
        "all":
            "1=1",
    }

    async def list_by_view(
        self, view: str = "all", limit: int = 100, offset: int = 0,
    ) -> list[StoredReceipt]:
        """Paginated view for the GUI / CLI. View names match StoredReceipt.view."""
        where = self._VIEW_WHERE.get(view)
        if where is None:
            raise ValueError(f"unknown view: {view!r}")

        # Lazy-init: GUI may call this before the submitter has had a
        # chance to bootstrap the schema (e.g. when escrow.enabled was
        # left false on test.95 but the Earnings card still polls). The
        # check is idempotent — see initialize() for the short-circuit.
        await self.initialize()

        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                      FROM signed_receipts
                     WHERE {where}
                     ORDER BY COALESCE(last_attempt_at, created_at) DESC
                     LIMIT ? OFFSET ?
                    """,
                    (int(limit), int(offset)),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)

    async def get_by_uuid(self, request_uuid: str) -> StoredReceipt | None:
        await self.initialize()

        def _do() -> StoredReceipt | None:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT {self._STORED_COLUMNS} FROM signed_receipts "
                    "WHERE request_uuid = ?",
                    (request_uuid,),
                ).fetchone()
            return self._row_to_stored(row) if row else None

        return await asyncio.to_thread(_do)

    async def summary(self) -> dict:
        """Counts and total-price-sum per view — cheap single-query rollup."""
        await self.initialize()

        def _do() -> dict:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                      SUM(CASE WHEN claimed_at IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN claimed_at IS NULL AND locked = 1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN claimed_at IS NULL AND locked = 0
                               AND signature IS NOT NULL
                               AND (last_error_code IS NULL OR last_error_code = '')
                               THEN 1 ELSE 0 END),
                      SUM(CASE WHEN claimed_at IS NULL AND locked = 0
                               AND last_error_code IS NOT NULL AND last_error_code != ''
                               THEN 1 ELSE 0 END),
                      SUM(CASE WHEN claimed_at IS NULL AND locked = 0
                               AND signature IS NULL
                               AND (last_error_code IS NULL OR last_error_code = '')
                               THEN 1 ELSE 0 END)
                    FROM signed_receipts
                    """
                ).fetchone()
                # rc.6 MAJ-4: SQLite SUM is INT64 (max ~9.22e18). At
                # 10^24 wei/GB rates the cumulative claimable_total_price
                # exceeds INT64 with as few as 12 receipts and the query
                # raises OperationalError("integer overflow"), which the
                # GUI catches and renders as zeros — cascades into BLK-1
                # (Earnings empty). Fetch per-row prices and aggregate
                # in Python (arbitrary precision).
                prices = conn.execute(
                    """
                    SELECT total_price FROM signed_receipts
                     WHERE claimed_at IS NULL AND locked = 0
                       AND signature IS NOT NULL
                       AND (last_error_code IS NULL OR last_error_code = '')
                    """
                ).fetchall()
            return {
                "claimed": int(row[0] or 0),
                "failed_terminal": int(row[1] or 0),
                "claimable": int(row[2] or 0),
                "failed_retryable": int(row[3] or 0),
                "pending_sign": int(row[4] or 0),
                "claimable_total_price": sum(int(p[0]) for p in prices),
            }

        return await asyncio.to_thread(_do)

    async def list_recently_claimed(
        self, younger_than_seconds: int, limit: int = 50,
    ) -> list[StoredReceipt]:
        """Rows marked ``claimed`` within the last ``younger_than_seconds``
        — used by the reaper's reorg-reconciliation pass. Excludes rows
        with the synthetic ``tx_hash="external"`` (already reconciled).
        """
        await self.initialize()
        cutoff = int(time.time()) - int(younger_than_seconds)

        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                      FROM signed_receipts
                     WHERE claimed_at IS NOT NULL
                       AND claimed_at >= ?
                       AND (claim_tx_hash IS NULL OR claim_tx_hash != 'external')
                     ORDER BY claimed_at ASC
                     LIMIT ?
                    """,
                    (cutoff, int(limit)),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)

    async def revert_claimed(self, request_uuid: str) -> bool:
        """Undo a claim because the chain says the nonce isn't used anymore.

        Used by the reaper's reorg-reconciliation. Clears ``claimed_at``
        and ``claim_tx_hash`` so the row re-enters the claim queue. Does
        NOT increment ``claim_attempts`` — reorg isn't the operator's
        fault and shouldn't burn their retry budget.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET claimed_at = NULL,
                           claim_tx_hash = NULL,
                           last_error_code = NULL,
                           last_error_detail = NULL
                     WHERE request_uuid = ?
                       AND claimed_at IS NOT NULL
                       AND locked = 0
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def list_timed_out_claims(self, older_than_seconds: int) -> list[StoredReceipt]:
        """Inflight rows the reaper should reconcile via ``isNonceUsed``.

        Picks up rows with a non-null ``claim_tx_pending`` breadcrumb whose
        last error is either ``CLAIM_TX_TIMEOUT`` (broadcast succeeded but
        confirmation timed out) or ``CLAIM_RPC_UNREACHABLE`` (broadcast
        raised mid-send so the tx may have leaked to mempool). Both cases
        require an on-chain check to determine whether the tx actually
        landed before the row can be safely retried — clearing the
        breadcrumb without that check risks double-claim.
        """
        cutoff = int(time.time()) - int(older_than_seconds)

        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                      FROM signed_receipts
                     WHERE claimed_at IS NULL
                       AND locked = 0
                       AND claim_tx_pending IS NOT NULL
                       AND last_error_code IN (?, ?)
                       AND last_attempt_at IS NOT NULL
                       AND last_attempt_at <= ?
                     ORDER BY last_attempt_at ASC
                    """,
                    (reasons.CLAIM_TX_TIMEOUT, reasons.CLAIM_RPC_UNREACHABLE, cutoff),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)

    # ------------------------------------------------------------------
    # P3/L4 — stale-fork reconcile finality
    # ------------------------------------------------------------------

    async def mark_external_with_block(
        self, request_uuids: list[str], block_number: int,
    ) -> int:
        """Mark a batch of timed-out rows as ``claim_tx_hash="external"``
        and stamp the block height of the chain query that resolved them.

        The block height is consulted later (see
        :func:`list_pending_external_unfinalized`) to decide whether the
        decision is past finality (trustworthy) or still inside the
        soak window (must be re-verified next tick in case the RPC was
        on a stale fork). Idempotent: a re-run with the same UUIDs
        won't bump ``claimed_at`` again — the SQL is gated on
        ``claimed_at IS NULL``.
        """
        if not request_uuids:
            return 0
        now = int(time.time())

        def _do() -> int:
            placeholders = ",".join("?" * len(request_uuids))
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE signed_receipts
                       SET claimed_at             = ?,
                           claim_tx_hash          = 'external',
                           reconcile_block_number = ?
                     WHERE request_uuid IN ({placeholders})
                       AND claimed_at IS NULL
                    """,
                    [now, int(block_number), *request_uuids],
                )
                return cur.rowcount

        return await asyncio.to_thread(_do)

    async def list_pending_external_unfinalized(
        self, current_block: int, finality_blocks: int, limit: int = 100,
    ) -> list[StoredReceipt]:
        """External-marked rows still inside the finality soak window.

        Returns rows where ``claim_tx_hash="external"`` and
        ``current_block - reconcile_block_number < finality_blocks``.
        Once the difference crosses ``finality_blocks`` the row is
        considered settled permanently — see L4 in the v1.5 plan.

        Rows missing ``reconcile_block_number`` (legacy pre-v5 rows
        that were marked external before this column existed) are
        treated as past finality and excluded from re-checks.
        """
        soak_floor = int(current_block) - int(finality_blocks) + 1

        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                      FROM signed_receipts
                     WHERE claim_tx_hash = 'external'
                       AND claimed_at IS NOT NULL
                       AND reconcile_block_number IS NOT NULL
                       AND reconcile_block_number >= ?
                     ORDER BY reconcile_block_number ASC
                     LIMIT ?
                    """,
                    (int(soak_floor), int(limit)),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)

    async def revert_external_mark(self, request_uuid: str) -> bool:
        """Undo an in-flight ``external`` mark when the chain disagrees.

        Used when the reaper's finality recheck flips ``isNonceUsed``
        back to ``false``. The row returns to the claim queue with
        ``CLAIM_TX_TIMEOUT`` so the regular timeout-resolution loop
        owns it again. ``claim_attempts`` is NOT incremented — the
        operator didn't cause the stale-fork situation.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET claimed_at             = NULL,
                           claim_tx_hash          = NULL,
                           reconcile_block_number = NULL,
                           last_error_code        = ?,
                           last_error_detail      = NULL,
                           last_attempt_at        = ?
                     WHERE request_uuid = ?
                       AND claim_tx_hash = 'external'
                       AND locked = 0
                    """,
                    (reasons.CLAIM_TX_TIMEOUT, int(time.time()), request_uuid),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    # ------------------------------------------------------------------
    # P3/L5 — claim-in-flight sentinel
    # ------------------------------------------------------------------

    async def set_claim_tx_pending(
        self, request_uuids: list[str], tx_hash: str,
    ) -> int:
        """Stamp the deterministic tx hash on rows about to be broadcast.

        Called BEFORE :func:`web3.eth.send_raw_transaction` so a crash
        between broadcast and ``mark_claimed`` leaves a breadcrumb the
        in-flight reconciler can resolve via ``isNonceUsed`` on
        startup. Idempotent: only updates rows that aren't already
        claimed or pending another tx.
        """
        if not request_uuids:
            return 0

        def _do() -> int:
            placeholders = ",".join("?" * len(request_uuids))
            with self._connect() as conn:
                cur = conn.execute(
                    f"""
                    UPDATE signed_receipts
                       SET claim_tx_pending = ?
                     WHERE request_uuid IN ({placeholders})
                       AND claimed_at IS NULL
                       AND locked = 0
                    """,
                    [tx_hash, *request_uuids],
                )
                return cur.rowcount

        return await asyncio.to_thread(_do)

    async def clear_claim_tx_pending(self, request_uuid: str) -> bool:
        """Drop the in-flight breadcrumb without touching anything else.

        Used when the reconciler determined the on-chain nonce is NOT
        used — the tx never landed, the row should re-enter the claim
        queue with a fresh attempt budget.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET claim_tx_pending = NULL
                     WHERE request_uuid = ?
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def reset_claim_attempts(self, request_uuid: str) -> bool:
        """Reset ``claim_attempts`` to zero for a single row.

        Used by the in-flight reconciler when it determines a pending
        tx was never broadcast — the failed-pre-broadcast row shouldn't
        burn the operator's retry budget on the next claim run.
        """
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET claim_attempts = 0
                     WHERE request_uuid = ?
                       AND claimed_at IS NULL
                       AND locked = 0
                    """,
                    (request_uuid,),
                )
                return cur.rowcount

        return (await asyncio.to_thread(_do)) > 0

    async def list_inflight(self, limit: int = 100) -> list[StoredReceipt]:
        """Rows with a pending broadcast that never confirmed.

        Selects ``claim_tx_pending IS NOT NULL AND claimed_at IS NULL``.
        Locked rows are excluded — once a row is terminal the
        operator's intervention is required regardless of pending
        breadcrumbs. The in-flight reconciler walks this set on daemon
        startup.
        """
        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT {self._STORED_COLUMNS}
                      FROM signed_receipts
                     WHERE claim_tx_pending IS NOT NULL
                       AND claimed_at IS NULL
                       AND locked = 0
                     ORDER BY last_attempt_at ASC
                     LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)


_singleton: ReceiptStore | None = None


def get_store(db_path: str | os.PathLike) -> ReceiptStore:
    global _singleton
    cached = _singleton is not None and str(_singleton.path) == str(Path(db_path).expanduser())
    if not cached:
        _singleton = ReceiptStore(db_path)
        logger.info(
            "[MAJ-6-diag] receipt_store.get_store FRESH: id=%s path=%s",
            id(_singleton), _singleton.path,
        )
    else:
        logger.info(
            "[MAJ-6-diag] receipt_store.get_store CACHED: id=%s path=%s",
            id(_singleton), _singleton.path,
        )
    return _singleton


def clear_singleton() -> None:
    """Invalidate the cached store. Call after disk-level operations
    that delete or replace receipts.db so the next get_store() returns
    a fresh instance whose ``_initialized`` reflects on-disk reality."""
    global _singleton
    logger.info(
        "[MAJ-6-diag] receipt_store.clear_singleton CALLED: prev id=%s",
        id(_singleton) if _singleton is not None else "None",
    )
    _singleton = None
