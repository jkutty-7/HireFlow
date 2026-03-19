"""
Search routes — triggers the full HireFlow pipeline.

POST /api/search        — start a new hiring search
GET  /api/search/{id}  — get search status + result
"""

import uuid
import asyncio
import structlog

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import get_db
from db.models import Search, Candidate as CandidateORM
from models.search import SearchRequest, SearchStatus, SearchResult
from models.candidate import CandidateScored

log = structlog.get_logger()
router = APIRouter(prefix="/api/search", tags=["search"])

# Background task registry so we can check status
_running_searches: dict[str, asyncio.Task] = {}


@router.post("", response_model=SearchStatus, status_code=202)
async def start_search(
    request: SearchRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Start a new hiring search pipeline.
    Returns 202 Accepted immediately — pipeline runs in background.
    Poll GET /api/search/{id} for status and results.
    """
    from datetime import datetime, timezone

    search_id = uuid.uuid4()
    search = Search(
        id=search_id,
        job_description=request.job_description,
        recruiter_wallet_id=request.recruiter_wallet_id,
        status="running",
    )
    db.add(search)
    await db.commit()

    # Launch pipeline in background
    background_tasks.add_task(
        _run_pipeline,
        str(search_id),
        request.job_description,
        request.max_candidates,
    )

    log.info("search_started", search_id=str(search_id))

    return SearchStatus(
        search_id=search_id,
        status="running",
        stage="parse_jd",
        progress_pct=0.0,
        total_spent_usdc=0.0,
        transaction_count=0,
        created_at=datetime.now(timezone.utc),
    )


@router.get("/{search_id}/status", response_model=SearchStatus)
async def get_search_status(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Poll the status of a running or completed search."""
    result = await db.execute(select(Search).where(Search.id == search_id))
    search = result.scalar_one_or_none()
    if not search:
        raise HTTPException(status_code=404, detail="Search not found")

    return SearchStatus(
        search_id=search.id,
        status=search.status,
        stage=None,
        progress_pct=_estimate_progress(search.status),
        total_spent_usdc=search.total_spent_usdc,
        transaction_count=search.transaction_count,
        created_at=search.created_at,
        completed_at=search.completed_at,
    )


@router.get("/{search_id}/results", response_model=SearchResult)
async def get_search_results(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve the ranked candidate results for a completed search."""
    result = await db.execute(select(Search).where(Search.id == search_id))
    search = result.scalar_one_or_none()
    if not search:
        raise HTTPException(status_code=404, detail="Search not found")
    if search.status not in ("complete", "failed"):
        raise HTTPException(status_code=202, detail="Search still running")

    # Load candidates
    cands_result = await db.execute(
        select(CandidateORM)
        .where(CandidateORM.search_id == search_id)
        .order_by(CandidateORM.rank)
    )
    candidates = cands_result.scalars().all()

    scored = [
        CandidateScored(
            name=c.name,
            title=c.title,
            company=c.company,
            linkedin_url=c.linkedin_url,
            location=c.location,
            email=c.email,
            email_confidence=c.email_confidence,
            email_status=c.email_status,
            github_username=c.github_username,
            skill_match_pct=c.skill_match_pct or 0,
            seniority_fit=c.seniority_fit or "unknown",
            github_score=c.github_score or 0,
            email_validity=_map_email_validity(c.email_status, c.email_confidence),
            composite_score=c.composite_score or 0,
            rank_justification=c.rank_justification or "",
            rank=c.rank,
            candidate_id=c.id,
        )
        for c in candidates
    ]

    from db.models import PaymentLog as PaymentLogORM
    from models.payment import TransactionLog
    from sqlalchemy import select as sa_select

    logs_result = await db.execute(
        sa_select(PaymentLogORM).where(PaymentLogORM.search_id == search_id)
    )
    payment_logs_orm = logs_result.scalars().all()
    payment_logs = [
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
        for p in payment_logs_orm
    ]

    return SearchResult(
        search_id=search.id,
        status=search.status,
        candidates=scored,
        total_spent_usdc=search.total_spent_usdc,
        transaction_count=search.transaction_count,
        payment_log=payment_logs,
        escrow_tx_hash=search.escrow_tx_hash,
        refund_tx_hash=search.refund_tx_hash,
        completed_at=search.completed_at,
    )


# ─── Background Pipeline Runner ──────────────────────────────────────────────

async def _run_pipeline(search_id: str, job_description: str, max_candidates: int):
    """Background task: runs the full orchestrator pipeline."""
    from db.database import AsyncSessionLocal
    from agents.orchestrator import HireFlowOrchestrator
    from payments.wallet_manager import LocalWalletManager
    from main import ws_manager  # WebSocket broadcast

    async with AsyncSessionLocal() as db:
        wallet_mgr = LocalWalletManager()

        orchestrator = HireFlowOrchestrator(
            db=db,
            wallet_manager=wallet_mgr,
            broadcast_fn=ws_manager.broadcast,
        )
        try:
            await orchestrator.run(search_id, job_description)
        except Exception as exc:
            log.error("pipeline_failed", search_id=search_id, error=str(exc))
            from sqlalchemy import update
            await db.execute(
                update(Search)
                .where(Search.id == uuid.UUID(search_id))
                .values(status="failed")
            )
            await db.commit()
        finally:
            await orchestrator.close()
            await wallet_mgr.close()


def _estimate_progress(status: str) -> float:
    return {"pending": 0, "running": 50, "complete": 100, "failed": 0}.get(status, 0)


def _map_email_validity(status: str | None, confidence: int | None) -> str:
    if status == "valid" and (confidence or 0) >= 80:
        return "verified"
    if status and status != "unknown":
        return "unverified"
    return "missing"
