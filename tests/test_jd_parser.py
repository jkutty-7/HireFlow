"""
Unit tests for agents/jd_parser.py.

Phase 2 additions:
  - LLM now returns required_skills + optional_skills (not a flat skills list)
  - Backward-compat validator maps old "skills" key → required_skills
  - optional_skills is an empty list when no nice-to-have skills are mentioned

All LLM calls are mocked — no network or API key required.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.jd_parser import parse_job_description, JDParseError
from models.job import ParsedJD


# ─── Helpers ──────────────────────────────────────────────────────────────────

VALID_JD_RESPONSE = {
    "required_skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
    "optional_skills": ["Terraform", "Kubernetes"],
    "seniority":   "senior",
    "location":    "Bangalore",
    "years_exp":   5,
    "salary_min":  None,
    "salary_max":  None,
    "titles":      ["Backend Engineer", "Senior Python Developer"],
    "languages":   ["Python"],
    "keywords":    ["microservices", "REST API"],
}

SAMPLE_JD = "We are looking for a Senior Python Engineer with FastAPI and PostgreSQL skills."


def _make_llm_response(content: str):
    return MagicMock(content=content)


# ─── Happy path ───────────────────────────────────────────────────────────────

class TestParseJobDescriptionHappyPath:
    async def test_returns_parsed_jd(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(VALID_JD_RESPONSE))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert isinstance(result, ParsedJD)
        assert "Python" in result.required_skills
        assert "FastAPI" in result.required_skills
        assert result.seniority == "senior"
        assert result.location == "Bangalore"
        assert result.years_exp == 5
        assert result.raw_jd == SAMPLE_JD

    async def test_optional_skills_populated(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(VALID_JD_RESPONSE))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert "Terraform" in result.optional_skills
        assert "Kubernetes" in result.optional_skills

    async def test_combined_skills_property(self):
        """ParsedJD.skills returns required + optional combined."""
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(VALID_JD_RESPONSE))
            )
            result = await parse_job_description(SAMPLE_JD)

        all_skills = result.skills
        for s in result.required_skills + result.optional_skills:
            assert s in all_skills

    async def test_json_wrapped_in_markdown_still_parsed(self):
        wrapped = f"```json\n{json.dumps(VALID_JD_RESPONSE)}\n```"
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(wrapped)
            )
            result = await parse_job_description(SAMPLE_JD)

        assert isinstance(result, ParsedJD)
        assert "Python" in result.required_skills


# ─── Backward compatibility ───────────────────────────────────────────────────

class TestBackwardCompatibility:
    def test_legacy_skills_key_maps_to_required(self):
        """Old records used a flat 'skills' list — model_validator migrates them."""
        legacy_data = {
            "skills": ["Python", "Go"],
            "seniority": "mid",
            "location": "Remote",
            "years_exp": 3,
        }
        parsed = ParsedJD(**legacy_data)
        assert parsed.required_skills == ["Python", "Go"]
        assert parsed.optional_skills == []

    def test_combined_skills_property_includes_all(self):
        jd = ParsedJD(
            required_skills=["Python"],
            optional_skills=["Docker"],
            raw_jd="",
        )
        assert set(jd.skills) == {"Python", "Docker"}

    def test_new_fields_take_precedence_over_legacy_key(self):
        """When required_skills is given, legacy 'skills' key is ignored."""
        data = {
            "required_skills": ["Python"],
            "optional_skills": ["Docker"],
            "skills": ["Go"],  # should be ignored
        }
        parsed = ParsedJD(**data)
        assert "Go" not in parsed.required_skills


# ─── Seniority normalisation ──────────────────────────────────────────────────

class TestSeniorityNormalisation:
    @pytest.mark.parametrize("raw_seniority,expected", [
        ("senior",  "senior"),
        ("SENIOR",  "senior"),
        ("Senior",  "senior"),
        ("mid",     "mid"),
        ("junior",  "junior"),
        ("lead",    "lead"),
        ("staff",   "staff"),
        ("unknown", "senior"),
        ("manager", "senior"),
        ("",        "senior"),
    ])
    async def test_seniority_normalised(self, raw_seniority, expected):
        response_data = {**VALID_JD_RESPONSE, "seniority": raw_seniority}
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(response_data))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert result.seniority == expected


# ─── Error propagation ────────────────────────────────────────────────────────

class TestJDParseError:
    async def test_llm_exception_raises_jd_parse_error(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                side_effect=RuntimeError("API unavailable")
            )
            with pytest.raises(JDParseError, match="JD parser agent error"):
                await parse_job_description(SAMPLE_JD)

    async def test_completely_unparseable_response_raises(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response("Sorry, I cannot help with that.")
            )
            with pytest.raises(JDParseError, match="Could not extract valid JSON"):
                await parse_job_description(SAMPLE_JD)

    async def test_invalid_json_with_no_braces_raises(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response("required_skills: Python FastAPI")
            )
            with pytest.raises(JDParseError):
                await parse_job_description(SAMPLE_JD)

    async def test_jd_parse_error_is_exception_subclass(self):
        assert issubclass(JDParseError, Exception)


# ─── Defaults ─────────────────────────────────────────────────────────────────

class TestDefaults:
    async def test_missing_years_exp_defaults_to_5(self):
        response_data = {k: v for k, v in VALID_JD_RESPONSE.items() if k != "years_exp"}
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(response_data))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert result.years_exp == 5

    async def test_empty_optional_lists_default_to_empty(self):
        minimal = {
            "required_skills": ["Python"],
            "optional_skills": [],
            "seniority": "mid",
            "location": "Remote",
            "years_exp": 3,
        }
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(minimal))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert result.languages == []
        assert result.titles == []
        assert result.keywords == []
        assert result.optional_skills == []

    async def test_no_optional_skills_key_defaults_to_empty(self):
        """LLM omits optional_skills → should default to empty list gracefully."""
        no_optional = {k: v for k, v in VALID_JD_RESPONSE.items() if k != "optional_skills"}
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(no_optional))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert result.optional_skills == []
        assert "Python" in result.required_skills
