"""
Talent Intelligence Agent — Claude Sonnet 4.6 (Anthropic).

Runs AFTER score_candidates. Produces a post-pool analysis report:
  - Executive summary of the top 3 candidates
  - Search quality score and notes (thin pool? good results?)
  - Red flags across the candidate pool (job-hopping, overqualification, etc.)
  - Recommended JD improvements if results were poor
  - 3 tailored interview questions per top-5 candidate (based on skill gaps)

The report is additive — a failure here does not fail the search pipeline.

Payment: $0.005 USDC per report via Circle Nanopayments.
"""

import json
import re
import asyncio
import structlog
from datetime import datetime, timezone

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from settings import settings
from models.candidate import CandidateScored
from models.job import ParsedJD
from models.intelligence import TalentIntelligenceReport, CandidateInterviewPlan

log = structlog.get_logger()


POOL_ANALYSIS_SYSTEM_PROMPT = """
You are a senior recruiting analyst reviewing a pool of scored software engineering candidates.

Respond with ONLY a JSON object with exactly these fields:
{
  "top_3_summary": "<2-3 sentence paragraph: who are the top 3 candidates and why they stand out>",
  "search_quality_score": <0-100 integer: how good was this candidate pool overall?>,
  "search_quality_notes": "<1-2 sentences explaining the quality score>",
  "red_flags": ["<flag1>", "<flag2>"],
  "recommended_jd_changes": ["<change1>", "<change2>"],
  "market_context_hint": "<one sentence on talent availability for the rarest required skill, e.g. 'Vyper is niche with <500 active developers globally — Solidity+Python should be accepted as equivalent.' Leave empty string if all skills are mainstream.>"
}

Red flags: job-hopping (avg tenure < 12 months), overqualified, underqualified,
thin skill match across the pool, no verified emails, location mismatches.

Recommended JD changes: if the pool is thin or low quality, suggest concrete improvements
(e.g. "Broaden to include mid-level candidates", "Remove Kubernetes requirement").

market_context_hint: identify the single rarest required skill and provide a one-sentence
note on market availability and any acceptable equivalents. Leave as "" for mainstream stacks.

Respond with ONLY the JSON object — no markdown, no explanation.
"""

INTERVIEW_QUESTIONS_SYSTEM_PROMPT = """
You are a technical interviewer preparing for a candidate screen.

Given a candidate's skill gaps and the job requirements, generate exactly 3 interview questions.
Questions should probe the specific gaps and verify the candidate's seniority.

Each question should be specific, open-ended, and require a substantive answer.
Avoid generic questions like "Tell me about yourself."

Respond with ONLY a JSON array of 3 strings — no markdown, no explanation.
["question 1", "question 2", "question 3"]
"""


