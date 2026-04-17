"""
Payment Coordinator — escrow deposit and batch payment recording.

Extracted from agents/orchestrator.py for separation of concerns.
The orchestrator delegates all payment I/O here; it only builds the
list of payment dicts and calls record_batch().

Responsibilities:
  - deposit_escrow()  — approve USDC spend + deposit into PaymentEscrow contract
  - record_batch()    — persist PaymentLog rows in a single DB commit + broadcast
"""

import asyncio
import uuid
import structlog

from typing import Callable, Awaitable
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3

from settings import settings
from db.models import PaymentLog
from models.payment import PaymentEvent
from payments.nanopayments import usdc_to_base_units

log = structlog.get_logger()

# Minimal ABI for PaymentEscrow.deposit()
ESCROW_DEPOSIT_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "search_id", "type": "bytes32"},
            {"name": "amount",    "type": "uint256"},
        ],
        "outputs": [],
    }
]

# Minimal ABI for USDC.approve()
USDC_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]


class PaymentCoordinator:
    """
    Handles all on-chain and off-chain payment operations for a pipeline run.

    Args:
        db:           Async SQLAlchemy session (owned by orchestrator)
        w3:           Web3 instance connected to Arc RPC
        private_key:  Orchestrator's private key for signing escrow txs
        broadcast_fn: Optional WebSocket broadcast callable
    """

    # Minimal ABI for PaymentEscrow.refund()
    ESCROW_REFUND_ABI = [
        {
            "name": "refund",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "search_id", "type": "bytes32"}],
            "outputs": [],
        }
    ]

    def __init__(
        self,
        db: AsyncSession,
        w3: Web3,
        private_key: str,
        broadcast_fn: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._w3 = w3
        self._private_key = private_key
        self._broadcast = broadcast_fn

    def _run_sync(self, fn, *args, **kwargs):
        """Run a synchronous web3 call in the default thread pool."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def deposit_escrow(self, search_id: str, budget_usdc: float = 0.30) -> str | None:
        """
        Approve USDC spend and deposit into PaymentEscrow contract.

        Returns the escrow deposit tx hash, or None if escrow is not deployed
        (dev mode) or the transaction fails.
        """
        if not settings.payment_escrow_address:
            log.warning("escrow_not_deployed", note="skipping — dev mode")
            return None

        budget_units = usdc_to_base_units(budget_usdc)
        search_id_bytes = bytes.fromhex(search_id.replace("-", ""))[:32]

        try:
            account = self._w3.eth.account.from_key(self._private_key)

            usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.usdc_contract_address),
                abi=USDC_APPROVE_ABI,
            )
            nonce = await self._run_sync(
                self._w3.eth.get_transaction_count, account.address
            )
            gas_price = await self._run_sync(lambda: self._w3.eth.gas_price)
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(settings.payment_escrow_address),
                budget_units,
            ).build_transaction({
                "from":     account.address,
                "nonce":    nonce,
                "gas":      100_000,
                "gasPrice": gas_price,
            })
            signed = self._w3.eth.account.sign_transaction(approve_tx, self._private_key)
            await self._run_sync(self._w3.eth.send_raw_transaction, signed.raw_transaction)

            escrow = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.payment_escrow_address),
                abi=ESCROW_DEPOSIT_ABI,
            )
            nonce2 = await self._run_sync(
                self._w3.eth.get_transaction_count, account.address
            )
            gas_price2 = await self._run_sync(lambda: self._w3.eth.gas_price)
            deposit_tx = escrow.functions.deposit(
                search_id_bytes, budget_units
            ).build_transaction({
                "from":     account.address,
                "nonce":    nonce2,
                "gas":      200_000,
                "gasPrice": gas_price2,
            })
            signed2 = self._w3.eth.account.sign_transaction(deposit_tx, self._private_key)
            tx_hash = await self._run_sync(
                self._w3.eth.send_raw_transaction, signed2.raw_transaction
            )
            receipt = await self._run_sync(
                self._w3.eth.wait_for_transaction_receipt, tx_hash, 60
            )

            if receipt.get("status") == 0:
                log.error("escrow_deposit_reverted", tx=tx_hash.hex())
                return None

            log.info("escrow_deposited", amount_usdc=budget_usdc, tx=tx_hash.hex())
            return tx_hash.hex()

        except Exception as exc:
            log.error("escrow_deposit_failed", error=str(exc))
            return None

    async def refund_unused_escrow(
        self, search_id: str, total_spent_usdc: float, budget_usdc: float = 0.30
    ) -> str | None:
        """
        Refund any unused escrow budget to the recruiter.
        Returns the refund tx hash, or None if no refund is needed / contract unavailable.
        """
        if not settings.payment_escrow_address:
            return None

        unused = budget_usdc - total_spent_usdc
        if unused <= 0:
            return None

        search_id_bytes = bytes.fromhex(search_id.replace("-", ""))[:32]
        try:
            account = self._w3.eth.account.from_key(self._private_key)
            escrow = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.payment_escrow_address),
                abi=self.ESCROW_REFUND_ABI,
            )
            nonce = await self._run_sync(
                self._w3.eth.get_transaction_count, account.address
            )
            gas_price = await self._run_sync(lambda: self._w3.eth.gas_price)
            tx = escrow.functions.refund(search_id_bytes).build_transaction({
                "from":     account.address,
                "nonce":    nonce,
                "gas":      200_000,
                "gasPrice": gas_price,
            })
            signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = await self._run_sync(
                self._w3.eth.send_raw_transaction, signed.raw_transaction
            )
            receipt = await self._run_sync(
                self._w3.eth.wait_for_transaction_receipt, tx_hash, 60
            )

            if receipt.get("status") == 0:
                log.error("refund_reverted", tx=tx_hash.hex())
                return None

            log.info("escrow_refunded", search_id=search_id, unused_usdc=unused, tx=tx_hash.hex())
            return tx_hash.hex()

        except Exception as exc:
            log.error("refund_failed", search_id=search_id, error=str(exc))
            return None

    async def record_batch(self, search_id: str, records: list[dict]) -> None:
        """
        Persist a batch of payment records in a single DB commit,
        then broadcast each event to connected WebSocket clients.

        Each record dict must have:
            action_type, paying_agent, receiving_agent, amount_usdc
        """
        if not records:
            return

        search_uuid = uuid.UUID(search_id)
        for rec in records:
            self._db.add(PaymentLog(
                search_id=search_uuid,
                action_type=rec["action_type"],
                paying_agent=rec["paying_agent"],
                receiving_agent=rec["receiving_agent"],
                amount_usdc=rec["amount_usdc"],
                status="confirmed",
            ))

        await self._db.commit()

        if self._broadcast:
            for rec in records:
                event = PaymentEvent(
                    search_id=search_id,
                    action_type=rec["action_type"],
                    paying_agent=rec["paying_agent"],
                    receiving_agent=rec["receiving_agent"],
                    amount_usdc=rec["amount_usdc"],
                    status="confirmed",
                )
                try:
                    await self._broadcast(search_id, event.model_dump(mode="json"))
                except Exception:
                    pass
