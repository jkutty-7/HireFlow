"""
AI Scoring Agent — Claude Sonnet 4.6 (Anthropic).

Phase 2 improvements:
  - Single LLM call per candidate (merged skill match + justification → 50% cost reduction)
  - Required vs optional skill weighting (required=1.0, optional=0.5)
  - Deterministic Python composite formula (not delegated to LLM)
  - Output validation with safe fallbacks

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
    "match":   20.0,
    "over":    10.0,
    "under":    5.0,
    "unknown":  0.0,
}
EMAIL_SCORE: dict[str, float] = {
    "verified":   10.0,
    "unverified":  5.0,
    "risky":       2.5,
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


def _compute_skill_match_pct(
    match_detail: list[dict],
    required_skills: list[str],
    optional_skills: list[str],
) -> tuple[float, list[str]]:
    """
    Weighted skill_match_pct:
      - matched required skill = 1.0 point
      - matched optional skill = 0.5 points
    Max achievable = len(required) * 1.0 + len(optional) * 0.5

    Returns (skill_match_pct, skill_gaps).
    """
    if not match_detail:
        return 0.0, list(required_skills)

    req_set  = {s.lower() for s in required_skills}
    opt_set  = {s.lower() for s in optional_skills}
    max_score = len(required_skills) * 1.0 + len(optional_skills) * 0.5
    if max_score == 0:
        return 0.0, []

    achieved = 0.0
    gaps: list[str] = []
    for m in match_detail:
        skill_lower = m.get("skill", "").lower()
        matched = bool(m.get("matched"))
        is_req = skill_lower in req_set
        is_opt = skill_lower in opt_set

        if matched:
            achieved += 1.0 if is_req else 0.5
        else:
            if is_req:
                gaps.append(m["skill"])
            # optional misses are not shown as gaps

    pct = round(achieved / max_score * 100, 1)
    return min(pct, 100.0), gaps


# ─── Combined single-call scoring prompt ────────────────────────────────────

COMBINED_SCORING_PROMPT = """
You are a senior technical recruiter evaluating a software engineering candidate.

Respond with ONLY a JSON object containing exactly these fields:
{
  "skill_matches": [
    {"skill": "<skill name>", "matched": true|false, "matched_via": "<exact candidate skill or null>", "is_required": true|false}
  ],
  "seniority_fit": "under | match | over",
  "email_validity": "verified | unverified | risky | missing",
  "rank_justification": "<4-5 sentence assessment>"
}

Skill matching rules:
- Include every skill from BOTH required_skills and optional_skills in skill_matches
- "Node.js" matches "JavaScript", "Express", "TypeScript" → matched=true
- "FastAPI" matches "Python", "Starlette", "Python web framework" → matched=true
- "React" does NOT match "Angular" or "Vue" → matched=false
- "PostgreSQL" matches "SQL", "relational databases", "Postgres" → matched=true
- Use the candidate's job title and company to infer unstated skills
- Set is_required=true for required skills, is_required=false for optional skills

rank_justification must cover:
1. Primary strengths relative to the role
2. Specific skill gaps from required_skills (optional misses are minor)
3. Seniority assessment and reasoning
4. One actionable note for the interviewer
5. Any red flags (job-hopping, overqualification, location mismatch)

Be precise and honest. Flag red flags explicitly.
Return ONLY the JSON object — no markdown, no explanation.
"""


def _build_scoring_prompt(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
) -> str:
    """Build the single combined scoring prompt for one candidate."""
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

    email_hint = "missing"
    if candidate.email:
        if candidate.email_status == "valid" and (candidate.email_confidence or 0) >= 80:
            email_hint = "verified"
        elif candidate.email_status == "risky":
            email_hint = "risky"
        else:
            email_hint = "unverified"

    tenure_note = ""
    if candidate.avg_tenure_months is not None:
        tenure_note = (
            f"\n  - Avg tenure: {candidate.avg_tenure_months:.0f} months per role"
            f" ({'job-hopper risk' if candidate.is_job_hopper else 'stable'})"
            f", trajectory: {candidate.career_trajectory}"
        )

    skills_str   = ", ".join(candidate.skills) if candidate.skills else "not listed"
    req_str      = ", ".join(parsed_jd.required_skills) if parsed_jd.required_skills else "none"
    opt_str      = ", ".join(parsed_jd.optional_skills) if parsed_jd.optional_skills else "none"

    return f"""Job Description Requirements:
  - Required skills:  {req_str}
  - Optional skills:  {opt_str}
  - Seniority:        {parsed_jd.seniority}
  - Location:         {parsed_jd.location}
  - Years experience: {parsed_jd.years_exp}+
  - Languages:        {', '.join(parsed_jd.languages) or 'not specified'}