async def _generate_pool_analysis(
    candidates: list[CandidateScored],
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> dict:
    """Generate pool-level analysis: top-3 summary, quality score, red flags."""
    top_10 = candidates[:10]

    candidates_compact = []
    for c in top_10:
        candidates_compact.append({
            "rank": c.rank,
            "name": c.name,
            "title": c.title,
            "company": c.company,
            "composite_score": c.composite_score,
            "skill_match_pct": c.skill_match_pct,
            "seniority_fit": c.seniority_fit,
            "skill_gaps": c.skill_gaps,
            "is_job_hopper": c.is_job_hopper,
            "avg_tenure_months": c.avg_tenure_months,
            "has_verified_email": c.email_validity == "verified",
            "location": c.location,
        })

    user_msg = f"""Job Requirements:
  - Role: {', '.join(parsed_jd.titles) if parsed_jd.titles else 'Software Engineer'}
  - Seniority: {parsed_jd.seniority}
  - Required skills: {', '.join(parsed_jd.skills)}
  - Location: {parsed_jd.location}

Candidate Pool (top {len(top_10)} of {len(candidates)} total, sorted by composite score):
{json.dumps(candidates_compact, indent=2)}

Analyse this candidate pool."""

    messages = [
        SystemMessage(content=POOL_ANALYSIS_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    response = await llm.ainvoke(messages)
    content = response.content

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        data = json.loads(match.group()) if match else {}

    return data or {}


async def _generate_interview_questions(
    candidate: CandidateScored,
    parsed_jd: ParsedJD,
    llm: ChatAnthropic,
) -> CandidateInterviewPlan:
    """Generate 3 targeted interview questions for one candidate."""
    skill_gaps = candidate.skill_gaps or []
    gaps_str = ", ".join(skill_gaps) if skill_gaps else "none identified — focus on depth"

    user_msg = f"""Candidate: {candidate.name} ({candidate.title} at {candidate.company})
Role: {', '.join(parsed_jd.titles) if parsed_jd.titles else 'Software Engineer'} ({parsed_jd.seniority})
Required skills: {', '.join(parsed_jd.skills)}
Candidate skill gaps: {gaps_str}
Seniority fit: {candidate.seniority_fit}
Justification: {candidate.rank_justification}

Generate 3 interview questions for this specific candidate."""

    messages = [
        SystemMessage(content=INTERVIEW_QUESTIONS_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    try:
        iq_llm = llm.with_config({"max_tokens": 512})
        response = await iq_llm.ainvoke(messages)
        content = response.content

        try:
            questions = json.loads(content)
        except json.JSONDecodeError:
            arr_match = re.search(r"\[.*\]", content, re.DOTALL)
            questions = json.loads(arr_match.group()) if arr_match else []

        if not isinstance(questions, list):
            questions = []

        # Ensure exactly 3
        questions = [str(q) for q in questions[:3]]
        while len(questions) < 3:
            questions.append(f"Walk me through your experience with {skill_gaps[0] if skill_gaps else parsed_jd.skills[0] if parsed_jd.skills else 'the core technologies in this role'}.")

    except Exception as exc:
        log.warning("interview_questions_failed", candidate=candidate.name, error=str(exc))
        questions = [
            f"Describe your experience with {parsed_jd.skills[0] if parsed_jd.skills else 'the core tech stack'}.",
            f"How have you handled {skill_gaps[0] if skill_gaps else 'complex technical challenges'} in past roles?",
            "Walk me through a system design decision you made and the trade-offs involved.",
        ]

    return CandidateInterviewPlan(
        candidate_name=candidate.name,
        rank=candidate.rank or 0,
        composite_score=candidate.composite_score,
        interview_questions=questions,
        skill_gap_focus=skill_gaps,
    )


async def run_talent_intelligence_agent(
    candidates: list[CandidateScored],
    parsed_jd: ParsedJD,
    search_id: str,
) -> TalentIntelligenceReport:
    """
    Analyse the scored candidate pool and generate:
      - Executive top-3 summary
      - Search quality score + notes
      - Red flags
      - JD improvement suggestions
      - 3 interview questions per top-5 candidate
    """
    if not candidates:
        log.warning("talent_intelligence_no_candidates", search_id=search_id)
        return TalentIntelligenceReport(
            search_id=search_id,
            search_quality_notes="No candidates were found for this search.",
            search_quality_score=0,
        )

    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        api_key=settings.anthropic_api_key,
        temperature=0.2,
        max_tokens=2048,
    )

    # Call 1: pool-level analysis
    pool_data = await _generate_pool_analysis(candidates, parsed_jd, llm)

    # Call 2: interview questions for top-5 candidates (parallel)
    top_5 = candidates[:5]
    interview_tasks = [
        _generate_interview_questions(c, parsed_jd, llm) for c in top_5
    ]
    interview_plans = await asyncio.gather(*interview_tasks, return_exceptions=True)

    valid_plans: list[CandidateInterviewPlan] = []
    for plan in interview_plans:
        if isinstance(plan, Exception):
            log.warning("interview_plan_failed", error=str(plan))
        else:
            valid_plans.append(plan)

    report = TalentIntelligenceReport(
        search_id=search_id,
        top_3_summary=str(pool_data.get("top_3_summary", "")),
        search_quality_score=int(pool_data.get("search_quality_score", 0)),
        search_quality_notes=str(pool_data.get("search_quality_notes", "")),
        red_flags=[str(f) for f in pool_data.get("red_flags", [])],
        recommended_jd_changes=[str(c) for c in pool_data.get("recommended_jd_changes", [])],
        market_context_hint=str(pool_data.get("market_context_hint", "")),
        interview_plans=valid_plans,
        generated_at=datetime.now(timezone.utc),
    )

    log.info(
        "talent_intelligence_done",
        search_id=search_id,
        quality_score=report.search_quality_score,
        red_flags=len(report.red_flags),
        interview_plans=len(report.interview_plans),
    )
    return report
