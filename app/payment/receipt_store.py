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
_SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signed_receipts (
    request_uuid      TEXT PRIMARY KEY,
    tunnel_request_id TEXT,
    client_address    TEXT NOT NULL,
    node_address      TEXT NOT NULL,
    data_amount       INTEGER NOT NULL,
    total_price       INTEGER NOT NULL,
    signature         TEXT,
    created_at        INTEGER NOT NULL,
    claimed_at        INTEGER,
    claim_tx_hash     TEXT,
    sign_attempts     INTEGER NOT NULL DEFAULT 0,
    claim_attempts    INTEGER NOT NULL DEFAULT 0,
    last_error_code   TEXT,
    last_error_detail TEXT,
    last_attempt_at   INTEGER,
    locked            INTEGER NOT NULL DEFAULT 0
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
            return

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
                        # v2 → v3: failure tracking columns. ALTER TABLE ADD COLUMN
                        # is safe for nullable / default-value columns in SQLite.
                        conn.executescript("""
                            ALTER TABLE signed_receipts
                                ADD COLUMN sign_attempts INTEGER NOT NULL DEFAULT 0;
                            ALTER TABLE signed_receipts
                                ADD COLUMN claim_attempts INTEGER NOT NULL DEFAULT 0;
                            ALTER TABLE signed_receipts
                                ADD COLUMN last_error_code TEXT;
                            ALTER TABLE signed_receipts
                                ADD COLUMN last_error_detail TEXT;
                            ALTER TABLE signed_receipts
                                ADD COLUMN last_attempt_at INTEGER;
                            ALTER TABLE signed_receipts
                                ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;
                            CREATE INDEX IF NOT EXISTS idx_signed_receipts_failed
                                ON signed_receipts (last_attempt_at)
                                WHERE last_error_code IS NOT NULL AND claimed_at IS NULL;
                        """)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

        await asyncio.to_thread(_do)
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
        """Fill in the signature for an unsigned row. Returns True if updated."""
        def _do() -> int:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE signed_receipts
                       SET signature = ?
                     WHERE request_uuid = ?
                       AND signature IS NULL
                    """,
                    (signature, request_uuid),
                )
                return cur.rowcount

        n = await asyncio.to_thread(_do)
        return n > 0

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
        "last_error_detail, last_attempt_at, locked"
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
        )

    async def unclaimed(
        self, limit: int = 50, include_retryable: bool = False,
    ) -> list[StoredReceipt]:
        """Return SIGNED, not-yet-claimed, not-locked receipts.

        By default, rows in a ``failed_retryable`` state (last claim reverted,
        under the attempt cap) are excluded — callers must opt in via
        ``include_retryable=True`` so the default ``--claim`` run preserves
        its pre-v1.5 behaviour of only picking up fresh signed receipts.
        """
        retryable_clause = "" if include_retryable else \
            "AND (last_error_code IS NULL OR last_error_code = '')"

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
                    (int(limit),),
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
                               THEN 1 ELSE 0 END),
                      COALESCE(SUM(
                        CASE WHEN claimed_at IS NULL AND locked = 0
                             AND signature IS NOT NULL
                             AND (last_error_code IS NULL OR last_error_code = '')
                             THEN total_price ELSE 0 END
                      ), 0)
                    FROM signed_receipts
                    """
                ).fetchone()
            return {
                "claimed": int(row[0] or 0),
                "failed_terminal": int(row[1] or 0),
                "claimable": int(row[2] or 0),
                "failed_retryable": int(row[3] or 0),
                "pending_sign": int(row[4] or 0),
                "claimable_total_price": int(row[5] or 0),
            }

        return await asyncio.to_thread(_do)

    async def list_timed_out_claims(self, older_than_seconds: int) -> list[StoredReceipt]:
        """Rows with ``CLAIM_TX_TIMEOUT`` last error whose last_attempt_at
        is older than ``older_than_seconds``. Used by the reaper.
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
                       AND last_error_code = ?
                       AND last_attempt_at IS NOT NULL
                       AND last_attempt_at <= ?
                     ORDER BY last_attempt_at ASC
                    """,
                    (reasons.CLAIM_TX_TIMEOUT, cutoff),
                ).fetchall()
            return [self._row_to_stored(r) for r in rows]

        return await asyncio.to_thread(_do)


_singleton: ReceiptStore | None = None


def get_store(db_path: str | os.PathLike) -> ReceiptStore:
    global _singleton
    if _singleton is None or str(_singleton.path) != str(Path(db_path).expanduser()):
        _singleton = ReceiptStore(db_path)
    return _singleton
