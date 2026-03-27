"""
Candidate Merger — deduplication and post-enrichment merging.

Extracted from orchestrator._collect_data_node for testability and clarity.

Two merge operations:
  1. merge_sources()   — Apollo candidates + GitHub Repo Source candidates
                         Deduplicates by github_username so the same dev
                         isn't scored twice.
  2. merge_enrichment() — After GitHub enrichment + Hunter email lookup
                          run in parallel, splice email data back into the
                          GitHub-enriched list (which is the authoritative copy).
"""

import structlog
from models.candidate import CandidateEnriched

log = structlog.get_logger()


def merge_sources(
    apollo_candidates: list[CandidateEnriched],
    github_source_candidates: list[CandidateEnriched],
) -> list[CandidateEnriched]:
    """
    Merge Apollo + GitHub Repo Source candidate pools.

    Deduplication strategy (in priority order):
      1. github_username match — same developer found by both sources
      2. apollo_id match       — (future) cross-reference if Apollo finds a GitHub user

    Apollo candidates are kept as-is; GitHub-source candidates are appended
    only if they are NOT already represented in the Apollo pool.
    """
    existing_github_usernames: set[str] = {
        c.github_username for c in apollo_candidates if c.github_username
    }
    new_from_repos = [
        c for c in github_source_candidates
        if c.github_username not in existing_github_usernames
    ]

    merged = apollo_candidates + new_from_repos

    log.info(
        "candidate_sources_merged",
        apollo=len(apollo_candidates),
        github_source=len(github_source_candidates),
        new_unique_from_repos=len(new_from_repos),
        total=len(merged),
    )
    return merged


def merge_enrichment(
    github_enriched: list[CandidateEnriched],
    hunter_enriched: list[CandidateEnriched],
) -> list[CandidateEnriched]:
    """
    Splice Hunter email results back onto the GitHub-enriched candidate list.

    GitHub enrichment is the authoritative copy of all candidate data.
    Hunter only provides email, email_confidence, email_status — those
    three fields are grafted onto the matching candidate.

    Matching strategy:
      - Apollo candidates: matched by apollo_id
      - GitHub-source candidates: matched by github_username (no apollo_id)
    """
    hunter_by_apollo: dict[str, CandidateEnriched] = {
        c.apollo_id: c for c in hunter_enriched if c.apollo_id
    }
    hunter_by_github: dict[str, CandidateEnriched] = {
        c.github_username: c
        for c in hunter_enriched
        if c.github_username and not c.apollo_id
    }

    merged: list[CandidateEnriched] = []
    for c in github_enriched:
        if c.apollo_id and c.apollo_id in hunter_by_apollo:
            h = hunter_by_apollo[c.apollo_id]
            c.email = h.email
            c.email_confidence = h.email_confidence
            c.email_status = h.email_status
        elif c.github_username and c.github_username in hunter_by_github and not c.email:
            h = hunter_by_github[c.github_username]
            c.email = h.email
            c.email_confidence = h.email_confidence
            c.email_status = h.email_status
        merged.append(c)

    with_email = sum(1 for c in merged if c.email)
    log.info("enrichment_merged", total=len(merged), with_email=with_email)
    return merged
