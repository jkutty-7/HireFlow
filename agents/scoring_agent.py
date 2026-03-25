"""
AI Scoring Agent — Claude Sonnet 4.6 (Anthropic).

The ONLY agent that uses Claude. Chosen for deep multi-factor reasoning
and the ability to write coherent justifications.

Phase 2 improvements:
  - Structured per-skill semantic matching (separate LLM call per candidate)
  - Deterministic Python composite formula (not delegated to LLM)
  - Extended 4-5 sentence justifications including skill gap specifics
  - Output validation with safe fallbacks
  - Correct seniority/email normalisation before composite calculation

Input:  List of CandidateEnriched + ParsedJD
Output: List of CandidateScored (sorted by composite_score desc)

Payment: $0.003 USDC per candidate scored via Circle Nanopayments.
"""

import json
import re
import asyncio
import structlog
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from settings import settings
from models.candidate import CandidateEnriched, CandidateScored
from models.job import ParsedJD

log = structlog.get_logger()

# ─── Scoring tables ───────────────────────────────────────────────────────────

SENIORITY_SCORE: dict[str, float] = {
    "match": 20.0,
    "over":  10.0,
    "under":  5.0,
    "unknown": 0.0,
}
EMAIL_SCORE: dict[str, float] = {
    "verified":   10.0,
    "unverified":  5.0,
    "missing":     0.0,
}

VALID_SENIORITY_FIT = frozenset(SENIORITY_SCORE.keys())
VALID_EMAIL_VALIDITY = frozenset(EMAIL_SCORE.keys())


def compute_composite_score(
    skill_match_pct: float,
    seniority_fit: str,
    github_score: float,
    email_validity: str,
) -> float:
    """
    Deterministic composite score — all inputs normalised to 0-100 before weighting.

    Weights:  skill_match_pct 40%  |  github_score 30%  |  seniority 20%  |  email 10%
    """
    seniority_norm = SENIORITY_SCORE.get(seniority_fit, 0.0) / 20.0 * 100.0
    email_norm     = EMAIL_SCORE.get(email_validity,  0.0) / 10.0 * 100.0
    raw = (
        skill_match_pct * 0.40
        + github_score  * 0.30
        + seniority_norm * 0.20
        + email_norm     * 0.10
    )
    return round(min(max(raw, 0.0), 100.0), 2)


# ─── Skill matching prompt ────────────────────────────────────────────────────

SKILL_MATCH_SYSTEM_PROMPT = """
You are a technical recruiter assessing skill alignment between a job description and a candidate.

For each required skill, output a JSON array where each item is:
{"skill": "<required skill>", "matched": true|false, "matched_via": "<exact skill or equivalent, or null>"}

Matching rules:
- "Node.js" matches "JavaScript", "Express", "TypeScript" → matched=true
- "FastAPI" matches "Python", "Starlette", "Python web framework" → matched=true
- "React" does NOT match "Angular" or "Vue" → matched=false
- "PostgreSQL" matches "SQL", "relational databases", "Postgres" → matched=true
- Use the candidate's job title and company to infer unstated skills
  (e.g. "Senior Backend Engineer at Stripe" implies distributed systems, API design)
- If a skill is listed in a different casing or abbreviation, treat as matched

Return ONLY the JSON array — no explanation, no markdown.
"""


