"""
Integration tests for agents/orchestrator.py.

All external I/O is mocked:
  - LLM calls (jd_parser, jd_enhancement, scoring, talent_intelligence)
  - Apollo, GitHub, Hunter API calls
  - Blockchain / payment calls (AgentVerifier, PaymentCoordinator)

Tests assert the DB state transitions: running → complete / failed.
"""

import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from db.models import Search, Candidate as CandidateORM
from models.job import ParsedJD, EnhancedJD
from models.candidate import CandidateEnriched, CandidateScored
from models.intelligence import TalentIntelligenceReport
from payments.wallet_manager import LocalWalletManager


# ─── Shared mock data ─────────────────────────────────────────────────────────

def _make_parsed_jd() -> ParsedJD:
    return ParsedJD(
        skills=["Python", "FastAPI"],
        seniority="senior",
        location="Remote",
        years_exp=5,
        languages=["Python"],
        titles=["Backend Engineer"],
        keywords=[],
        raw_jd="Looking for a senior Python engineer with FastAPI.",
    )


def _make_candidate_enriched(name: str = "Alice Dev") -> CandidateEnriched:
    return CandidateEnriched(
        name=name,
        title="Senior Engineer",
        company="TechCo",
        location="Remote",
        skills=["Python", "FastAPI"],
        email=f"{name.lower().replace(' ', '')}@techco.com",
        email_status="valid",
        email_confidence=90,
        source="apollo",
    )


def _make_candidate_scored(name: str = "Alice Dev") -> CandidateScored:
    return CandidateScored(
        name=name,
        title="Senior Engineer",
        company="TechCo",
        location="Remote",
        skills=["Python", "FastAPI"],
        email=f"{name.lower().replace(' ', '')}@techco.com",
        email_status="valid",
        email_confidence=90,
        source="apollo",
        skill_match_pct=90.0,
        seniority_fit="match",
        github_score=60.0,
        email_validity="verified",
        composite_score=78.0,
        rank_justification="Strong Python match.",
        rank=1,
    )


def _make_intelligence_report(search_id: str) -> TalentIntelligenceReport:
    return TalentIntelligenceReport(
        search_id=search_id,
        top_3_summary="Excellent candidates.",
        search_quality_score=85,
        red_flags=[],
        recommended_jd_changes=[],
        interview_plans=[],
    )


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def search_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def running_search_in_db(db_session, search_id) -> Search:
    search = Search(
        id=uuid.UUID(search_id),
        job_description="Senior Python engineer with FastAPI skills.",
        status="running",
    )
    db_session.add(search)
    await db_session.commit()
    return search


