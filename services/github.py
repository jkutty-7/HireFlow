"""
GitHub REST API v3 client.

Endpoints used:
  GET /search/users                       — find developers by language/location
  GET /users/{username}                   — full public profile
  GET /users/{username}/repos             — top 10 recent repos
  GET /repos/{owner}/{repo}/languages     — byte-level language breakdown
  GET /users/{username}/events/public     — last 30 events for activity score
"""

import httpx
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings
from models.candidate import GitHubProfile, GitHubRepo


# Scoring constants
MAX_STARS_SCORE = 50      # max points for repo stars
MAX_LANGUAGE_SCORE = 30   # max points for language match
MAX_ACTIVITY_SCORE = 20   # max points for recent activity


class GitHubClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.github_base_url,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search_users(
        self,
        language: str,
        location: str,
        min_followers: int = 10,
        per_page: int = 30,
    ) -> list[str]:
        """Return list of GitHub usernames matching criteria."""
        query = f"language:{language} location:{location} followers:>{min_followers}"
        resp = await self._client.get(
            "/search/users",
            params={"q": query, "per_page": per_page, "sort": "followers"},
        )
        resp.raise_for_status()
        return [item["login"] for item in resp.json().get("items", [])]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_user_profile(self, username: str) -> dict:
        """Fetch full public profile for a developer."""
        resp = await self._client.get(f"/users/{username}")
        resp.raise_for_status()
        return resp.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_user_repos(self, username: str, limit: int = 10) -> list[GitHubRepo]:
        """Get the most recently active public repos."""
        resp = await self._client.get(
            f"/users/{username}/repos",
            params={"sort": "pushed", "per_page": limit, "type": "owner"},
        )
        resp.raise_for_status()
        repos = []
        for r in resp.json():
            repos.append(
                GitHubRepo(
                    name=r["name"],
                    description=r.get("description"),
                    language=r.get("language"),
                    stars=r.get("stargazers_count", 0),
                    forks=r.get("forks_count", 0),
                    pushed_at=r.get("pushed_at"),
                    topics=r.get("topics", []),
                )
            )
        return repos

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_repo_languages(self, owner: str, repo: str) -> dict[str, int]:
        """Get byte-level language breakdown for a single repo."""
        resp = await self._client.get(f"/repos/{owner}/{repo}/languages")
        resp.raise_for_status()
        return resp.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_user_events(self, username: str, per_page: int = 30) -> list[dict]:
        """Get last N public events for activity recency scoring."""
        resp = await self._client.get(
            f"/users/{username}/events/public",
            params={"per_page": per_page},
        )
        resp.raise_for_status()
        return resp.json()

    async def build_github_profile(
        self,
        username: str,
        required_languages: list[str],
    ) -> GitHubProfile:
        """
        Fetch profile + repos + events and compute github_score (0-100).

        Scoring formula (from design doc):
          repo_stars_score  = min(sum(stars across top 10 repos) / 10, 50)
          language_match    = (matching_languages / required_languages) * 30
          activity_score    = min(events_last_30_days * 2, 20)
          github_total      = sum of above
        """
        profile_data = await self.get_user_profile(username)
        repos = await self.get_user_repos(username)
        events = await self.get_user_events(username)

        # Aggregate languages across all repos
        all_languages: dict[str, int] = {}
        for repo in repos:
            try:
                langs = await self.get_repo_languages(username, repo.name)
                for lang, bytes_count in langs.items():
                    all_languages[lang] = all_languages.get(lang, 0) + bytes_count
            except Exception:
                pass

        # Stars score
        total_stars = sum(r.stars for r in repos)
        stars_score = min(total_stars / 10, MAX_STARS_SCORE)

        # Language match score
        required_lower = {l.lower() for l in required_languages}
        matched = sum(1 for l in all_languages if l.lower() in required_lower)
        lang_score = (matched / max(len(required_languages), 1)) * MAX_LANGUAGE_SCORE

        # Activity score (events in last 30 days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        scored_event_types = {"PushEvent", "PullRequestEvent", "IssuesEvent", "CreateEvent"}
        recent_count = sum(
            1 for e in events
            if e.get("type") in scored_event_types
            and datetime.fromisoformat(
                e["created_at"].replace("Z", "+00:00")
            ) > cutoff
        )
        activity_score = min(recent_count * 2, MAX_ACTIVITY_SCORE)

        github_score = round(stars_score + lang_score + activity_score, 2)

        return GitHubProfile(
            username=username,
            name=profile_data.get("name"),
            bio=profile_data.get("bio"),
            company=profile_data.get("company"),
            location=profile_data.get("location"),
            public_repos=profile_data.get("public_repos", 0),
            followers=profile_data.get("followers", 0),
            top_repos=repos,
            top_languages=all_languages,
            recent_event_count=recent_count,
            github_score=github_score,
        )
