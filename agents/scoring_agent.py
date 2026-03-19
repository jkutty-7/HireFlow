"""
AI Scoring Agent — Claude Sonnet 4.5 (Anthropic).

The ONLY agent that uses Claude. Chosen for deep multi-factor reasoning
and the ability to write coherent 2-sentence justifications.

Input:  List of CandidateEnriched + ParsedJD
Output: List of CandidateScored (sorted by composite_score desc)

Payment: $0.003 USDC per candidate scored via Circle Nanopayments.
"""

import json
import structlog
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from settings import settings
from models.candidate import CandidateEnriched, CandidateScored
from models.job import ParsedJD

log = structlog.get_logger()

SCORING_SYSTEM_PROMPT = """
You are a senior technical recruiter scoring software engineering candidates.

For each candidate, produce a JSON score object with exactly these fields:
{
  "skill_match_pct": <0-100 integer, percentage of JD required skills found>,
  "seniority_fit": "<under | match | over>",
  "github_score": <0-100 float from pre-computed score, or 0 if no GitHub data>,
  "email_validity": "<verified | unverified | missing>",
  "composite_score": <0-100 float, weighted overall score>,
  "rank_justification": "<2-sentence explanation: why this candidate fits or doesn't>"
}

Composite score weights:
  - skill_match_pct:  40%
  - github_score:     30%
  - seniority_fit:    20%  (match=20, over=10, under=5)
  - email_validity:   10%  (verified=10, unverified=5, missing=0)

Be precise. Be honest. Flag red flags explicitly in rank_justification.
Respond with ONLY the JSON object — no markdown, no explanation.
"""


def _build_candidate_prompt(candidate: CandidateEnriched, parsed_jd: ParsedJD) -> str:
    """Build the user prompt for scoring one candidate."""
    github_summary = "No GitHub data available."
    if candidate.github_data:
        gh = candidate.github_data
        top_langs = sorted(gh.top_languages.items(), key=lambda x: x[1], reverse=True)[:5]
        lang_str = ", ".join(f"{l}({b // 1000}kb)" for l, b in top_langs)
        github_summary = (
            f"GitHub @{gh.username}: {gh.public_repos} repos, "
            f"{gh.followers} followers, pre-computed score={gh.github_score:.1f}/100. "
            f"Top languages: {lang_str}. "
            f"Recent activity (last 30 days): {gh.recent_event_count} events."
        )

    email_validity = "missing"
    if candidate.email:
        if candidate.email_status == "valid" and (candidate.email_confidence or 0) >= 80:
            email_validity = "verified"
        else:
            email_validity = "unverified"

    skills_str = ", ".join(candidate.skills) if candidate.skills else "not listed"
    required_str = ", ".join(parsed_jd.skills)

    return f"""
Job Description Requirements:
  - Required skills: {required_str}
  - Seniority: {parsed_jd.seniority}
  - Location: {parsed_jd.location}
  - Years experience: {parsed_jd.years_exp}+
  - Programming languages: {', '.join(parsed_jd.languages)}

Candidate Profile:
  - Name: {candidate.name}
  - Title: {candidate.title or 'Unknown'}
  - Company: {candidate.company or 'Unknown'}
  - Location: {candidate.location or 'Unknown'}
  - Skills listed: {skills_str}
  - Email: {candidate.email or 'Not found'} (status: {email_validity})
  - {github_summary}

Score this candidate for the role described above.
"""


async def score_candidate(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> CandidateScored:
    """Score a single candidate using Claude Sonnet 4.5."""
    prompt = _build_candidate_prompt(candidate, parsed_jd)

    messages = [
        SystemMessage(content=SCORING_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = await llm.ainvoke(messages)
    content = response.content

    try:
        score_data = json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from the response if wrapped in text
        import re
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            score_data = json.loads(match.group())
        else:
            log.error("scoring_parse_failed", candidate=candidate.name, response=content[:200])
            score_data = {
                "skill_match_pct": 0,
                "seniority_fit": "unknown",
                "github_score": candidate.github_data.github_score if candidate.github_data else 0,
                "email_validity": "missing",
                "composite_score": 0,
                "rank_justification": "Scoring failed — could not parse AI response.",
            }

    # Build CandidateScored from enriched + score data
    scored = CandidateScored(
        **candidate.model_dump(),
        skill_match_pct=float(score_data.get("skill_match_pct", 0)),
        seniority_fit=score_data.get("seniority_fit", "unknown"),
        github_score=float(score_data.get("github_score", 0)),
        email_validity=score_data.get("email_validity", "missing"),
        composite_score=float(score_data.get("composite_score", 0)),
        rank_justification=score_data.get("rank_justification", ""),
    )
    log.info(
        "candidate_scored",
        name=candidate.name,
        composite=scored.composite_score,
        skill_match=scored.skill_match_pct,
    )
    return scored


async def run_scoring_agent(
    candidates: list[CandidateEnriched],
    parsed_jd: ParsedJD,
) -> list[CandidateScored]:
    """
    Score all candidates concurrently using Claude Sonnet 4.5.
    Returns candidates sorted by composite_score descending with rank assigned.
    """
    import asyncio

    llm = ChatAnthropic(
        model="claude-sonnet-4-5",
        api_key=settings.anthropic_api_key,
        temperature=0.0,   # zero temp for consistent, reproducible scoring
        max_tokens=512,    # score JSON is small
    )

    # Score all candidates concurrently (Claude handles parallel requests)
    tasks = [score_candidate(c, parsed_jd, llm) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    scored_candidates = []
    for original, result in zip(candidates, results):
        if isinstance(result, Exception):
            log.warning("scoring_task_failed", candidate=original.name, error=str(result))
            # Include with zero score so candidate isn't silently dropped
            scored_candidates.append(
                CandidateScored(
                    **original.model_dump(),
                    composite_score=0,
                    rank_justification=f"Scoring error: {result}",
                )
            )
        else:
            scored_candidates.append(result)

    # Sort by composite_score descending and assign ranks
    scored_candidates.sort(key=lambda c: c.composite_score, reverse=True)
    for i, candidate in enumerate(scored_candidates):
        candidate.rank = i + 1

    log.info("scoring_complete", total=len(scored_candidates))
    return scored_candidates
