"""SQLite-backed store for gateway-signed Leg 2 receipts.

After the gateway signs a receipt for relayed bandwidth, the provider stores
the signed receipt locally. The ``claim`` CLI command later reads unclaimed
receipts from this store and settles them on-chain via ``claimBatch()``.

Schema is versioned with a simple ``PRAGMA user_version``. The file is created
on first use; the directory is derived from settings (default ~/.spacerouter/).

Uses the stdlib ``sqlite3`` driver inside ``asyncio.to_thread`` rather than
``aiosqlite`` to keep the provider's dependency surface minimal (providers
run on user machines; smaller deps = fewer install failures).
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

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signed_receipts (
    request_uuid    TEXT PRIMARY KEY,
    client_address  TEXT NOT NULL,
    node_address    TEXT NOT NULL,
    data_amount     INTEGER NOT NULL,
    total_price     INTEGER NOT NULL,
    signature       TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER,
    claim_tx_hash   TEXT
);

CREATE INDEX IF NOT EXISTS idx_signed_receipts_unclaimed
    ON signed_receipts (claimed_at) WHERE claimed_at IS NULL;
"""


@dataclass
class StoredReceipt:
    receipt: Receipt
    signature: str
    created_at: int
    claimed_at: int | None
    claim_tx_hash: str | None


class ReceiptStore:
    """Async-friendly SQLite store for gateway-signed Leg 2 receipts."""

    def __init__(self, db_path: str | os.PathLike) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def initialize(self) -> None:
        def _do() -> None:
            with self._connect() as conn:
                conn.executescript(_SCHEMA_SQL)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        await asyncio.to_thread(_do)

    async def store(self, receipt: Receipt, signature: str) -> None:
        """Persist a gateway-signed receipt. Idempotent on request_uuid."""
        now = int(time.time())

        def _do() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO signed_receipts
                        (request_uuid, client_address, node_address,
                         data_amount, total_price, signature, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
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
        """Return up to ``limit`` receipts that have not been settled yet."""
        def _do() -> list[StoredReceipt]:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT request_uuid, client_address, node_address,
                           data_amount, total_price, signature,
                           created_at, claimed_at, claim_tx_hash
                    FROM signed_receipts
                    WHERE claimed_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            return [
                StoredReceipt(
                    receipt=Receipt(
                        client_address=r[1],
                        node_address=r[2],
                        request_uuid=r[0],
                        data_amount=int(r[3]),
                        total_price=int(r[4]),
                    ),
                    signature=r[5],
                    created_at=int(r[6]),
                    claimed_at=int(r[7]) if r[7] is not None else None,
                    claim_tx_hash=r[8],
                )
                for r in rows
            ]

        return await asyncio.to_thread(_do)

    async def mark_claimed(self, request_uuids: list[str], tx_hash: str) -> int:
        """Mark receipts as settled. Returns number of rows updated."""
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
        """Return (count, total_price_sum) of unclaimed receipts."""
        def _do() -> tuple[int, int]:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total_price), 0)
                      FROM signed_receipts
                     WHERE claimed_at IS NULL
                    """
                ).fetchone()
            return int(row[0] or 0), int(row[1] or 0)

        return await asyncio.to_thread(_do)


_singleton: ReceiptStore | None = None


def get_store(db_path: str | os.PathLike) -> ReceiptStore:
    """Module-level singleton so the proxy handler and CLI share one store."""
    global _singleton
    if _singleton is None or str(_singleton.path) != str(Path(db_path).expanduser()):
        _singleton = ReceiptStore(db_path)
    return _singleton
