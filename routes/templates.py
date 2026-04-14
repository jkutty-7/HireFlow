"""
Search template routes — save and reuse common JDs.

POST   /api/templates            — create a template
GET    /api/templates            — list all active templates
DELETE /api/templates/{id}       — soft-delete a template
POST   /api/templates/{id}/use   — launch a new search from this template
"""

import uuid
import structlog

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from auth.dependencies import verify_api_key
from db.database import get_db
from db.models import Search, SearchTemplate
from models.search import (
    TemplateCreate,
    TemplateResponse,
    SearchStatus,
)

log = structlog.get_logger()
router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.post("", response_model=TemplateResponse, status_code=201)
async def create_template(
    body: TemplateCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """Save a reusable JD template."""
    template = SearchTemplate(
        name=body.name,
        description=body.description,
        template_jd=body.template_jd,
        location_filter=body.location_filter,
        max_candidates=body.max_candidates,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    log.info("template_created", template_id=str(template.id), name=template.name)
    return template


@router.get("", response_model=list[TemplateResponse])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """List all non-deleted templates, newest first."""
    result = await db.execute(
        select(SearchTemplate)
        .where(SearchTemplate.is_deleted == False)  # noqa: E712
        .order_by(SearchTemplate.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """Soft-delete a template (sets is_deleted=True)."""
    result = await db.execute(
        select(SearchTemplate).where(
            SearchTemplate.id == template_id,
            SearchTemplate.is_deleted == False,  # noqa: E712
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    await db.execute(
        update(SearchTemplate)
        .where(SearchTemplate.id == template_id)
        .values(is_deleted=True)
    )
    await db.commit()
    log.info("template_deleted", template_id=str(template_id))


@router.post("/{template_id}/use", response_model=SearchStatus, status_code=202)
async def use_template(
    template_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """
    Launch a new pipeline search from a saved template.
    Increments use_count. Returns 202 with the new search_id.
    """
    from datetime import datetime, timezone
    from routes.search import _run_pipeline

    result = await db.execute(
        select(SearchTemplate).where(
            SearchTemplate.id == template_id,
            SearchTemplate.is_deleted == False,  # noqa: E712
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Create the Search row
    search_id = uuid.uuid4()
    search = Search(
        id=search_id,
        job_description=template.template_jd,
        status="running",
    )
    db.add(search)

    # Increment use count
    await db.execute(
        update(SearchTemplate)
        .where(SearchTemplate.id == template_id)
        .values(use_count=SearchTemplate.use_count + 1)
    )
    await db.commit()

    background_tasks.add_task(
        _run_pipeline,
        str(search_id),
        template.template_jd,
        template.max_candidates,
        template.location_filter,
    )

    log.info(
        "template_used",
        template_id=str(template_id),
        search_id=str(search_id),
    )

    return SearchStatus(
        search_id=search_id,
        status="running",
        stage="parse_jd",
        progress_pct=0.0,
        total_spent_usdc=0.0,
        transaction_count=0,
        created_at=datetime.now(timezone.utc),
    )
