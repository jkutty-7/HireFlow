"""
JD Enhancement Agent — Claude Sonnet 4.6 (Anthropic).

Runs BEFORE jd_parser. Expands abbreviated or vague job descriptions so the
parser and Apollo search produce better results.

What it does:
  - Adds implied skills/technologies that are standard for the role
  - Suggests additional Apollo search titles beyond what was stated
  - Adds search keywords that are commonly used for this type of role
  - Skips enhancement if the JD is already comprehensive (>=200 words)

Payment: $0.002 USDC per enhancement via Circle Nanopayments.
"""

import json
import re
import structlog

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from settings import settings
from models.job import EnhancedJD

log = structlog.get_logger()

JD_ENHANCEMENT_SYSTEM_PROMPT = """You are a senior technical recruiter who improves job descriptions for better candidate sourcing.

Given a job description, your job is to:
1. Expand it with implied skills/technologies that are standard for this type of role
   (e.g. a "Senior Backend Engineer" role implies Docker, CI/CD, SQL even if not stated)
2. Add additional job titles that Apollo.io should search
   (e.g. "Backend Engineer" also matches "Software Engineer III", "Staff Engineer", "Platform Engineer")
3. Add important search keywords not already in the JD

If the JD is already comprehensive (more than 200 words with specific skills listed),
set enhancement_applied to false and return the original text unchanged.

Respond with ONLY a valid JSON object — no markdown, no explanation:
{
  "enhanced_text": "<full expanded job description, or original if comprehensive>",
  "additional_titles": ["title1", "title2"],
  "additional_keywords": ["keyword1", "keyword2"],
  "enhancement_applied": true
}"""


async def enhance_job_description(raw_jd: str) -> EnhancedJD:
    """
    Expand a potentially brief JD for better Apollo search coverage.
    Falls back to original text if the agent fails.
    """
    # Fast path: skip enhancement for already-comprehensive JDs
    if len(raw_jd.split()) >= 200:
        log.debug("jd_enhancement_skipped", word_count=len(raw_jd.split()))
        return EnhancedJD(
            enhanced_text=raw_jd,
            additional_titles=[],
            additional_keywords=[],
            enhancement_applied=False,
        )

    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
        temperature=0.1,
        max_tokens=2048,
    )

    try:
        response = await llm.ainvoke([
            SystemMessage(content=JD_ENHANCEMENT_SYSTEM_PROMPT),
            HumanMessage(content=raw_jd),
        ])
        content = response.content if hasattr(response, "content") else str(response)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group()) if match else None

        if not data or not isinstance(data, dict):
            log.warning("jd_enhancement_parse_failed", raw_response=content[:200])
            return EnhancedJD(
                enhanced_text=raw_jd,
                additional_titles=[],
                additional_keywords=[],
                enhancement_applied=False,
            )

        enhanced = EnhancedJD(
            enhanced_text=data.get("enhanced_text", raw_jd),
            additional_titles=[str(t) for t in data.get("additional_titles", [])],
            additional_keywords=[str(k) for k in data.get("additional_keywords", [])],
            enhancement_applied=bool(data.get("enhancement_applied", False)),
        )

        if enhanced.enhancement_applied:
            log.info(
                "jd_enhanced",
                added_titles=enhanced.additional_titles,
                added_keywords=enhanced.additional_keywords,
                original_words=len(raw_jd.split()),
                enhanced_words=len(enhanced.enhanced_text.split()),
            )
        else:
            log.debug("jd_enhancement_not_needed")

        return enhanced

    except Exception as exc:
        log.warning("jd_enhancement_failed", error=str(exc))
        return EnhancedJD(
            enhanced_text=raw_jd,
            additional_titles=[],
            additional_keywords=[],
            enhancement_applied=False,
        )
