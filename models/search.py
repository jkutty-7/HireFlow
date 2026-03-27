from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid

from models.candidate import CandidateScored
from models.payment import TransactionLog


class SearchRequest(BaseModel):
    job_description: str = Field(..., min_length=20, description="Free-text job description")
    recruiter_wallet_id: Optional[str] = Field(
        None, description="Circle Wallet ID of the recruiter funding the search"
    )
    max_candidates: int = Field(default=25, ge=5, le=50)
    location_filter: Optional[str] = Field(
        None,
        description=(
            "Restrict candidates to a specific city/region. "
            "Overrides any location parsed from the JD. "
            "Examples: 'Bangalore', 'Kerala', 'Mumbai', 'Remote'."
        ),
    )


class SearchStatus(BaseModel):
    search_id: uuid.UUID
    status: str  # pending | running | complete | failed
    stage: Optional[str] = None  # parse_jd | verify_agents | deposit | collect | score | finalize
    progress_pct: float = 0.0
    total_spent_usdc: float = 0.0
    transaction_count: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None


class SearchResult(BaseModel):
    search_id: uuid.UUID
    status: str
    candidates: list[CandidateScored] = Field(default_factory=list)
    total_spent_usdc: float = 0.0
    transaction_count: int = 0
    payment_log: list[TransactionLog] = Field(default_factory=list)
    escrow_tx_hash: Optional[str] = None
    refund_tx_hash: Optional[str] = None
    completed_at: Optional[datetime] = None
