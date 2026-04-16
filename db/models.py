import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_description: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_jd: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default="pending"
    )  # pending | running | complete | failed
    total_spent_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)
    escrow_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    refund_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    recruiter_wallet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    intelligence_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Phase 4.2: surfaced from intelligence_report for fast status polling
    search_quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_jd_changes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )
    payment_logs: Mapped[list["PaymentLog"]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("searches.id"), nullable=False
    )

    # Raw data from Apollo
    apollo_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    company: Mapped[str | None] = mapped_column(String(256), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    github_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    email_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # GitHub enrichment
    github_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    github_repos: Mapped[list | None] = mapped_column(JSON, nullable=True)
    github_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Discovery source
    source: Mapped[str] = mapped_column(String(32), default="apollo")  # apollo | github_repo
    source_repos: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # AI Scoring (Claude)
    skill_match_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    seniority_fit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_match_detail: Mapped[list | None] = mapped_column(JSON, nullable=True)
    skill_gaps: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Enrichment fields (previously computed on-the-fly but never persisted)
    skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    employment_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    avg_tenure_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_job_hopper: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    career_trajectory: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_validity: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Recruiter workflow — Phase 3.1
    # new | contacted | interviewing | rejected | hired
    recruiter_status: Mapped[str] = mapped_column(String(32), default="new")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    search: Mapped["Search"] = relationship(back_populates="candidates")


class PaymentLog(Base):
    __tablename__ = "payment_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("searches.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    paying_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    receiving_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    arc_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    x402_payment_proof: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | confirmed | failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    search: Mapped["Search"] = relationship(back_populates="payment_logs")


class SearchTemplate(Base):
    """Saved JD templates so recruiters can re-run common searches quickly."""

    __tablename__ = "search_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_jd: Mapped[str] = mapped_column(Text, nullable=False)
    location_filter: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_candidates: Mapped[int] = mapped_column(Integer, default=25)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentWallet(Base):
    __tablename__ = "agent_wallets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    circle_wallet_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    wallet_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    arc_registry_tx: Mapped[str | None] = mapped_column(String(66), nullable=True)
    is_registered_on_chain: Mapped[bool] = mapped_column(Boolean, default=False)
    total_earned_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
