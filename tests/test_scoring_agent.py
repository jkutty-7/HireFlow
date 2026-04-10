"""
Unit tests for agents/scoring_agent.py.

Phase 2 asserts:
  - Only ONE LLM call per candidate (merged skill match + justification)
  - Weighted skill_match_pct: required=1.0pt, optional=0.5pt
  - Optional skill misses are NOT included in skill_gaps

All LLM calls are mocked — no network or API key required.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.scoring_agent import (
    compute_composite_score,
    score_candidate,
    _compute_skill_match_pct,
    SENIORITY_SCORE,
    EMAIL_SCORE,
)
from models.candidate import CandidateEnriched, GitHubProfile
from models.job import ParsedJD


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def parsed_jd() -> ParsedJD:
    return ParsedJD(
        required_skills=["Python", "FastAPI", "PostgreSQL"],
        optional_skills=["Docker", "Terraform"],
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
        risky     = compute_composite_score(100.0, "match", 0.0, "risky")
        unverified = compute_composite_score(100.0, "match", 0.0, "unverified")
        missing    = compute_composite_score(100.0, "match", 0.0, "missing")
        assert missing < risky < unverified

    def test_unknown_seniority_treated_as_zero(self):
        score_unknown = compute_composite_score(50.0, "unknown", 50.0, "verified")
        score_match   = compute_composite_score(50.0, "match",   50.0, "verified")
        assert score_unknown < score_match

    def test_invalid_seniority_falls_back_to_zero(self):
        result = compute_composite_score(50.0, "bogus_value", 50.0, "verified")
        assert result == compute_composite_score(50.0, "unknown", 50.0, "verified")

    def test_result_clamped_to_0_100(self):
        assert compute_composite_score(100.0, "match", 100.0, "verified") <= 100.0
        assert compute_composite_score(0.0, "unknown", 0.0, "missing") >= 0.0

    def test_over_seniority_lower_than_match(self):
        assert compute_composite_score(80.0, "over", 60.0, "verified") < \
               compute_composite_score(80.0, "match", 60.0, "verified")

    def test_under_seniority_lower_than_over(self):
        assert compute_composite_score(80.0, "under", 60.0, "verified") < \
               compute_composite_score(80.0, "over", 60.0, "verified")


# ─── _compute_skill_match_pct ─────────────────────────────────────────────────

class TestComputeSkillMatchPct:
    def test_all_required_matched(self):
        detail = [
            {"skill": "Python",     "matched": True,  "is_required": True},
            {"skill": "FastAPI",    "matched": True,  "is_required": True},
            {"skill": "PostgreSQL", "matched": True,  "is_required": True},
        ]
        pct, gaps = _compute_skill_match_pct(detail, ["Python", "FastAPI", "PostgreSQL"], [])
        assert pct == 100.0
        assert gaps == []

    def test_optional_miss_not_in_gaps(self):
        detail = [
            {"skill": "Python",    "matched": True,  "is_required": True},
            {"skill": "Terraform", "matched": False, "is_required": False},
        ]
        pct, gaps = _compute_skill_match_pct(detail, ["Python"], ["Terraform"])
        assert "Terraform" not in gaps
        assert pct > 0.0

    def test_required_miss_appears_in_gaps(self):
        detail = [
            {"skill": "Python",      "matched": True,  "is_required": True},
            {"skill": "PostgreSQL",  "matched": False, "is_required": True},
        ]
        pct, gaps = _compute_skill_match_pct(detail, ["Python", "PostgreSQL"], [])
        assert "PostgreSQL" in gaps

    def test_optional_miss_reduces_score_less_than_required_miss(self):
        # Two equal-size scenarios: miss one required vs miss one optional
        required_miss = [
            {"skill": "Python",     "matched": True,  "is_required": True},
            {"skill": "Go",         "matched": False, "is_required": True},
        ]
        optional_miss = [
            {"skill": "Python",    "matched": True,  "is_required": True},
            {"skill": "Terraform", "matched": False, "is_required": False},
        ]
        pct_req_miss, _ = _compute_skill_match_pct(required_miss, ["Python", "Go"], [])
        pct_opt_miss, _ = _compute_skill_match_pct(optional_miss, ["Python"], ["Terraform"])
        # Missing a required skill hurts more
        assert pct_req_miss < pct_opt_miss

    def test_no_skills_returns_zero(self):
        pct, gaps = _compute_skill_match_pct([], [], [])
        assert pct == 0.0
        assert gaps == []

    def test_empty_match_detail_returns_zero(self):
        pct, gaps = _compute_skill_match_pct([], ["Python", "FastAPI"], [])
        assert pct == 0.0
        assert gaps == ["Python", "FastAPI"]

    def test_all_optional_matched(self):
        detail = [
            {"skill": "Terraform", "matched": True, "is_required": False},
            {"skill": "Docker",    "matched": True, "is_required": False},
        ]
        pct, gaps = _compute_skill_match_pct(detail, [], ["Terraform", "Docker"])
        assert pct == 100.0
        assert gaps == []


# ─── score_candidate — single LLM call ────────────────────────────────────────

class TestScoreCandidate:
    async def test_only_one_llm_call(self, candidate_with_github, parsed_jd, mock_llm):
        """Phase 2.1 key assertion: exactly one LLM call per candidate."""
        combined_response = json.dumps({
            "skill_matches": [
                {"skill": "Python",     "matched": True,  "matched_via": "Python",  "is_required": True},
                {"skill": "FastAPI",    "matched": True,  "matched_via": "FastAPI", "is_required": True},
                {"skill": "PostgreSQL", "matched": False, "matched_via": None,      "is_required": True},
                {"skill": "Docker",     "matched": True,  "matched_via": "Docker",  "is_required": False},
                {"skill": "Terraform",  "matched": False, "matched_via": None,      "is_required": False},
            ],
            "seniority_fit":      "match",
            "email_validity":     "verified",
            "rank_justification": "Strong Python and FastAPI background. Missing PostgreSQL.",
        })
        mock_llm.ainvoke.return_value = MagicMock(content=combined_response)

        await score_candidate(candidate_with_github, parsed_jd, mock_llm)

        assert mock_llm.ainvoke.call_count == 1

    async def test_optional_miss_not_in_gaps(self, candidate_with_github, parsed_jd, mock_llm):
        combined = json.dumps({
            "skill_matches": [
                {"skill": "Python",    "matched": True,  "matched_via": "Python", "is_required": True},
                {"skill": "FastAPI",   "matched": True,  "matched_via": "FastAPI","is_required": True},
                {"skill": "PostgreSQL","matched": True,  "matched_via": "SQL",    "is_required": True},
                {"skill": "Docker",    "matched": True,  "matched_via": "Docker", "is_required": False},
                {"skill": "Terraform", "matched": False, "matched_via": None,     "is_required": False},
            ],
            "seniority_fit": "match", "email_validity": "verified",
            "rank_justification": "ok",
        })
        mock_llm.ainvoke.return_value = MagicMock(content=combined)

        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)

        assert "Terraform" not in result.skill_gaps

    def _full_match_response(self) -> dict:
        return {
            "skill_matches": [
                {"skill": "Python",     "matched": True,  "matched_via": "Python",  "is_required": True},
                {"skill": "FastAPI",    "matched": True,  "matched_via": "FastAPI", "is_required": True},
                {"skill": "PostgreSQL", "matched": False, "matched_via": None,      "is_required": True},
                {"skill": "Docker",     "matched": True,  "matched_via": "Docker",  "is_required": False},
                {"skill": "Terraform",  "matched": False, "matched_via": None,      "is_required": False},
            ],
            "seniority_fit":      "match",
            "email_validity":     "verified",
            "rank_justification": "Good candidate.",
        }

    async def test_composite_score_computed_deterministically(
        self, candidate_with_github, parsed_jd, mock_llm
    ):
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(self._full_match_response()))
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)

        expected = compute_composite_score(
            result.skill_match_pct,
            "match",
            result.github_score,
            "verified",
        )
        assert result.composite_score == expected

    async def test_invalid_seniority_normalised(self, candidate_with_github, parsed_jd, mock_llm):
        bad = {**self._full_match_response(), "seniority_fit": "BOGUS"}
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(bad))
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.seniority_fit == "unknown"

    async def test_risky_email_accepted(self, candidate_with_github, parsed_jd, mock_llm):
        risky = {**self._full_match_response(), "email_validity": "risky"}
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(risky))
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.email_validity == "risky"

    async def test_no_github_zero_github_score(self, candidate_no_github, mock_llm):
        jd = ParsedJD(required_skills=["Python"], optional_skills=[], raw_jd="")
        resp = {
            "skill_matches":      [{"skill": "Python", "matched": True, "matched_via": "Python", "is_required": True}],
            "seniority_fit":      "under",
            "email_validity":     "missing",
            "rank_justification": "Junior candidate.",
        }
        mock_llm.ainvoke.return_value = MagicMock(content=json.dumps(resp))
        result = await score_candidate(candidate_no_github, jd, mock_llm)
        assert result.github_score == 0.0
        assert mock_llm.ainvoke.call_count == 1

    async def test_llm_failure_returns_partial_score(
        self, candidate_with_github, parsed_jd, mock_llm
    ):
        mock_llm.ainvoke.side_effect = RuntimeError("Rate limit")
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.composite_score >= 0
        assert "unavailable" in result.rank_justification

    async def test_invalid_json_response_handled_gracefully(
        self, candidate_with_github, parsed_jd, mock_llm
    ):
        mock_llm.ainvoke.return_value = MagicMock(content="NOT JSON")
        result = await score_candidate(candidate_with_github, parsed_jd, mock_llm)
        assert result.composite_score >= 0
        assert mock_llm.ainvoke.call_count == 1
