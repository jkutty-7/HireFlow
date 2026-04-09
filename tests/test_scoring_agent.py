"""
Unit tests for agents/scoring_agent.py.

All LLM calls are mocked — no network or API key required.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.scoring_agent import (
    compute_composite_score,
    score_candidate,
    match_skills_structured,
    SENIORITY_SCORE,
    EMAIL_SCORE,
)
from models.candidate import CandidateEnriched, GitHubProfile
from models.job import ParsedJD


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def parsed_jd() -> ParsedJD:
    return ParsedJD(
        skills=["Python", "FastAPI", "PostgreSQL"],
        seniority="senior",
        location="Remote",
        years_exp=5,
        languages=["Python"],
        titles=["Backend Engineer"],
        keywords=[],
        raw_jd="Looking for a senior Python engineer",
    )


@pytest.fixture
def candidate_with_github() -> CandidateEnriched:
    gh = GitHubProfile(
        username="devjane",
        public_repos=30,
        followers=120,
        top_languages={"Python": 500_000, "JavaScript": 100_000},
        recent_event_count=15,
        github_score=72.0,
    )
    return CandidateEnriched(
        name="Jane Dev",
        title="Senior Backend Engineer",
        company="TechCorp",
        location="Remote",
        skills=["Python", "FastAPI", "Docker"],
        email="jane@techcorp.com",
        email_status="valid",
        email_confidence=92,
        github_username="devjane",
        github_data=gh,
        avg_tenure_months=24.0,
        is_job_hopper=False,
        career_trajectory="ascending",
    )


@pytest.fixture
def candidate_no_github() -> CandidateEnriched:
    return CandidateEnriched(
        name="John Nogh",
        title="Software Engineer",
        company="Startup",
        location="Bangalore",
        skills=["Python"],
        email=None,
        github_username=None,
        github_data=None,
    )


# ─── compute_composite_score ──────────────────────────────────────────────────

class TestComputeCompositeScore:
    def test_perfect_score(self):
        result = compute_composite_score(
            skill_match_pct=100.0,
            seniority_fit="match",
            github_score=100.0,
            email_validity="verified",
        )
        assert result == 100.0

    def test_zero_score(self):
        result = compute_composite_score(
            skill_match_pct=0.0,
            seniority_fit="unknown",
            github_score=0.0,
            email_validity="missing",
        )
        assert result == 0.0

    def test_weights_applied_correctly(self):
        # skill=80% × 0.40 + github=60 × 0.30 + seniority=match(20→100) × 0.20 + email=verified(10→100) × 0.10
        # = 32 + 18 + 20 + 10 = 80
        result = compute_composite_score(
            skill_match_pct=80.0,
            seniority_fit="match",
            github_score=60.0,
            email_validity="verified",
        )
        assert result == 80.0

    def test_risky_email_between_unverified_and_missing(self):
        risky = compute_composite_score(100.0, "match", 0.0, "risky")
        unverified = compute_composite_score(100.0, "match", 0.0, "unverified")
        missing = compute_composite_score(100.0, "match", 0.0, "missing")
        assert missing < risky < unverified

    def test_unknown_seniority_treated_as_zero(self):
        score_unknown = compute_composite_score(50.0, "unknown", 50.0, "verified")
        score_match   = compute_composite_score(50.0, "match",   50.0, "verified")
        assert score_unknown < score_match

    def test_invalid_seniority_falls_back_to_zero(self):
        result = compute_composite_score(50.0, "bogus_value", 50.0, "verified")
        assert result == compute_composite_score(50.0, "unknown", 50.0, "verified")

    def test_result_clamped_to_0_100(self):
        # Should never exceed 100 or go below 0
        assert compute_composite_score(100.0, "match", 100.0, "verified") <= 100.0
        assert compute_composite_score(0.0, "unknown", 0.0, "missing") >= 0.0

    def test_over_seniority_lower_than_match(self):
        match_score = compute_composite_score(80.0, "match", 60.0, "verified")
        over_score  = compute_composite_score(80.0, "over",  60.0, "verified")
        assert over_score < match_score

    def test_under_seniority_lower_than_over(self):
        over_score  = compute_composite_score(80.0, "over",  60.0, "verified")
        under_score = compute_composite_score(80.0, "under", 60.0, "verified")
        assert under_score < over_score


# ─── match_skills_structured ──────────────────────────────────────────────────

class TestMatchSkillsStructured:
    async def test_happy_path(self, candidate_with_github, parsed_jd, mock_llm):
        payload = [
            {"skill": "Python",     "matched": True,  "matched_via": "Python"},
            {"skill": "FastAPI",    "matched": True,  "matched_via": "FastAPI"},
            {"skill": "PostgreSQL", "matched": False, "matched_via": None},
        ]
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(payload))

        detail, pct, gaps = await match_skills_structured(
            candidate_with_github, parsed_jd, mock_llm
        )

        assert len(detail) == 3
        assert pct == pytest.approx(66.7, abs=0.2)
        assert "PostgreSQL" in gaps
        assert "Python" not in gaps

    async def test_empty_skills_returns_zero(self, candidate_with_github, mock_llm):
        jd = ParsedJD(skills=[], raw_jd="")
        detail, pct, gaps = await match_skills_structured(candidate_with_github, jd, mock_llm)
        assert pct == 0.0
        assert detail == []
        mock_llm.ainvoke.assert_not_called()

    async def test_llm_returns_invalid_json(self, candidate_with_github, parsed_jd, mock_llm):
        mock_llm.ainvoke.return_value = MagicMock(content="NOT JSON AT ALL")
        detail, pct, gaps = await match_skills_structured(
            candidate_with_github, parsed_jd, mock_llm
        )
        # Falls back gracefully — all skills become gaps
        assert pct == 0.0
        assert set(gaps) == set(parsed_jd.skills)

    async def test_llm_exception_falls_back(self, candidate_with_github, parsed_jd, mock_llm):
        mock_llm.ainvoke.side_effect = RuntimeError("API timeout")
        detail, pct, gaps = await match_skills_structured(
            candidate_with_github, parsed_jd, mock_llm
        )
        assert pct == 0.0
        assert set(gaps) == set(parsed_jd.skills)


# ─── score_candidate ──────────────────────────────────────────────────────────

class TestScoreCandidate:
    async def test_full_scoring_pipeline(self, candidate_with_github, parsed_jd, mock_llm):
        """Two LLM calls: skill match + seniority/justification."""
        skill_response = json.dumps([
            {"skill": "Python",     "matched": True,  "matched_via": "Python"},
            {"skill": "FastAPI",    "matched": True,  "matched_via": "FastAPI"},
            {"skill": "PostgreSQL", "matched": False, "matched_via": None},
        ])
        score_response = json.dumps({
            "seniority_fit":      "match",
            "email_validity":     "verified",
            "rank_justification": "Strong Python background. Missing PostgreSQL.",
        })

        mock_llm.ainvoke.side_effect = [
            MagicMock(content=skill_response),
            MagicMock(content=score_response),
        ]

        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)

        assert result.seniority_fit == "match"
        assert result.email_validity == "verified"
        assert result.skill_match_pct == pytest.approx(66.7, abs=0.2)
        assert "PostgreSQL" in result.skill_gaps
        assert result.composite_score > 0
        assert result.rank_justification != ""
        assert mock_llm.ainvoke.call_count == 2

    async def test_invalid_seniority_normalised(self, candidate_with_github, parsed_jd, mock_llm):
        mock_llm.ainvoke.side_effect = [
            MagicMock(content="[]"),  # skill match
            MagicMock(content=json.dumps({
                "seniority_fit":      "BOGUS_VALUE",
                "email_validity":     "verified",
                "rank_justification": "ok",
            })),
        ]
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.seniority_fit == "unknown"

    async def test_risky_email_validity_accepted(self, candidate_with_github, parsed_jd, mock_llm):
        mock_llm.ainvoke.side_effect = [
            MagicMock(content="[]"),
            MagicMock(content=json.dumps({
                "seniority_fit":      "match",
                "email_validity":     "risky",
                "rank_justification": "ok",
            })),
        ]
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.email_validity == "risky"
        assert result.composite_score == compute_composite_score(
            result.skill_match_pct,
            "match",
            result.github_score,
            "risky",
        )

    async def test_no_github_data(self, candidate_no_github, parsed_jd, mock_llm):
        mock_llm.ainvoke.side_effect = [
            MagicMock(content="[]"),
            MagicMock(content=json.dumps({
                "seniority_fit":      "under",
                "email_validity":     "missing",
                "rank_justification": "Junior candidate without GitHub.",
            })),
        ]
        result = await score_candidate(candidate_no_github, parsed_jd, mock_llm)
        assert result.github_score == 0.0
        assert result.email_validity == "missing"

    async def test_second_llm_call_failure_returns_partial(
        self, candidate_with_github, parsed_jd, mock_llm
    ):
        mock_llm.ainvoke.side_effect = [
            MagicMock(content="[]"),            # skill match succeeds
            RuntimeError("Rate limit hit"),     # justification fails
        ]
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        # Should not raise — returns a partial CandidateScored
        assert result.composite_score >= 0
        assert "unavailable" in result.rank_justification
