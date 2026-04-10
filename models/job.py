from pydantic import BaseModel, Field, model_validator
from typing import Optional, Any


class JobDescription(BaseModel):
    raw_text: str = Field(..., description="Free-text job description from the recruiter")


class EnhancedJD(BaseModel):
    """Output from JD Enhancement Agent — expanded JD with additional search hints."""
    enhanced_text: str
    additional_titles: list[str] = Field(default_factory=list)
    additional_keywords: list[str] = Field(default_factory=list)
    enhancement_applied: bool = False  # False if JD was already comprehensive


class ParsedJD(BaseModel):
    # Phase 2.2: required vs optional skills
    # required_skills: must-have for the role
    # optional_skills: nice-to-have (weighted at 0.5 in scoring formula)
    required_skills: list[str] = Field(default_factory=list)
    optional_skills: list[str] = Field(default_factory=list)

    seniority: str = Field(default="senior")  # junior | mid | senior | lead | staff
    location: str = Field(default="Remote")
    years_exp: int = Field(default=5)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    titles: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)  # e.g. ["Python", "TypeScript"]
    keywords: list[str] = Field(default_factory=list)
    raw_jd: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_skills(cls, data: Any) -> Any:
        """
        Backward-compat: old records stored a flat `skills` list.
        Map skills → required_skills when the new fields are absent.
        """
        if isinstance(data, dict):
            if "skills" in data and "required_skills" not in data:
                data = dict(data)
                data["required_skills"] = data.pop("skills", [])
        return data

    @property
    def skills(self) -> list[str]:
        """Combined list for any code that still reads ParsedJD.skills."""
        return self.required_skills + self.optional_skills
