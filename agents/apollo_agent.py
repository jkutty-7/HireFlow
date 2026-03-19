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


async def run_apollo_agent(
    parsed_jd: ParsedJD,
    max_candidates: int = 25,
) -> list[CandidateEnriched]:
    """
    Run the Apollo data collection pipeline:
      1. Search for candidates (free, no credits)
      2. Batch-enrich top candidates (credits consumed)
    """
    client = ApolloClient()
    try:
        # Step 1: Discover candidates
        # Apollo needs real city/country — skip location filter for "Remote" roles
        location = parsed_jd.location or ""
        is_remote = "remote" in location.lower() or not location
        locations = [] if is_remote else [location]

        log.info("apollo_search_start", titles=parsed_jd.titles, location=location, remote=is_remote)
        # Use top 3 skills only — too many keywords over-filters Apollo results
        top_skills = (parsed_jd.languages + parsed_jd.skills)[:3]
        keywords = " ".join(top_skills)

        raw_candidates: list[CandidateRaw] = await client.search_people(
            titles=parsed_jd.titles or ["Software Engineer", "Senior Software Engineer"],
            locations=locations,
            seniorities=[parsed_jd.seniority],
            keywords=keywords,
            per_page=max_candidates,
        )
        log.info("apollo_search_done", count=len(raw_candidates))

        if not raw_candidates:
            return []

        # Step 2: Batch-enrich in groups of 10 (API limit)
        apollo_ids = [c.apollo_id for c in raw_candidates if c.apollo_id]
        enriched: list[CandidateEnriched] = []

        batches = [apollo_ids[i:i + 10] for i in range(0, len(apollo_ids), 10)]
        for batch in batches:
            batch_result = await client.bulk_enrich(batch)
            enriched.extend(batch_result)
            log.info("apollo_batch_enriched", batch_size=len(batch))

        # For candidates without apollo_id, do single enrichment
        no_id = [c for c in raw_candidates if not c.apollo_id]
        for candidate in no_id[:5]:  # limit to 5 to save credits
            result = await client.enrich_person(
                name=candidate.name,
                organization_name=candidate.company or "",
            )
            if result:
                enriched.append(result)

        log.info("apollo_enrichment_done", total=len(enriched))
        return enriched

    finally:
        await client.close()
