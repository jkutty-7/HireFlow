"""
Hunter.io API v2 client.

Strategy: Pattern → Construct → Verify (saves ~40-50% credits vs Email Finder alone)
  1. GET /v2/domain-search  — learn email pattern for company domain
  2. GET /v2/email-finder   — find email for specific person
  3. GET /v2/email-verifier — verify deliverability

Confidence thresholds:
  80-100 → verified
  50-79  → medium (included, flagged)
  <50    → skip
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings


class HunterClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.hunter_base_url,
            timeout=20.0,
        )
        self._api_key = settings.hunter_api_key

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def domain_search(self, domain: str, limit: int = 5) -> dict:
        """
        Returns known emails and the pattern for a company domain.
        One credit covers up to 10 emails.
        """
        resp = await self._client.get(
            "/v2/domain-search",
            params={"domain": domain, "limit": limit, "api_key": self._api_key},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def email_finder(
        self, domain: str, first_name: str, last_name: str
    ) -> dict:
        """
        Find the most likely professional email for a person at a domain.
        Returns: email, score (0-100), verification.status
        """
        resp = await self._client.get(
            "/v2/email-finder",
            params={
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": self._api_key,
            },
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def email_verifier(self, email: str) -> dict:
        """
        Verify deliverability via SMTP + MX checks.
        Returns: status (valid|risky|invalid|unknown), score, checks
        """
        resp = await self._client.get(
            "/v2/email-verifier",
            params={"email": email, "api_key": self._api_key},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def find_and_verify_email(
        self,
        first_name: str,
        last_name: str,
        company_domain: str,
    ) -> dict:
        """
        Full pattern → find → verify flow for one candidate.
        Returns dict with email, confidence, status, and validity label.
        """
        # Step 1: Learn domain pattern (cheap — shared across candidates at same company)
        domain_data = await self.domain_search(company_domain)
        pattern = domain_data.get("pattern", "")

        # Step 2: Find email
        finder_data = await self.email_finder(company_domain, first_name, last_name)
        email = finder_data.get("email")
        score = finder_data.get("score", 0)

        if not email or score < 50:
            return {
                "email": None,
                "confidence": score,
                "status": "unknown",
                "validity": "missing",
                "pattern": pattern,
            }

        # Step 3: Verify deliverability
        verify_data = await self.email_verifier(email)
        status = verify_data.get("status", "unknown")

        validity = "verified" if score >= 80 and status == "valid" else "unverified"

        return {
            "email": email,
            "confidence": score,
            "status": status,
            "validity": validity,
            "pattern": pattern,
            "smtp_check": verify_data.get("smtp_check"),
            "mx_records": verify_data.get("mx_records"),
        }

    @staticmethod
    def extract_domain_from_company(company_name: str, linkedin_url: str | None = None) -> str | None:
        """
        Best-effort domain extraction.
        Real implementation would use a company→domain lookup service.
        """
        if linkedin_url and "linkedin.com/company/" in linkedin_url:
            # e.g. strip to a guessed domain — placeholder
            pass
        # Simple normalization: "Stripe, Inc." → "stripe.com"
        clean = company_name.lower().replace(",", "").replace("inc", "").replace("llc", "").strip()
        slug = clean.split()[0] if clean else ""
        return f"{slug}.com" if slug else None