def _all_mocks(search_id: str):
    """Return a context manager that patches all external calls in the orchestrator."""
    parsed_jd = _make_parsed_jd()
    candidates = [_make_candidate_enriched()]
    scored = [_make_candidate_scored()]
    report = _make_intelligence_report(search_id)

    return [
        patch("agents.orchestrator.enhance_job_description",
              new=AsyncMock(return_value=EnhancedJD(
                  enhanced_text="Senior Python engineer with FastAPI.",
                  enhancement_applied=False,
              ))),
        patch("agents.orchestrator.parse_job_description",
              new=AsyncMock(return_value=parsed_jd)),
        patch("agents.orchestrator.AgentVerifier.is_verified",
              new=AsyncMock(return_value=True)),
        patch("agents.orchestrator.PaymentCoordinator.deposit_escrow",
              new=AsyncMock(return_value=None)),
        patch("agents.orchestrator.PaymentCoordinator.record_batch",
              new=AsyncMock(return_value=None)),
        patch("agents.orchestrator.run_apollo_agent",
              new=AsyncMock(return_value=candidates)),
        patch("agents.orchestrator.run_github_source_agent",
              new=AsyncMock(return_value=[])),
        patch("agents.orchestrator.run_github_agent",
              new=AsyncMock(return_value=candidates)),
        patch("agents.orchestrator.run_hunter_agent",
              new=AsyncMock(return_value=candidates)),
        patch("agents.orchestrator.run_scoring_agent",
              new=AsyncMock(return_value=scored)),
        patch("agents.orchestrator.run_talent_intelligence_agent",
              new=AsyncMock(return_value=report)),
    ]


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestOrchestratorRunComplete:
    async def test_status_transitions_to_complete(
        self, db_session, running_search_in_db, search_id
    ):
        from agents.orchestrator import HireFlowOrchestrator

        wallet_mgr = LocalWalletManager()

        with (
            patch("agents.orchestrator.enhance_job_description",
                  new=AsyncMock(return_value=EnhancedJD(
                      enhanced_text="Senior Python engineer.",
                      enhancement_applied=False,
                  ))),
            patch("agents.orchestrator.parse_job_description",
                  new=AsyncMock(return_value=_make_parsed_jd())),
            patch("agents.orchestrator.AgentVerifier.is_verified",
                  new=AsyncMock(return_value=True)),
            patch("agents.orchestrator.PaymentCoordinator.deposit_escrow",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.PaymentCoordinator.record_batch",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.run_apollo_agent",
                  new=AsyncMock(return_value=[_make_candidate_enriched()])),
            patch("agents.orchestrator.run_github_source_agent",
                  new=AsyncMock(return_value=[])),
            patch("agents.orchestrator.run_github_agent",
                  new=AsyncMock(return_value=[_make_candidate_enriched()])),
            patch("agents.orchestrator.run_hunter_agent",
                  new=AsyncMock(return_value=[_make_candidate_enriched()])),
            patch("agents.orchestrator.run_scoring_agent",
                  new=AsyncMock(return_value=[_make_candidate_scored()])),
            patch("agents.orchestrator.run_talent_intelligence_agent",
                  new=AsyncMock(return_value=_make_intelligence_report(search_id))),
        ):
            orchestrator = HireFlowOrchestrator(
                db=db_session,
                wallet_manager=wallet_mgr,
                broadcast_fn=AsyncMock(),
            )
            await orchestrator.run(search_id, "Senior Python engineer.")
            await orchestrator.close()

        result = await db_session.execute(
            select(Search).where(Search.id == uuid.UUID(search_id))
        )
        search = result.scalar_one()
        assert search.status == "complete"
        assert search.completed_at is not None

    async def test_candidates_written_to_db(
        self, db_session, running_search_in_db, search_id
    ):
        from agents.orchestrator import HireFlowOrchestrator

        wallet_mgr = LocalWalletManager()
        two_candidates = [
            _make_candidate_enriched("Alice"),
            _make_candidate_enriched("Bob"),
        ]
        two_scored = [
            _make_candidate_scored("Alice"),
            _make_candidate_scored("Bob"),
        ]
        two_scored[1].rank = 2

        with (
            patch("agents.orchestrator.enhance_job_description",
                  new=AsyncMock(return_value=EnhancedJD(
                      enhanced_text="Test JD", enhancement_applied=False
                  ))),
            patch("agents.orchestrator.parse_job_description",
                  new=AsyncMock(return_value=_make_parsed_jd())),
            patch("agents.orchestrator.AgentVerifier.is_verified",
                  new=AsyncMock(return_value=True)),
            patch("agents.orchestrator.PaymentCoordinator.deposit_escrow",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.PaymentCoordinator.record_batch",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.run_apollo_agent",
                  new=AsyncMock(return_value=two_candidates)),
            patch("agents.orchestrator.run_github_source_agent",
                  new=AsyncMock(return_value=[])),
            patch("agents.orchestrator.run_github_agent",
                  new=AsyncMock(return_value=two_candidates)),
            patch("agents.orchestrator.run_hunter_agent",
                  new=AsyncMock(return_value=two_candidates)),
            patch("agents.orchestrator.run_scoring_agent",
                  new=AsyncMock(return_value=two_scored)),
            patch("agents.orchestrator.run_talent_intelligence_agent",
                  new=AsyncMock(return_value=_make_intelligence_report(search_id))),
        ):
            orchestrator = HireFlowOrchestrator(
                db=db_session,
                wallet_manager=wallet_mgr,
                broadcast_fn=AsyncMock(),
            )
            await orchestrator.run(search_id, "Test JD")
            await orchestrator.close()

        cand_result = await db_session.execute(
            select(CandidateORM).where(CandidateORM.search_id == uuid.UUID(search_id))
        )
        candidates = cand_result.scalars().all()
        assert len(candidates) == 2
        names = {c.name for c in candidates}
        assert "Alice" in names
        assert "Bob" in names