Candidate Profile:
  - Name:     {candidate.name}
  - Title:    {candidate.title or 'Unknown'}
  - Company:  {candidate.company or 'Unknown'}
  - Location: {candidate.location or 'Unknown'}
  - Skills listed: {skills_str}
  - Email:    {candidate.email or 'Not found'} (status hint: {email_hint}){tenure_note}
  - {github_summary}

Evaluate this candidate against the job description above.
Include ALL skills from both required_skills and optional_skills in skill_matches.
"""


async def score_candidate(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> CandidateScored:
    """Score a single candidate using ONE Claude Sonnet 4.6 call."""

    prompt = _build_scoring_prompt(candidate, parsed_jd)
    messages = [
        SystemMessage(content=COMBINED_SCORING_PROMPT),
        HumanMessage(content=prompt),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content
    except Exception as exc:
        log.warning("scoring_failed", candidate=candidate.name, error=str(exc)[:150])
        github_score = float(candidate.github_data.github_score if candidate.github_data else 0)
        return CandidateScored(
            **candidate.model_dump(),
            composite_score=round(github_score * 0.30, 2),
            rank_justification=f"Scoring unavailable: {exc}",
        )

    # ── Parse response ────────────────────────────────────────────────────────
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

    # ── Validate LLM outputs ──────────────────────────────────────────────────
    seniority_fit = str(score_data.get("seniority_fit", "unknown")).lower()
    if seniority_fit not in VALID_SENIORITY_FIT:
        seniority_fit = "unknown"

    email_validity = str(score_data.get("email_validity", "missing")).lower()
    if email_validity not in VALID_EMAIL_VALIDITY:
        email_validity = "missing"

    rank_justification = score_data.get("rank_justification", "")
    if not isinstance(rank_justification, str):
        rank_justification = ""

    # ── Skill match with weighted formula ────────────────────────────────────
    match_detail: list[dict] = score_data.get("skill_matches", [])
    if not isinstance(match_detail, list):
        match_detail = []

    skill_match_pct, skill_gaps = _compute_skill_match_pct(
        match_detail,
        parsed_jd.required_skills,
        parsed_jd.optional_skills,
    )

    github_score = float(candidate.github_data.github_score if candidate.github_data else 0)

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
        skill_match_detail=match_detail,
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
    market_context: str = "",
) -> list[CandidateScored]:
    """
    Score all candidates concurrently using Claude Sonnet 4.6 (one call per candidate).
    Returns candidates sorted by composite_score descending with rank assigned.
    """
    system_prompt = COMBINED_SCORING_PROMPT
    if market_context:
        system_prompt = f"Market context: {market_context}\n\n{system_prompt}"

    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
        temperature=0.0,
        max_tokens=1024,
    )

    semaphore = asyncio.Semaphore(3)

    async def _score_one(c: CandidateEnriched) -> CandidateScored:
        async with semaphore:
            return await score_candidate(c, parsed_jd, llm)

    tasks = [_score_one(c) for c in candidates]
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

    scored_candidates.sort(
        key=lambda c: (c.composite_score, c.github_score, c.skill_match_pct),
        reverse=True,
    )
    for i, candidate in enumerate(scored_candidates):
        candidate.rank = i + 1

    log.info("scoring_complete", total=len(scored_candidates))
    return scored_candidates
