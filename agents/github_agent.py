"""
GitHub Agent — Kimi K2.5 via ChatNVIDIA.

Responsibilities:
  - Given a candidate's GitHub username (from Apollo enrichment or name search),
    fetch their profile, repos, and activity
  - Compute github_score (0-100) using the formula from the design doc
  - Attach GitHubProfile to each CandidateEnriched

Payment wall: $0.001 per profile lookup, $0.001 per repo fetch
"""

import re
import asyncio
import structlog

from services.github import GitHubClient
from models.candidate import CandidateEnriched, GitHubProfile
from models.job import ParsedJD

log = structlog.get_logger()


def _extract_username_from_url(github_url: str | None) -> str | None:
    """Extract GitHub username from a URL like https://github.com/username."""
    if not github_url:
        return None
    match = re.search(r"github\.com/([^/?#]+)", github_url)
    return match.group(1) if match else None


async def enrich_with_github(
    candidate: CandidateEnriched,
    parsed_jd: ParsedJD,
    github_client: GitHubClient,
) -> CandidateEnriched:
    """
    Fetch GitHub data for a single candidate and attach it to their profile.
    Returns the candidate with github_data and github_username populated.
    """
    # Skip candidates already enriched by the GitHub Source Agent
    if candidate.github_data is not None:
        return candidate

    # Try to get username from GitHub URL
    username = candidate.github_username or _extract_username_from_url(candidate.github_url)

    if not username:
        log.debug("github_no_username", candidate=candidate.name)
        return candidate

    try:
        profile: GitHubProfile = await github_client.build_github_profile(
            username=username,
            required_languages=parsed_jd.languages,
        )
        candidate.github_username = username
        candidate.github_data = profile
        log.info(
            "github_enriched",
            candidate=candidate.name,
            username=username,
            score=profile.github_score,
        )
    except Exception as exc:
        log.warning("github_enrichment_failed", candidate=candidate.name, error=str(exc))

    return candidate


async def run_github_agent(
    candidates: list[CandidateEnriched],
    parsed_jd: ParsedJD,
    concurrency: int = 5,
) -> list[CandidateEnriched]:
    """
    Enrich all candidates with GitHub data concurrently.
    Uses a semaphore to stay within GitHub rate limits (5k req/hr).
    """
    client = GitHubClient()
    semaphore = asyncio.Semaphore(concurrency)

    async def _enrich_one(candidate: CandidateEnriched) -> CandidateEnriched:
        async with semaphore:
            return await enrich_with_github(candidate, parsed_jd, client)

    try:
        tasks = [_enrich_one(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched = []
        for original, result in zip(candidates, results):
            if isinstance(result, Exception):
                log.warning("github_task_failed", error=str(result))
                enriched.append(original)
            else:
                enriched.append(result)

        log.info(
            "github_agent_done",
            total=len(enriched),
            with_github=sum(1 for c in enriched if c.github_data is not None),
        )
        return enriched

    finally:
        await client.close()
