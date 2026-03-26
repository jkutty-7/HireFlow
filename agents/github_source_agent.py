"""
GitHub Repo Source Agent — discovers candidates from relevant open-source projects.

Strategy:
  1. Build a GitHub repository search query from the parsed JD (languages + skills)
  2. Find the top matching repos (sorted by stars)
  3. Pull each repo's top contributors
  4. Fetch full GitHub profiles for each unique contributor
  5. Return as CandidateEnriched objects (github_data already populated — no re-enrichment needed)

Why this works:
  Developers who have built projects using the exact technologies in the JD are
  by definition strong signals. Their public repos are living proof of skill.

Payment: $0.001 per repo search + $0.001 per repo scanned for contributors.
"""

import asyncio
import structlog

from services.github import GitHubClient
from models.candidate import CandidateEnriched, GitHubProfile
from models.job import ParsedJD

log = structlog.get_logger()

# Language aliases for building the GitHub search query
_QUERY_LANG_MAP: dict[str, str] = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "c#": "csharp",
}


def _build_repo_query(parsed_jd: ParsedJD) -> str:
    """
    Construct a GitHub repository search query from the JD.

    Strategy:
    - Use the primary language as a language: filter (most precise signal)
    - Add top 3 skill keywords as free-text terms (repo description/readme match)
    - Require at least 5 stars to exclude toy/empty projects

    Example output: "language:python fastapi postgresql async stars:>5"
    """
    parts: list[str] = []

    # Primary language filter
    languages = parsed_jd.languages or []
    if languages:
        primary = _QUERY_LANG_MAP.get(languages[0].lower(), languages[0].lower())
        parts.append(f"language:{primary}")

    # Top skill keywords (avoid duplicating the language)
    lang_set = {l.lower() for l in languages}
    skill_keywords = [
        s for s in (parsed_jd.skills or [])
        if s.lower() not in lang_set
    ][:3]
    parts.extend(skill_keywords)

    # Minimum stars filter — keeps results meaningful
    parts.append("stars:>5")

    return " ".join(parts) if parts else "stars:>50"


async def _candidate_from_github_profile(
    username: str,
    source_repos: list[str],
    client: GitHubClient,
    required_languages: list[str],
) -> CandidateEnriched | None:
    """
    Fetch a full GitHub profile and convert it into a CandidateEnriched.
    Returns None on any API failure (silently skipped by caller).
    """
    try:
        profile: GitHubProfile = await client.build_github_profile(
            username=username,
            required_languages=required_languages,
        )
        raw_profile = await client.get_user_profile(username)

        name = raw_profile.get("name") or username
        company = (raw_profile.get("company") or "").lstrip("@").strip() or None
        location = raw_profile.get("location")
        email = raw_profile.get("email")  # public email if the user has set one
        bio = raw_profile.get("bio") or ""

        # Derive a rough title from bio (first sentence, max 80 chars)
        title = bio.split(".")[0].strip()[:80] if bio else None

        # Infer skills from top languages in their repos
        skills = [
            lang for lang, _ in sorted(
                profile.top_languages.items(), key=lambda x: x[1], reverse=True
            )
        ][:8]

        return CandidateEnriched(
            name=name,
            title=title,
            company=company,
            location=location,
            github_username=username,
            github_url=f"https://github.com/{username}",
            github_data=profile,
            email=email,
            email_status="valid" if email else None,
            skills=skills,
            source="github_repo",
            source_repos=source_repos,
        )
    except Exception as exc:
        log.debug("github_source_profile_failed", username=username, error=str(exc))
        return None


async def run_github_source_agent(
    parsed_jd: ParsedJD,
    max_repos: int = 5,
    max_contributors_per_repo: int = 10,
    concurrency: int = 3,
) -> list[CandidateEnriched]:
    """
    Discover candidates by mining contributors from relevant GitHub repositories.

    Returns a list of CandidateEnriched with github_data already populated.
    The caller should deduplicate against Apollo candidates by github_username.
    """
    client = GitHubClient()
    try:
        query = _build_repo_query(parsed_jd)
        log.info("github_source_search", query=query, max_repos=max_repos)

        # ── Step 1: Find matching repos ────────────────────────────────────────
        try:
            repos = await client.search_repos(query, max_repos=max_repos)
        except Exception as exc:
            log.warning("github_source_repo_search_failed", error=str(exc))
            return []

        if not repos:
            log.info("github_source_no_repos", query=query)
            return []

        log.info("github_source_repos_found", count=len(repos), repos=[r["full_name"] for r in repos])

        # ── Step 2: Collect contributors from all repos ────────────────────────
        # contributor_login → list of repos they appear in
        contributor_repos: dict[str, list[str]] = {}

        contributor_tasks = [
            client.get_repo_contributors(r["owner"], r["name"], max_contributors=max_contributors_per_repo)
            for r in repos
        ]
        results = await asyncio.gather(*contributor_tasks, return_exceptions=True)

        for repo, result in zip(repos, results):
            if isinstance(result, Exception):
                log.warning("github_source_contributors_failed", repo=repo["full_name"], error=str(result))
                continue
            for contributor in result:
                login = contributor["login"]
                contributor_repos.setdefault(login, []).append(repo["full_name"])

        if not contributor_repos:
            log.info("github_source_no_contributors")
            return []

        log.info("github_source_unique_contributors", count=len(contributor_repos))

        # ── Step 3: Build candidate profiles (rate-limited via semaphore) ───────
        semaphore = asyncio.Semaphore(concurrency)
        required_languages = parsed_jd.languages or []

        async def _fetch_one(login: str, repos_list: list[str]) -> CandidateEnriched | None:
            async with semaphore:
                return await _candidate_from_github_profile(
                    login, repos_list, client, required_languages
                )

        profile_tasks = [
            _fetch_one(login, repos_list)
            for login, repos_list in contributor_repos.items()
        ]
        profiles = await asyncio.gather(*profile_tasks, return_exceptions=True)

        candidates: list[CandidateEnriched] = []
        for result in profiles:
            if isinstance(result, Exception) or result is None:
                continue
            candidates.append(result)

        log.info(
            "github_source_done",
            repos_scanned=len(repos),
            contributors_found=len(contributor_repos),
            candidates_built=len(candidates),
        )
        return candidates

    finally:
        await client.close()
