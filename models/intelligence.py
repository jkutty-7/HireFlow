"""
Talent Intelligence Report models.

Produced by TalentIntelligenceAgent after all candidates are scored.
Stored as JSON on the Search record and exposed via GET /api/search/{id}/intelligence.
"""

from datetime import datetime, timezone
from pydantic import BaseModel, Field


class CandidateInterviewPlan(BaseModel):
    candidate_name: str
    rank: int
    composite_score: float
    interview_questions: list[str] = Field(default_factory=list)  # exactly 3
    skill_gap_focus: list[str] = Field(default_factory=list)


class TalentIntelligenceReport(BaseModel):
    search_id: str
    top_3_summary: str = ""
    search_quality_score: int = 0          # 0–100: how good was this candidate pool?
    search_quality_notes: str = ""         # e.g. "Only 5 candidates — thin pool"
    red_flags: list[str] = Field(default_factory=list)
    recommended_jd_changes: list[str] = Field(default_factory=list)
    interview_plans: list[CandidateInterviewPlan] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
