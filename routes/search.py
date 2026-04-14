"""
Search routes — triggers the full HireFlow pipeline.

POST /api/search        — start a new hiring search
GET  /api/search/{id}  — get search status + result
"""

import csv
import io
import uuid
import asyncio
import structlog

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from auth.dependencies import verify_api_key
from db.database import get_db
from db.models import Search, Candidate as CandidateORM
from models.search import SearchRequest, SearchStatus, SearchResult, CandidateStatusUpdate
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
    _: str = Depends(verify_api_key),
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
        request.location_filter,
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
    _: str = Depends(verify_api_key),
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
    _: str = Depends(verify_api_key),
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

    from models.candidate import GitHubProfile

    def _github_data(profile_json: dict | None) -> "GitHubProfile | None":
        if not profile_json:
            return None
        try:
            return GitHubProfile(**profile_json)
        except Exception:
            return None

    scored = [
        CandidateScored(
            apollo_id=c.apollo_id,
            name=c.name,
            title=c.title,
            company=c.company,
            linkedin_url=c.linkedin_url,
            location=c.location,
            source=c.source or "apollo",
            source_repos=c.source_repos or [],
            email=c.email,
            email_confidence=c.email_confidence,
            email_status=c.email_status,
            github_username=c.github_username,
            github_data=_github_data(c.github_profile),
            skill_match_pct=c.skill_match_pct or 0,
            seniority_fit=c.seniority_fit or "unknown",
            github_score=c.github_score or 0,
            # Use persisted email_validity if available; fall back to recomputing for old records
            email_validity=c.email_validity or _map_email_validity(c.email_status, c.email_confidence),
            composite_score=c.composite_score or 0,
            rank_justification=c.rank_justification or "",
            rank=c.rank,
            candidate_id=c.id,
            skill_match_detail=c.skill_match_detail or [],
            skill_gaps=c.skill_gaps or [],
            # Enrichment fields — now persisted; no longer recomputed on every request
            skills=c.skills or [],
            employment_history=c.employment_history or [],
            avg_tenure_months=c.avg_tenure_months,
            is_job_hopper=c.is_job_hopper or False,
            career_trajectory=c.career_trajectory,
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

async def _run_pipeline(
    search_id: str,
    job_description: str,
    max_candidates: int,
    location_filter: str | None = None,
):
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
            final_state = await orchestrator.run(search_id, job_description, location_filter)
            # Notify any connected frontend clients that the pipeline finished
            await ws_manager.broadcast(search_id, {
                "type":            "pipeline_complete",
                "candidate_count": len(final_state.get("candidates_scored", [])),
                "total_usdc":      final_state.get("total_spent_usdc", 0.0),
            })
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
    if status == "risky":
        return "risky"
    if status and status != "unknown":
        return "unverified"
    return "missing"


@router.get("/{search_id}/candidates")
async def get_candidates(
    search_id: uuid.UUID,
    status: str | None = Query(None, description="Filter by recruiter_status (e.g. contacted)"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    List candidates for a search, optionally filtered by recruiter pipeline stage.
    Returns lightweight dicts (id, name, rank, composite_score, recruiter_status, notes).
    """
    result = await db.execute(select(Search).where(Search.id == search_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Search not found")

    q = select(CandidateORM).where(CandidateORM.search_id == search_id)
    if status:
        q = q.where(CandidateORM.recruiter_status == status)
    q = q.order_by(CandidateORM.rank)

    cands = (await db.execute(q)).scalars().all()
    return [
        {
            "id":               str(c.id),
            "name":             c.name,
            "title":            c.title,
            "company":          c.company,
            "rank":             c.rank,
            "composite_score":  c.composite_score,
            "recruiter_status": c.recruiter_status,
            "notes":            c.notes,
            "status_updated_at": c.status_updated_at.isoformat() if c.status_updated_at else None,
        }
        for c in cands
    ]


@router.patch("/{search_id}/candidates/{candidate_id}/status", status_code=200)
async def update_candidate_status(
    search_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: CandidateStatusUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Update the recruiter pipeline status for a single candidate.
    Optionally attach a recruiter note.
    """
    result = await db.execute(
        select(CandidateORM).where(
            CandidateORM.id == candidate_id,
            CandidateORM.search_id == search_id,
        )
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    now = datetime.now(timezone.utc)
    values: dict = {"recruiter_status": body.status, "status_updated_at": now}
    if body.note is not None:
        values["notes"] = body.note

    await db.execute(
        update(CandidateORM)
        .where(CandidateORM.id == candidate_id)
        .values(**values)
    )
    await db.commit()

    log.info(
        "candidate_status_updated",
        candidate_id=str(candidate_id),
        status=body.status,
    )
    return {"id": str(candidate_id), "status": body.status, "updated_at": now.isoformat()}


@router.get("/{search_id}/export")
async def export_candidates_csv(
    search_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Stream a CSV of all ranked candidates for a completed search.
    Columns: rank, name, title, company, location, email, email_validity,
             composite_score, skill_match_pct, github_score, seniority_fit,
             recruiter_status, skill_gaps, rank_justification, linkedin_url, notes
    """
    result = await db.execute(select(Search).where(Search.id == search_id))
    search = result.scalar_one_or_none()
    if not search:
        raise HTTPException(status_code=404, detail="Search not found")
    if search.status not in ("complete", "failed"):
        raise HTTPException(status_code=202, detail="Search still running — export not yet available")

    cands_result = await db.execute(
        select(CandidateORM)
        .where(CandidateORM.search_id == search_id)
        .order_by(CandidateORM.rank)
    )
    candidates = cands_result.scalars().all()

    fieldnames = [
        "rank", "name", "title", "company", "location",
        "email", "email_validity", "composite_score", "skill_match_pct",
        "github_score", "seniority_fit", "recruiter_status",
        "skill_gaps", "rank_justification", "linkedin_url", "notes",
    ]

    def _generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        yield buf.getvalue()

        for c in candidates:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writerow({
                "rank":             c.rank or "",
                "name":             c.name,
                "title":            c.title or "",
                "company":          c.company or "",
                "location":         c.location or "",
                "email":            c.email or "",
                "email_validity":   c.email_validity or "",
                "composite_score":  c.composite_score or 0,
                "skill_match_pct":  c.skill_match_pct or 0,
                "github_score":     c.github_score or 0,
                "seniority_fit":    c.seniority_fit or "",
                "recruiter_status": c.recruiter_status or "new",
                "skill_gaps":       "; ".join(c.skill_gaps or []),
                "rank_justification": c.rank_justification or "",
                "linkedin_url":     c.linkedin_url or "",
                "notes":            c.notes or "",
            })
            yield buf.getvalue()

    filename = f"hireflow_candidates_{search_id}.csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{search_id}/intelligence")
async def get_intelligence_report(
    search_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Return the Talent Intelligence Report for a completed search.
    Includes top-3 summary, red flags, interview questions per candidate, and search quality score.
    """
    try:
        search_uuid = uuid.UUID(search_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid search_id format")

    result = await db.execute(select(Search).where(Search.id == search_uuid))
    search = result.scalar_one_or_none()

    if not search:
        raise HTTPException(status_code=404, detail="Search not found")

    if search.status != "complete":
        raise HTTPException(status_code=202, detail=f"Search is {search.status} — intelligence report not yet available")

    if not search.intelligence_report:
        raise HTTPException(status_code=404, detail="Intelligence report not available for this search")

    return search.intelligence_report
