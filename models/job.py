from pydantic import BaseModel, Field
from typing import Optional


class JobDescription(BaseModel):
    raw_text: str = Field(..., description="Free-text job description from the recruiter")


class EnhancedJD(BaseModel):
    """Output from JD Enhancement Agent — expanded JD with additional search hints."""
    enhanced_text: str
    additional_titles: list[str] = Field(default_factory=list)
    additional_keywords: list[str] = Field(default_factory=list)
    enhancement_applied: bool = False  # False if JD was already comprehensive


class ParsedJD(BaseModel):
    skills: list[str] = Field(default_factory=list)
    seniority: str = Field(default="senior")  # junior | mid | senior | lead | staff
    location: str = Field(default="Remote")
    years_exp: int = Field(default=5)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    titles: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)  # e.g. ["Python", "TypeScript"]
    keywords: list[str] = Field(default_factory=list)
    raw_jd: str = Field(default="")
