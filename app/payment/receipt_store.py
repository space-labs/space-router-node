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

from app.payment.eip712 import Receipt

logger = logging.getLogger(__name__)

# Schema version 2 adds ``tunnel_request_id`` (gateway's tunnel correlation id)
# and relaxes ``signature`` to NULL for receipts awaiting coord API / gateway
# signing.
_SCHEMA_VERSION = 2

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
    claim_tx_hash     TEXT
);

CREATE INDEX IF NOT EXISTS idx_signed_receipts_unclaimed
    ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_signed_receipts_unsigned
    ON signed_receipts (created_at) WHERE signature IS NULL;
"""


@dataclass
class StoredReceipt:
    receipt: Receipt
    signature: str | None
    created_at: int
    claimed_at: int | None
    claim_tx_hash: str | None
    tunnel_request_id: str | None = None


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
                    conn.executescript(_SCHEMA_SQL)
                elif current < 2:
                    # v1 → v2 migration: add new columns, relax signature NOT NULL.
                    # SQLite doesn't support dropping NOT NULL directly — rebuild.
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
                        CREATE INDEX idx_signed_receipts_unclaimed
                            ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;
                        CREATE INDEX idx_signed_receipts_unsigned
                            ON signed_receipts (created_at) WHERE signature IS NULL;
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

    async def unclaimed(self, limit: int = 50) -> list[StoredReceipt]:
        """Return up to ``limit`` SIGNED receipts that haven't been settled."""
        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT request_uuid, tunnel_request_id, client_address, node_address,
                           data_amount, total_price, signature,
                           created_at, claimed_at, claim_tx_hash
                    FROM signed_receipts
                    WHERE claimed_at IS NULL
                      AND signature IS NOT NULL
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            return [
                StoredReceipt(
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
                )
                for r in rows
            ]

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
        """Return (signed-but-unclaimed count, total_price_sum)."""
        def _do() -> tuple[int, int]:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total_price), 0)
                      FROM signed_receipts
                     WHERE claimed_at IS NULL
                       AND signature IS NOT NULL
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


_singleton: ReceiptStore | None = None


def get_store(db_path: str | os.PathLike) -> ReceiptStore:
    global _singleton
    if _singleton is None or str(_singleton.path) != str(Path(db_path).expanduser()):
        _singleton = ReceiptStore(db_path)
    return _singleton
