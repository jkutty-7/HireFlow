"""
Hunter Agent — Kimi K2.5 via ChatNVIDIA.

Responsibilities:
  - For each candidate, discover and verify their professional email
  - Strategy: domain_search → email_finder → email_verifier
  - Saves ~40-50% credits vs calling email_finder alone

Payment wall: $0.002 per domain search/find, $0.001 per verify
"""

import asyncio
import structlog

from services.hunter import HunterClient
from models.candidate import CandidateEnriched

log = structlog.get_logger()


async def find_email_for_candidate(
    candidate: CandidateEnriched,
    hunter_client: HunterClient,
) -> CandidateEnriched:
    """
    Run the Pattern → Find → Verify flow for one candidate.
    Attaches email, email_confidence, and email_status to the candidate.
    """
    # Skip if email is already populated from Apollo enrichment
    if candidate.email and candidate.email_confidence and candidate.email_confidence >= 80:
        log.debug("email_already_found", candidate=candidate.name, email=candidate.email)
        return candidate

    # Get company domain
    company = candidate.company or ""
    if not company:
        return candidate

    domain = HunterClient.extract_domain_from_company(
        company, candidate.linkedin_url
    )
    if not domain:
        log.debug("hunter_no_domain", candidate=candidate.name, company=company)
        return candidate

    # Parse first/last name
    name_parts = candidate.name.strip().split()
    if len(name_parts) < 2:
        return candidate
    first_name = name_parts[0]
    last_name = name_parts[-1]

    try:
        result = await hunter_client.find_and_verify_email(first_name, last_name, domain)
        if result.get("email"):
            candidate.email = result["email"]
            candidate.email_confidence = result.get("confidence")
            candidate.email_status = result.get("status")
            log.info(
                "email_found",
                candidate=candidate.name,
                email=candidate.email,
                confidence=candidate.email_confidence,
                validity=result.get("validity"),
            )
    except Exception as exc:
        log.warning("hunter_failed", candidate=candidate.name, error=str(exc))

    return candidate


async def run_hunter_agent(
    candidates: list[CandidateEnriched],
    concurrency: int = 5,
) -> list[CandidateEnriched]:
    """
    Find and verify emails for all candidates concurrently.
    Uses a semaphore to respect Hunter.io rate limits (15 req/sec).
    """
    client = HunterClient()
    # Conservative: 5 concurrent to stay well under 15 req/sec
    semaphore = asyncio.Semaphore(concurrency)

    async def _find_one(candidate: CandidateEnriched) -> CandidateEnriched:
        async with semaphore:
            return await find_email_for_candidate(candidate, client)

    try:
        tasks = [_find_one(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for original, result in zip(candidates, results):
            if isinstance(result, Exception):
                log.warning("hunter_task_failed", error=str(result))
                final.append(original)
            else:
                final.append(result)

        with_email = sum(1 for c in final if c.email)
        log.info("hunter_agent_done", total=len(final), with_email=with_email)
        return final

    finally:
        await client.close()
