"""
GitHub Source Agent — discovers candidates from public GitHub activity.

Two complementary discovery paths run in parallel:

  1. REPO PATH: search recently-active repos matching the tech stack, then mine
     their top contributors. Optimised for "who is currently building with X?"
     — no star floor, recently-pushed only, archived projects excluded.

  2. USER PATH: search GitHub users directly by primary language (and location
     if specified). Optimised for "who self-identifies as an X developer in Y?"

Both paths feed into a single deduplicated candidate set, with full GitHub
profiles fetched for each unique developer.

Why stars/forks don't matter: a recruiter wants developers with hands-on
experience, not OSS celebrities. A 3-star side project that uses FastAPI is
just as strong a signal as contributing to FastAPI itself — both prove the
person has shipped working code with that stack.

Payment: $0.001 per repo search + $0.001 per repo scanned for contributors.
"""

import asyncio
import structlog
from datetime import datetime, timedelta, timezone

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

# How recent is "actively developed"
_RECENCY_DAYS = 365


def _normalize_language(lang: str) -> str:
    return _QUERY_LANG_MAP.get(lang.lower(), lang.lower())


def _build_repo_query(parsed_jd: ParsedJD) -> str:
    """
    Construct a GitHub repository search query from the JD.

    Strategy (relaxed — stars don't matter, recency does):
    - Filter on primary language for precision
    - Add top 3 skill keywords as free-text (matched in description/README/topics)
    - Restrict to repos pushed in the last year (proves active development)
    - Exclude archived projects (no point sourcing from dead code)

    Example output:
        "language:python fastapi postgresql archived:false pushed:>2025-04-07"
    """
    parts: list[str] = []

    # Primary language filter
    languages = parsed_jd.languages or []
    if languages:
        parts.append(f"language:{_normalize_language(languages[0])}")

    # Top skill keywords (avoid duplicating the language)
    lang_set = {l.lower() for l in languages}
    skill_keywords = [
        s for s in (parsed_jd.skills or [])
        if s.lower() not in lang_set
    ][:3]
    parts.extend(skill_keywords)

    # Recency + non-archived — filter out dead/abandoned work
    parts.append("archived:false")
    recent_date = (datetime.now(timezone.utc) - timedelta(days=_RECENCY_DAYS)).strftime("%Y-%m-%d")
    parts.append(f"pushed:>{recent_date}")

    return " ".join(parts) if parts else f"pushed:>{recent_date}"


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


async def _discover_via_repos(
    client: GitHubClient,
    parsed_jd: ParsedJD,
    max_repos: int,
    max_contributors_per_repo: int,
) -> dict[str, list[str]]:
    """
    REPO PATH: search recently-active repos matching the stack, mine contributors.
    Returns: {github_username: [list of repo full_names they contributed to]}
    """
    query = _build_repo_query(parsed_jd)
    log.info("github_source_repo_query", query=query, max_repos=max_repos)

    try:
        repos = await client.search_repos(query, max_repos=max_repos, sort="updated")
    except Exception as exc:
        log.warning("github_source_repo_search_failed", error=str(exc))
        return {}

    if not repos:
        log.info("github_source_no_repos", query=query)
        return {}

    log.info(
        "github_source_repos_found",
        count=len(repos),
        repos=[r["full_name"] for r in repos],
    )

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

    return contributor_repos


async def _discover_via_users(
    client: GitHubClient,
    parsed_jd: ParsedJD,
    max_users: int,
) -> list[str]:
    """
    USER PATH: search GitHub users directly by primary language (and location
    if the JD specified one). Returns a list of usernames.

    Skipped if the JD has no language — without that filter, results are noise.
    """
    languages = parsed_jd.languages or []
    if not languages:
        log.debug("github_source_user_search_skipped", reason="no_language_in_jd")
        return []

    primary_language = _normalize_language(languages[0])

    # Use parsed_jd.location only if it's an actual place (not "Remote")
    location = parsed_jd.location or ""
    location_arg = location if location and location.lower() != "remote" else None

    log.info(
        "github_source_user_query",
        language=primary_language,
        location=location_arg,
        max_users=max_users,
    )

    try:
        usernames = await client.search_users(
            language=primary_language,
            location=location_arg,
            min_followers=0,   # fame doesn't matter
            min_repos=2,       # but they should have built *something*
            per_page=max_users,
            sort="repositories",
        )
    except Exception as exc:
        log.warning("github_source_user_search_failed", error=str(exc))
        return []

    log.info("github_source_users_found", count=len(usernames))
    return usernames


async def run_github_source_agent(
    parsed_jd: ParsedJD,
    max_repos: int = 5,
    max_contributors_per_repo: int = 10,
    max_users: int = 10,
    concurrency: int = 3,
) -> list[CandidateEnriched]:
    """
    Discover candidates via two parallel paths:

      1. Repo path — mine contributors of recently-active repos in the stack
      2. User path — search GitHub users by primary language (+ location)

    Both paths feed into a single deduplicated set of GitHub usernames, then
    each unique developer's full profile is fetched and converted into a
    CandidateEnriched. The caller should further deduplicate against Apollo
    results by github_username.
    """
    client = GitHubClient()
    try:
        # ── Step 1: Run repo + user discovery in parallel ──────────────────────
        contributor_repos, user_search_logins = await asyncio.gather(
            _discover_via_repos(client, parsed_jd, max_repos, max_contributors_per_repo),
            _discover_via_users(client, parsed_jd, max_users),
        )

        # ── Step 2: Merge sources, tagging which repos each candidate came from ─
        # username → list of repo full_names (or ["github_user_search"] for user-search hits)
        all_candidates: dict[str, list[str]] = dict(contributor_repos)
        for login in user_search_logins:
            if login not in all_candidates:
                all_candidates[login] = ["github_user_search"]

        if not all_candidates:
            log.info("github_source_no_candidates")
            return []

        log.info(
            "github_source_unique_candidates",
            total=len(all_candidates),
            from_repos=len(contributor_repos),
            from_user_search=len(user_search_logins),
        )

        # ── Step 3: Build full candidate profiles (rate-limited via semaphore) ─
        semaphore = asyncio.Semaphore(concurrency)
        required_languages = parsed_jd.languages or []

        async def _fetch_one(login: str, repos_list: list[str]) -> CandidateEnriched | None:
            async with semaphore:
                return await _candidate_from_github_profile(
                    login, repos_list, client, required_languages
                )

        profile_tasks = [
            _fetch_one(login, repos_list)
            for login, repos_list in all_candidates.items()
        ]
        profiles = await asyncio.gather(*profile_tasks, return_exceptions=True)

        candidates: list[CandidateEnriched] = []
        for result in profiles:
            if isinstance(result, Exception) or result is None:
                continue
            candidates.append(result)

        log.info(
            "github_source_done",
            candidates_built=len(candidates),
        )
        return candidates

    finally:
        await client.close()
