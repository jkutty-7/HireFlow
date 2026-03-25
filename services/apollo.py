"""
Apollo.io REST client.

Endpoints used:
  POST /api/v1/mixed_people/api_search  — free, no credits, discovery only
  POST /api/v1/people/match             — single enrichment (credits)
  POST /api/v1/people/bulk_match        — batch enrichment (credits)
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings
from models.candidate import CandidateRaw, CandidateEnriched


class ApolloClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.apollo_base_url,
            headers={"X-Api-Key": settings.apollo_api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search_people(
        self,
        titles: list[str],
        locations: list[str],
        seniorities: list[str],
        keywords: str,
        per_page: int = 25,
        page: int = 1,
    ) -> list[CandidateRaw]:
        """
        Free search — returns basic profile info, no email.
        Endpoint: POST /api/v1/mixed_people/api_search
        """
        payload = {
            "person_titles": titles,
            "person_seniorities": seniorities,
            "q_keywords": keywords,
            "per_page": per_page,
            "page": page,
        }
        # Only add location filter if not a remote role (Apollo ignores "Remote")
        if locations:
            payload["person_locations"] = locations
        resp = await self._client.post("/api/v1/mixed_people/api_search", json=payload)
        resp.raise_for_status()
        data = resp.json()

        import structlog as _log
        _log.get_logger().debug(
            "apollo_raw_response",
            keys=list(data.keys()),
            total_count=data.get("pagination", {}).get("total_entries", "?"),
            people_count=len(data.get("people", [])),
            contacts_count=len(data.get("contacts", [])),
        )

        # Apollo returns people under "people" or sometimes "contacts"
        people = data.get("people") or data.get("contacts") or []
        candidates = []
        for person in people:
            candidates.append(
                CandidateRaw(
                    apollo_id=person.get("id"),
                    name=person.get("name", ""),
                    title=person.get("title"),
                    company=person.get("organization_name"),
                    linkedin_url=person.get("linkedin_url"),
                    location=person.get("city") or person.get("country"),
                    github_url=None,
                )
            )
        return candidates

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def enrich_person(
        self,
        name: str,
        organization_name: str,
        reveal_emails: bool = True,
    ) -> CandidateEnriched | None:
        """
        Single enrichment — credits consumed.
        Endpoint: POST /api/v1/people/match
        """
        payload = {
            "name": name,
            "organization_name": organization_name,
            "reveal_personal_emails": reveal_emails,
            "reveal_phone_number": False,
        }
        resp = await self._client.post("/api/v1/people/match", json=payload)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        person = resp.json().get("person", {})
        return self._parse_enriched(person)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def bulk_enrich(self, apollo_ids: list[str]) -> list[CandidateEnriched]:
        """
        Batch enrichment up to 10 candidates per call — credits consumed.
        Endpoint: POST /api/v1/people/bulk_match
        """
        payload = {
            "details": [{"id": aid} for aid in apollo_ids[:10]],
            "reveal_personal_emails": True,
        }
        resp = await self._client.post("/api/v1/people/bulk_match", json=payload)
        resp.raise_for_status()
        return [self._parse_enriched(p) for p in resp.json().get("matches", [])]

    def _parse_enriched(self, person: dict) -> CandidateEnriched:
        email_data = person.get("email", None)
        github_url = None
        for account in person.get("account", {}).get("organization", {}).get("accounts", []):
            if "github" in account.get("url", "").lower():
                github_url = account["url"]

        # Also check top-level github_url field if present
        if not github_url:
            github_url = person.get("github_url")

        # Extract actual company domain from Apollo org data (Bug 4 fix)
        org = person.get("organization") or {}
        organization_domain = org.get("primary_domain") or org.get("website_url") or None

        skills = [s.get("name", "") for s in person.get("skills", []) if s.get("name")]
        employment = [
            {
                "title": exp.get("title"),
                "company": exp.get("organization_name"),
                "start": exp.get("start_date"),
                "end": exp.get("end_date"),
            }
            for exp in person.get("employment_history", [])
        ]

        # Derive LinkedIn tenure/trajectory signals from employment history
        tenure_signals = self._analyze_employment(employment)

        return CandidateEnriched(
            apollo_id=person.get("id"),
            name=person.get("name", ""),
            title=person.get("title"),
            company=person.get("organization_name"),
            linkedin_url=person.get("linkedin_url"),
            location=person.get("city") or person.get("country"),
            github_url=github_url,
            email=email_data if isinstance(email_data, str) else None,
            email_confidence=None,
            email_status=None,
            skills=skills,
            employment_history=employment,
            github_username=None,
            organization_domain=organization_domain,
            avg_tenure_months=tenure_signals["avg_tenure_months"],
            is_job_hopper=tenure_signals["is_job_hopper"],
            career_trajectory=tenure_signals["career_trajectory"],
        )

    @staticmethod
    def _analyze_employment(history: list[dict]) -> dict:
        """Derive tenure and career trajectory signals from employment history."""
        from datetime import datetime

        if not history:
            return {"avg_tenure_months": None, "is_job_hopper": False, "career_trajectory": "unknown"}

        tenures = []
        for job in history:
            start_str = job.get("start")
            end_str = job.get("end")
            if not start_str:
                continue
            try:
                # Apollo dates can be "YYYY-MM-DD" or "YYYY-MM" or "YYYY"
                def _parse_date(s: str) -> datetime:
                    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
                        try:
                            return datetime.strptime(s, fmt)
                        except ValueError:
                            continue
                    raise ValueError(f"Unknown date format: {s}")

                start = _parse_date(start_str)
                end = _parse_date(end_str) if end_str and end_str.lower() != "present" else datetime.now()
                months = (end.year - start.year) * 12 + (end.month - start.month)
                if months > 0:
                    tenures.append(months)
            except Exception:
                continue

        if not tenures:
            return {"avg_tenure_months": None, "is_job_hopper": False, "career_trajectory": "unknown"}

        avg = round(sum(tenures) / len(tenures), 1)
        is_job_hopper = avg < 12 and len(history) >= 3

        # Infer trajectory: look for seniority keywords in titles over time
        seniority_keywords = {
            "staff": 5, "principal": 5, "distinguished": 5,
            "lead": 4, "architect": 4,
            "senior": 3, "sr": 3,
            "mid": 2, "ii": 2, "2": 2,
            "junior": 1, "jr": 1, "associate": 1,
        }
        titles_with_level = []
        for job in history:
            title = (job.get("title") or "").lower()
            for kw, level in seniority_keywords.items():
                if kw in title.split():
                    titles_with_level.append(level)
                    break

        trajectory = "unknown"
        if len(titles_with_level) >= 2:
            if titles_with_level[0] > titles_with_level[-1]:
                trajectory = "ascending"
            elif titles_with_level[0] == titles_with_level[-1]:
                trajectory = "lateral"
            else:
                trajectory = "descending"

        return {"avg_tenure_months": avg, "is_job_hopper": is_job_hopper, "career_trajectory": trajectory}
