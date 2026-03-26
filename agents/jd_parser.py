"""
JD Parser Agent — Kimi K2.5 via ChatNVIDIA.

Input:  Free-text job description string
Output: ParsedJD (structured JSON with skills, seniority, location, etc.)

Payment: $0.002 USDC per parse via Circle Nanopayments.
"""

import asyncio
import json
import re
import structlog

from langchain_core.tools import tool
from agents.base import create_kimi_agent
from models.job import ParsedJD

log = structlog.get_logger()

VALID_SENIORITIES = {"junior", "mid", "senior", "lead", "staff"}


class JDParseError(Exception):
    """Raised when the LLM returns a response that cannot be parsed into a ParsedJD."""
    pass

JD_PARSER_SYSTEM_PROMPT = """
You are a technical recruiter assistant that extracts structured hiring criteria
from free-text job descriptions.

When given a job description, respond with ONLY a valid JSON object. Do not include
any explanation or markdown code blocks — raw JSON only.

The JSON must match this schema exactly:
{
  "skills": ["list", "of", "required", "technical", "skills"],
  "seniority": "junior | mid | senior | lead | staff",
  "location": "city, state or Remote",
  "years_exp": <integer>,
  "salary_min": <integer or null>,
  "salary_max": <integer or null>,
  "titles": ["list", "of", "acceptable", "job", "titles"],
  "languages": ["list", "of", "programming", "languages"],
  "keywords": ["other", "important", "search", "keywords"]
}

Rules:
- skills: include frameworks, databases, tools (e.g. FastAPI, PostgreSQL, Docker)
- languages: programming languages only (Python, TypeScript, Go, etc.)
- titles: what Apollo.io search should use as person_titles
- seniority must be exactly one of: junior, mid, senior, lead, staff
- years_exp: integer, default to 5 if not specified
"""


async def parse_job_description(raw_jd: str) -> ParsedJD:
    """
    Parse a free-text JD into structured ParsedJD using Kimi K2.5.
    Calls the LangGraph ReAct agent with no tools (pure LLM reasoning).
    """
    agent = create_kimi_agent(tools=[], system_prompt=JD_PARSER_SYSTEM_PROMPT)

    try:
        result = await asyncio.wait_for(
            agent.ainvoke({"messages": [{"role": "user", "content": raw_jd}]}),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        log.error("jd_parse_timeout")
        raise JDParseError("JD parser timed out after 45 seconds — NVIDIA NIM unavailable")
    except Exception as exc:
        log.error("jd_parse_agent_error", error=str(exc)[:200])
        raise JDParseError(f"JD parser agent error: {exc}") from exc

    # Extract the last message content
    last_message = result["messages"][-1]
    content = last_message.content if hasattr(last_message, "content") else str(last_message)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Attempt to recover JSON embedded in prose or markdown fences
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                data = None
        else:
            data = None

    if data is None:
        log.error("jd_parse_failed_unrecoverable", raw_response=content[:300])
        raise JDParseError(f"Could not extract valid JSON from JD parser response: {content[:300]}")

    try:
        # Normalise seniority to a valid value
        seniority = str(data.get("seniority", "senior")).lower()
        if seniority not in VALID_SENIORITIES:
            seniority = "senior"
        data["seniority"] = seniority

        parsed = ParsedJD(**data, raw_jd=raw_jd)
        log.info("jd_parsed", skills=parsed.skills, seniority=parsed.seniority)
        return parsed
    except Exception as exc:
        log.error("jd_parse_model_failed", error=str(exc), data=str(data)[:200])
        raise JDParseError(f"ParsedJD construction failed: {exc}") from exc