class TestOrchestratorJDParseFailure:
    async def test_jd_parse_error_sets_status_failed(
        self, db_session, running_search_in_db, search_id
    ):
        from agents.orchestrator import HireFlowOrchestrator
        from agents.jd_parser import JDParseError

        wallet_mgr = LocalWalletManager()

        with (
            patch("agents.orchestrator.enhance_job_description",
                  new=AsyncMock(return_value=EnhancedJD(
                      enhanced_text="Bad JD", enhancement_applied=False
                  ))),
            patch("agents.orchestrator.parse_job_description",
                  new=AsyncMock(side_effect=JDParseError("LLM returned garbage"))),
            patch("agents.orchestrator.PaymentCoordinator.record_batch",
                  new=AsyncMock(return_value=None)),
        ):
            orchestrator = HireFlowOrchestrator(
                db=db_session,
                wallet_manager=wallet_mgr,
                broadcast_fn=AsyncMock(),
            )
            await orchestrator.run(search_id, "Bad JD")
            await orchestrator.close()

        result = await db_session.execute(
            select(Search).where(Search.id == uuid.UUID(search_id))
        )
        search = result.scalar_one()
        assert search.status == "failed"


class TestOrchestratorEmptyCandidates:
    async def test_empty_candidates_still_completes(
        self, db_session, running_search_in_db, search_id
    ):
        from agents.orchestrator import HireFlowOrchestrator

        wallet_mgr = LocalWalletManager()

        with (
            patch("agents.orchestrator.enhance_job_description",
                  new=AsyncMock(return_value=EnhancedJD(
                      enhanced_text="Senior Go engineer", enhancement_applied=False
                  ))),
            patch("agents.orchestrator.parse_job_description",
                  new=AsyncMock(return_value=_make_parsed_jd())),
            patch("agents.orchestrator.AgentVerifier.is_verified",
                  new=AsyncMock(return_value=True)),
            patch("agents.orchestrator.PaymentCoordinator.deposit_escrow",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.PaymentCoordinator.record_batch",
                  new=AsyncMock(return_value=None)),
            patch("agents.orchestrator.run_apollo_agent",
                  new=AsyncMock(return_value=[])),
            patch("agents.orchestrator.run_github_source_agent",
                  new=AsyncMock(return_value=[])),
            patch("agents.orchestrator.run_scoring_agent",
                  new=AsyncMock(return_value=[])),
            patch("agents.orchestrator.run_talent_intelligence_agent",
                  new=AsyncMock(return_value=_make_intelligence_report(search_id))),
        ):
            orchestrator = HireFlowOrchestrator(
                db=db_session,
                wallet_manager=wallet_mgr,
                broadcast_fn=AsyncMock(),
            )
            await orchestrator.run(search_id, "Senior Go engineer")
            await orchestrator.close()

        result = await db_session.execute(
            select(Search).where(Search.id == uuid.UUID(search_id))
        )
        search = result.scalar_one()
        assert search.status == "complete"


class TestOrchestratorClose:
    async def test_close_is_idempotent(self, db_session):
        from agents.orchestrator import HireFlowOrchestrator

        wallet_mgr = LocalWalletManager()
        orchestrator = HireFlowOrchestrator(
            db=db_session,
            wallet_manager=wallet_mgr,
            broadcast_fn=AsyncMock(),
        )
        await orchestrator.close()
        await orchestrator.close()  # Should not raise
