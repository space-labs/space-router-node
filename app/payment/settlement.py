"""Provider SettlementManager — accumulates signed receipts and settles on-chain.

The Provider receives EIP-712 signed receipts from the Gateway (Leg 2).
These are stored and periodically submitted to the TokenPaymentEscrow
contract via ``claimBatch()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3

from app.payment.eip712 import Receipt

logger = logging.getLogger(__name__)

_ABI_PATH = Path(__file__).parent / "escrow_abi.json"


@dataclass
class SettlementStats:
    total: int = 0
    unsettled: int = 0
    settled: int = 0
    failed: int = 0


class SettlementManager:
    """Accumulates signed receipts and settles them on-chain in batches.

    If Supabase is not configured, falls back to in-memory storage.
    """

    def __init__(
        self,
        rpc_url: str = "",
        contract_address: str = "",
        private_key: str = "",
        batch_size: int = 50,
        settlement_interval: int = 3600,
        supabase_url: str = "",
        supabase_key: str = "",
    ) -> None:
        self._rpc_url = rpc_url
        self._contract_address = contract_address
        self._private_key = private_key
        self._batch_size = batch_size
        self._settlement_interval = settlement_interval
        self._supabase_url = supabase_url
        self._supabase_key = supabase_key

        self._w3: Web3 | None = None
        self._contract = None
        self._account = Account.from_key(private_key) if private_key else None

        # In-memory fallback
        self._receipts: list[dict] = []
        self._settlement_task: asyncio.Task | None = None

    @property
    def address(self) -> str:
        return self._account.address if self._account else ""

    def _init_web3(self) -> None:
        if self._w3 is not None:
            return
        if not self._rpc_url or not self._contract_address:
            raise RuntimeError("RPC URL and contract address required")

        with open(_ABI_PATH) as f:
            abi_data = json.load(f)

        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        self._contract = self._w3.eth.contract(
            address=to_checksum_address(self._contract_address),
            abi=abi_data["escrow"],
        )
        logger.info(
            "SettlementManager: contract=%s rpc=%s",
            self._contract_address, self._rpc_url,
        )

    def add_receipt(self, receipt: Receipt, signature: str) -> None:
        """Store a signed receipt for later batch settlement."""
        entry = {
            "request_uuid": receipt.request_uuid,
            "client_address": receipt.client_address,
            "node_address": receipt.node_address,
            "data_amount": receipt.data_amount,
            "total_price": str(receipt.total_price),
            "signature": signature,
            "status": "unsettled",
            "claim_tx_hash": None,
            "created_at": time.time(),
            "settled_at": None,
            "source": "node_leg2",
        }

        if self._supabase_url:
            self._store_to_supabase(entry)
        else:
            self._receipts.append(entry)

    def _store_to_supabase(self, entry: dict) -> None:
        import httpx
        try:
            resp = httpx.post(
                f"{self._supabase_url}/rest/v1/signed_receipts",
                json=entry,
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=10.0,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to store receipt in Supabase: %s", e)
            self._receipts.append(entry)

    def get_unsettled_batch(self, limit: int | None = None) -> list[dict]:
        batch_size = limit or self._batch_size
        if self._supabase_url:
            return self._fetch_from_supabase(batch_size)
        return [r for r in self._receipts if r["status"] == "unsettled"][:batch_size]

    def _fetch_from_supabase(self, limit: int) -> list[dict]:
        import httpx
        try:
            resp = httpx.get(
                f"{self._supabase_url}/rest/v1/signed_receipts",
                params={
                    "status": "eq.unsettled",
                    "source": "eq.node_leg2",
                    "order": "created_at.asc",
                    "limit": str(limit),
                },
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Failed to fetch receipts from Supabase: %s", e)
            return []

    def mark_settled(self, request_uuids: list[str], tx_hash: str) -> None:
        now = time.time()
        if self._supabase_url:
            self._mark_status_supabase(request_uuids, "settled", tx_hash)
        for r in self._receipts:
            if r["request_uuid"] in request_uuids:
                r["status"] = "settled"
                r["claim_tx_hash"] = tx_hash
                r["settled_at"] = now

    def mark_failed(self, request_uuids: list[str]) -> None:
        if self._supabase_url:
            self._mark_status_supabase(request_uuids, "failed")
        for r in self._receipts:
            if r["request_uuid"] in request_uuids:
                r["status"] = "failed"

    def _mark_status_supabase(
        self, request_uuids: list[str], status: str, tx_hash: str = "",
    ) -> None:
        import httpx
        now = time.time()
        for uuid_val in request_uuids:
            try:
                update = {"status": status}
                if tx_hash:
                    update["claim_tx_hash"] = tx_hash
                if status == "settled":
                    update["settled_at"] = now
                resp = httpx.patch(
                    f"{self._supabase_url}/rest/v1/signed_receipts",
                    params={"request_uuid": f"eq.{uuid_val}"},
                    json=update,
                    headers={
                        "apikey": self._supabase_key,
                        "Authorization": f"Bearer {self._supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
            except Exception as e:
                logger.error("Failed to update receipt status in Supabase: %s", e)

    def get_stats(self) -> SettlementStats:
        total = len(self._receipts)
        unsettled = sum(1 for r in self._receipts if r["status"] == "unsettled")
        settled = sum(1 for r in self._receipts if r["status"] == "settled")
        failed = sum(1 for r in self._receipts if r["status"] == "failed")
        return SettlementStats(total=total, unsettled=unsettled, settled=settled, failed=failed)

    # ── On-chain Settlement ───────────────────────────────────────────

    def _send_tx(self, tx_func, gas: int = 200_000) -> str:
        if not self._account:
            raise RuntimeError("Private key required")
        self._init_web3()
        wallet = self._account.address

        tx = tx_func.build_transaction({
            "from": wallet,
            "nonce": self._w3.eth.get_transaction_count(wallet),
            "chainId": self._w3.eth.chain_id,
            "gas": gas,
        })
        try:
            est = self._w3.eth.estimate_gas(tx)
            tx["gas"] = int(est * 1.2)
        except Exception:
            pass

        signed = self._w3.eth.account.sign_transaction(tx, self._account.key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def claim_batch(self, receipts: list[Receipt], signatures: list[str]) -> str:
        """Submit a batch of signed receipts to claimBatch() on-chain."""
        if not receipts:
            raise ValueError("Empty batch")
        if len(receipts) != len(signatures):
            raise ValueError("receipts and signatures must have same length")

        self._init_web3()
        receipt_tuples = [r.to_contract_tuple() for r in receipts]
        sig_bytes = [bytes.fromhex(s.removeprefix("0x")) for s in signatures]

        gas = max(200_000, len(receipts) * 120_000)
        return self._send_tx(
            self._contract.functions.claimBatch(receipt_tuples, sig_bytes),
            gas=gas,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._settlement_interval > 0:
            self._settlement_task = asyncio.create_task(self._settlement_loop())
            logger.info(
                "Node SettlementManager started: batch=%d interval=%ds",
                self._batch_size, self._settlement_interval,
            )

    async def stop(self) -> None:
        if self._settlement_task and not self._settlement_task.done():
            self._settlement_task.cancel()
            try:
                await self._settlement_task
            except asyncio.CancelledError:
                pass

    async def _settlement_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settlement_interval)
                await self._settle_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Settlement loop error: %s", e)

    async def _settle_batch(self) -> None:
        batch = self.get_unsettled_batch()
        if not batch:
            return

        receipts = []
        signatures = []
        uuids = []
        for entry in batch:
            try:
                receipt = Receipt(
                    client_address=entry["client_address"],
                    node_address=entry["node_address"],
                    request_uuid=entry["request_uuid"],
                    data_amount=int(entry["data_amount"]),
                    total_price=int(entry["total_price"]),
                )
                receipts.append(receipt)
                signatures.append(entry["signature"])
                uuids.append(entry["request_uuid"])
            except (KeyError, ValueError) as e:
                logger.error("Skipping malformed receipt: %s", e)

        if not receipts:
            return

        logger.info("Settling batch of %d node receipts...", len(receipts))
        try:
            tx_hash = self.claim_batch(receipts, signatures)
            logger.info("Node settlement tx: %s (%d receipts)", tx_hash, len(receipts))
            self.mark_settled(uuids, tx_hash)
        except Exception as e:
            logger.error("Node settlement failed: %s", e)
            self.mark_failed(uuids)
