"""
Payment feed routes.

GET /api/payments/{search_id}/feed — full payment history for a search
"""

import uuid
import structlog

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import get_db
from db.models import PaymentLog as PaymentLogORM
from models.payment import TransactionLog

log = structlog.get_logger()
router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.get("/{search_id}/feed", response_model=list[TransactionLog])
async def get_payment_feed(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Return all payment events for a given search, ordered by creation time.
    These map 1:1 to Arc Block Explorer transactions.
    """
    result = await db.execute(
        select(PaymentLogORM)
        .where(PaymentLogORM.search_id == search_id)
        .order_by(PaymentLogORM.created_at)
    )
    logs = result.scalars().all()

    return [
        TransactionLog(
            id=p.id,
            search_id=p.search_id,
            action_type=p.action_type,
            paying_agent=p.paying_agent,
            receiving_agent=p.receiving_agent,
            amount_usdc=p.amount_usdc,
            arc_tx_hash=p.arc_tx_hash,
            status=p.status,
            created_at=p.created_at,
        )
        for p in logs
    ]


@router.get("/{search_id}/summary")
async def get_payment_summary(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return aggregate cost breakdown by action type for a search."""
    result = await db.execute(
        select(PaymentLogORM).where(PaymentLogORM.search_id == search_id)
    )
    logs = result.scalars().all()

    summary: dict[str, dict] = {}
    for log_entry in logs:
        key = log_entry.action_type
        if key not in summary:
            summary[key] = {"count": 0, "total_usdc": 0.0, "action_type": key}
        summary[key]["count"] += 1
        summary[key]["total_usdc"] += log_entry.amount_usdc

    total = sum(s["total_usdc"] for s in summary.values())
    tx_count = len(logs)

    return {
        "search_id": str(search_id),
        "breakdown": list(summary.values()),
        "total_usdc": round(total, 6),
        "transaction_count": tx_count,
        "ethereum_equivalent_gas": f"${total * 6:.2f}–${total * 30:.2f}",
    }
