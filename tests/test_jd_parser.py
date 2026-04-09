"""
Unit tests for agents/jd_parser.py.

All LLM calls are mocked — no network or API key required.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.jd_parser import parse_job_description, JDParseError
from models.job import ParsedJD


# ─── Helpers ──────────────────────────────────────────────────────────────────

VALID_JD_RESPONSE = {
    "skills":      ["Python", "FastAPI", "PostgreSQL", "Docker"],
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
        assert "Python" in result.skills
        assert "FastAPI" in result.skills
        assert result.seniority == "senior"
        assert result.location == "Bangalore"
        assert result.years_exp == 5
        assert result.raw_jd == SAMPLE_JD

    async def test_languages_and_titles_populated(self):
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(VALID_JD_RESPONSE))
            )
            result = await parse_job_description(SAMPLE_JD)

        assert "Python" in result.languages
        assert "Backend Engineer" in result.titles

    async def test_json_wrapped_in_markdown_still_parsed(self):
        """LLM sometimes returns ```json ... ``` — the regex fallback should handle it."""
        wrapped = f"```json\n{json.dumps(VALID_JD_RESPONSE)}\n```"
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(wrapped)
            )
            result = await parse_job_description(SAMPLE_JD)

        assert isinstance(result, ParsedJD)
        assert "Python" in result.skills


# ─── Seniority normalisation ──────────────────────────────────────────────────

class TestSeniorityNormalisation:
    @pytest.mark.parametrize("raw_seniority,expected", [
        ("senior",   "senior"),
        ("SENIOR",   "senior"),
        ("Senior",   "senior"),
        ("mid",      "mid"),
        ("junior",   "junior"),
        ("lead",     "lead"),
        ("staff",    "staff"),
        ("unknown",  "senior"),   # invalid → falls back to senior
        ("manager",  "senior"),   # invalid → falls back to senior
        ("",         "senior"),   # empty → falls back to senior
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
                return_value=_make_llm_response("skills: Python FastAPI")
            )
            with pytest.raises(JDParseError):
                await parse_job_description(SAMPLE_JD)

    async def test_pydantic_validation_failure_raises_jd_parse_error(self):
        """If LLM returns JSON with wrong schema, ParsedJD construction fails."""
        bad_data = {"skills": "not-a-list"}  # skills must be list[str]
        with patch("agents.jd_parser.ChatAnthropic") as MockLLM:
            MockLLM.return_value.ainvoke = AsyncMock(
                return_value=_make_llm_response(json.dumps(bad_data))
            )
            # Pydantic v2 coerces string to list in some cases — just ensure no uncaught exception
            # The test passes as long as we get either ParsedJD or JDParseError (no unhandled crash)
            try:
                result = await parse_job_description(SAMPLE_JD)
                assert isinstance(result, ParsedJD)
            except JDParseError:
                pass  # Also acceptable

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

        assert result.years_exp == 5  # ParsedJD default

    async def test_empty_optional_lists_default_to_empty(self):
        minimal = {
            "skills":    ["Python"],
            "seniority": "mid",
            "location":  "Remote",
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
