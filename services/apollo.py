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
        )
