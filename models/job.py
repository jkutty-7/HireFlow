from pydantic import BaseModel, Field
from typing import Optional


class JobDescription(BaseModel):
    raw_text: str = Field(..., description="Free-text job description from the recruiter")


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