async def match_skills_structured(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> tuple[list[dict], float, list[str]]:
    """
    Per-skill semantic matching via structured LLM output.
    Returns (match_detail, skill_match_pct, skill_gaps).
    """
    if not parsed_jd.skills:
        return [], 0.0, []

    required_str = json.dumps(parsed_jd.skills)
    candidate_skills_str = json.dumps(candidate.skills) if candidate.skills else "[]"

    user_msg = f"""Required JD skills: {required_str}
Candidate skills listed: {candidate_skills_str}
Candidate title/context: {candidate.title or 'Unknown'} at {candidate.company or 'Unknown'}"""

    messages = [
        SystemMessage(content=SKILL_MATCH_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    try:
        skill_llm = llm.with_config({"max_tokens": 400})
        response = await skill_llm.ainvoke(messages)
        content = response.content

        try:
            match_detail = json.loads(content)
        except json.JSONDecodeError:
            arr_match = re.search(r"\[.*\]", content, re.DOTALL)
            match_detail = json.loads(arr_match.group()) if arr_match else []

        if not isinstance(match_detail, list):
            match_detail = []

        matched_count = sum(1 for m in match_detail if m.get("matched"))
        skill_match_pct = round(matched_count / max(len(parsed_jd.skills), 1) * 100, 1)
        skill_gaps = [m["skill"] for m in match_detail if not m.get("matched")]

        return match_detail, skill_match_pct, skill_gaps

    except Exception as exc:
        log.warning("skill_match_structured_failed", candidate=candidate.name, error=str(exc))
        return [], 0.0, list(parsed_jd.skills)


# ─── Main scoring prompt ──────────────────────────────────────────────────────

SCORING_SYSTEM_PROMPT = """
You are a senior technical recruiter evaluating a software engineering candidate.

The skill_match_pct has already been computed — do NOT recalculate it.

Respond with ONLY a JSON object containing exactly these fields:
{
  "seniority_fit": "<under | match | over>",
  "email_validity": "<verified | unverified | missing>",
  "rank_justification": "<4-5 sentence assessment>"
}

rank_justification must cover:
1. Primary strengths relative to the role
2. Specific skill gaps (use the skill_gaps list provided)
3. Seniority assessment and reasoning
4. One actionable note for the interviewer
5. Any red flags (job-hopping, overqualification, location mismatch)

Be precise and honest. Flag red flags explicitly.
Respond with ONLY the JSON object — no markdown, no explanation.
"""


def _build_candidate_prompt(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    skill_match_pct: float,
    skill_gaps: list[str],
) -> str:
    """Build the scoring user prompt for one candidate."""
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

    tenure_note = ""
    if candidate.avg_tenure_months is not None:
        tenure_note = (
            f"\n  - Avg tenure: {candidate.avg_tenure_months:.0f} months per role"
            f" ({'job-hopper risk' if candidate.is_job_hopper else 'stable'})"
            f", trajectory: {candidate.career_trajectory}"
        )

    skills_str = ", ".join(candidate.skills) if candidate.skills else "not listed"
    gaps_str   = ", ".join(skill_gaps) if skill_gaps else "none identified"

    return f"""Job Description Requirements:
  - Required skills: {', '.join(parsed_jd.skills)}
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
  - Email: {candidate.email or 'Not found'} (status: {email_validity}){tenure_note}
  - {github_summary}

Pre-computed skill_match_pct: {skill_match_pct:.1f}%
Skill gaps (skills required by JD but not found on candidate): {gaps_str}

Assess seniority_fit and provide rank_justification for this candidate.
"""


async def score_candidate(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> CandidateScored:
    """Score a single candidate using Claude Sonnet 4.6 (two LLM calls)."""

    # Call 1: structured skill matching
    skill_match_detail, skill_match_pct, skill_gaps = await match_skills_structured(
        candidate, parsed_jd, llm
    )

    # Call 2: seniority + justification
    prompt = _build_candidate_prompt(candidate, parsed_jd, skill_match_pct, skill_gaps)
    messages = [
        SystemMessage(content=SCORING_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = await llm.ainvoke(messages)
    content = response.content

    try:
        score_data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                score_data = json.loads(match.group())
            except json.JSONDecodeError:
                score_data = {}
        else:
            score_data = {}

    if not score_data:
        log.error("scoring_parse_failed", candidate=candidate.name, response=content[:200])

    # Validate and sanitise LLM outputs
    seniority_fit = str(score_data.get("seniority_fit", "unknown")).lower()
    if seniority_fit not in VALID_SENIORITY_FIT:
        seniority_fit = "unknown"

    email_validity = str(score_data.get("email_validity", "missing")).lower()
    if email_validity not in VALID_EMAIL_VALIDITY:
        email_validity = "missing"

    rank_justification = score_data.get("rank_justification", "")
    if not isinstance(rank_justification, str):
        rank_justification = ""

    github_score = float(candidate.github_data.github_score if candidate.github_data else 0)

    # Deterministic composite — no LLM arithmetic
    composite = compute_composite_score(
        skill_match_pct=skill_match_pct,
        seniority_fit=seniority_fit,
        github_score=github_score,
        email_validity=email_validity,
    )

    scored = CandidateScored(
        **candidate.model_dump(),
        skill_match_pct=skill_match_pct,
        seniority_fit=seniority_fit,
        github_score=github_score,
        email_validity=email_validity,
        composite_score=composite,
        rank_justification=rank_justification,
        skill_match_detail=skill_match_detail,
        skill_gaps=skill_gaps,
    )
    log.info(
        "candidate_scored",
        name=candidate.name,
        composite=scored.composite_score,
        skill_match=scored.skill_match_pct,
        skill_gaps=skill_gaps,
    )
    return scored


async def run_scoring_agent(
    candidates: list[CandidateEnriched],
    parsed_jd: ParsedJD,
) -> list[CandidateScored]:
    """
    Score all candidates concurrently using Claude Sonnet 4.6.
    Returns candidates sorted by composite_score descending with rank assigned.
    """
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
        temperature=0.0,
        max_tokens=1024,
    )

    tasks = [score_candidate(c, parsed_jd, llm) for c in candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    scored_candidates = []
    for original, result in zip(candidates, results):
        if isinstance(result, Exception):
            log.warning("scoring_task_failed", candidate=original.name, error=str(result))
            scored_candidates.append(
                CandidateScored(
                    **original.model_dump(),
                    composite_score=0,
                    rank_justification=f"Scoring error: {result}",
                )
            )
        else:
            scored_candidates.append(result)

    scored_candidates.sort(key=lambda c: c.composite_score, reverse=True)
    for i, candidate in enumerate(scored_candidates):
        candidate.rank = i + 1

    log.info("scoring_complete", total=len(scored_candidates))
    return scored_candidates
