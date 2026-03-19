from pydantic import BaseModel, Field
from typing import Optional
import uuid


class GitHubRepo(BaseModel):
    name: str
    description: Optional[str] = None
    language: Optional[str] = None
    stars: int = 0
    forks: int = 0
    pushed_at: Optional[str] = None
    topics: list[str] = Field(default_factory=list)


class GitHubProfile(BaseModel):
    username: str
    name: Optional[str] = None
    bio: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    public_repos: int = 0
    followers: int = 0
    top_repos: list[GitHubRepo] = Field(default_factory=list)
    top_languages: dict[str, int] = Field(default_factory=dict)  # lang → bytes
    recent_event_count: int = 0  # events in last 30 days
    github_score: float = 0.0


class CandidateRaw(BaseModel):
    """Output from Apollo search (no email/enrichment yet)."""
    apollo_id: Optional[str] = None
    name: str
    title: Optional[str] = None
    company: Optional[str] = None
    linkedin_url: Optional[str] = None
    location: Optional[str] = None
    github_url: Optional[str] = None


class CandidateEnriched(CandidateRaw):
    """After Apollo enrichment + GitHub + Hunter."""
    email: Optional[str] = None
    email_confidence: Optional[int] = None
    email_status: Optional[str] = None  # valid | risky | invalid | unknown
    skills: list[str] = Field(default_factory=list)
    employment_history: list[dict] = Field(default_factory=list)
    github_username: Optional[str] = None
    github_data: Optional[GitHubProfile] = None


class CandidateScored(CandidateEnriched):
    """After AI scoring agent (Claude Sonnet 4.5)."""
    skill_match_pct: float = 0.0
    seniority_fit: str = "unknown"   # under | match | over | unknown
    github_score: float = 0.0
    email_validity: str = "missing"  # verified | unverified | missing
    composite_score: float = 0.0
    rank_justification: str = ""
    rank: Optional[int] = None
    candidate_id: Optional[uuid.UUID] = None
