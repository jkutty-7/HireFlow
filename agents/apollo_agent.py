"""
Apollo Agent — Kimi K2.5 via ChatNVIDIA.

Responsibilities:
  - Search Apollo.io for candidates matching the parsed JD
  - Bulk-enrich the top candidates (skills, employment history, email hints)

Payment wall: $0.001 per search, $0.003 per enrichment (x402 on each call)
"""

import asyncio
import structlog

from langchain_core.tools import tool
from agents.base import create_kimi_agent
from services.apollo import ApolloClient
from models.candidate import CandidateRaw, CandidateEnriched
from models.job import ParsedJD

log = structlog.get_logger()

APOLLO_SYSTEM_PROMPT = """
You are a talent acquisition agent that uses Apollo.io to find software engineering candidates.

Your job is to:
1. Search for candidates using the provided job criteria (titles, location, seniority, keywords)
2. Return the raw list of candidate profiles found

Always use the search_candidates tool first, then enrich_candidates with the returned IDs.
Respond with a JSON array of enriched candidate profiles when done.
"""


async def _do_apollo_search(
    client: ApolloClient,
    parsed_jd: ParsedJD,
    max_candidates: int,
) -> list[CandidateEnriched]:
    """
    Single Apollo search + enrich pass. Extracted so run_apollo_agent can retry.
    """
    location = parsed_jd.location or ""
    is_remote = "remote" in location.lower() or not location
    locations = [] if is_remote else [location]

    seniorities = [parsed_jd.seniority] if parsed_jd.seniority else []
    top_skills = (parsed_jd.languages + parsed_jd.skills)[:3]
    keywords = " ".join(top_skills)

    raw_candidates: list[CandidateRaw] = await client.search_people(
        titles=parsed_jd.titles or ["Software Engineer", "Senior Software Engineer"],
        locations=locations,
        seniorities=seniorities,
        keywords=keywords,
        per_page=max_candidates,
    )

    if not raw_candidates:
        return []

    apollo_ids = [c.apollo_id for c in raw_candidates if c.apollo_id]
    enriched: list[CandidateEnriched] = []

    batches = [apollo_ids[i:i + 10] for i in range(0, len(apollo_ids), 10)]
    for batch in batches:
        batch_result = await client.bulk_enrich(batch)
        enriched.extend(batch_result)
        log.info("apollo_batch_enriched", batch_size=len(batch))

    no_id = [c for c in raw_candidates if not c.apollo_id]
    for candidate in no_id[:5]:
        result = await client.enrich_person(
            name=candidate.name,
            organization_name=candidate.company or "",
        )
        if result:
            enriched.append(result)

    return enriched


async def run_apollo_agent(
    parsed_jd: ParsedJD,
    max_candidates: int = 25,
    min_threshold: int = 8,
) -> list[CandidateEnriched]:
    """
    Run the Apollo data collection pipeline:
      1. Search for candidates (free, no credits)
      2. Batch-enrich top candidates (credits consumed)
      3. If results are thin (< min_threshold), retry with relaxed parameters
    """
    client = ApolloClient()
    try:
        log.info("apollo_search_start", titles=parsed_jd.titles, seniority=parsed_jd.seniority)

        enriched = await _do_apollo_search(client, parsed_jd, max_candidates)
        log.info("apollo_search_done", count=len(enriched))

        # Retry with relaxed parameters if results are thin
        if len(enriched) < min_threshold:
            log.warning(
                "apollo_thin_results_retrying",
                count=len(enriched),
                threshold=min_threshold,
            )
            relaxed_jd = parsed_jd.model_copy(update={
                "seniority": "",  # remove seniority filter
                "titles": list(set(parsed_jd.titles + ["Software Engineer", "Engineer", "Developer"])),
            })
            remaining = max_candidates - len(enriched)
            additional = await _do_apollo_search(client, relaxed_jd, remaining)

            # Deduplicate by apollo_id
            existing_ids = {c.apollo_id for c in enriched if c.apollo_id}
            new_candidates = [c for c in additional if c.apollo_id not in existing_ids]
            enriched.extend(new_candidates)
            log.info("apollo_retry_done", total=len(enriched), added=len(new_candidates))

        log.info("apollo_enrichment_done", total=len(enriched))
        return enriched

    finally:
        await client.close()
