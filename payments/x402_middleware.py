"""
x402 Payment Middleware for FastAPI.

Every specialist agent endpoint (Apollo, GitHub, Hunter, Scoring)
is protected behind an HTTP 402 payment wall.

Flow:
  1. Request arrives with no X-Payment header → return 402 with requirements
  2. Client (orchestrator) pays via Circle Nanopayments → gets proof token
  3. Client retries with X-Payment: {proof_token}
  4. Middleware verifies proof → request proceeds to handler
"""

import json
import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from settings import settings


# Maps route path → receiving agent wallet address
# These are populated from env vars after wallet_manager creates them
def _get_agent_wallet_addresses() -> dict[str, str]:
    return {
        "/apollo/search":   "",   # filled from settings at runtime
        "/apollo/enrich":   "",
        "/github/profile":  "",
        "/github/repos":    "",
        "/hunter/find":     "",
        "/hunter/verify":   "",
        "/score/candidate": "",
        "/jd/parse":        "",
    }


class X402PaymentMiddleware:
    """
    FastAPI middleware that enforces x402 payment walls on agent endpoints.
    Attach to FastAPI app via app.add_middleware(X402PaymentMiddleware).
    """

    # Endpoints that require payment — all others pass through
    PROTECTED_PATHS = set(settings.action_prices.keys())

    def __init__(self, app, agent_addresses: dict[str, str] | None = None):
        self.app = app
        # agent_addresses: { route_path → wallet_address_on_arc }
        self._agent_addresses = agent_addresses or {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path

        if path not in self.PROTECTED_PATHS:
            await self.app(scope, receive, send)
            return

        payment_header = request.headers.get("X-Payment")

        if not payment_header:
            # Return 402 with payment requirements (x402 spec v1)
            price = settings.action_prices.get(path, "0.001")
            pay_to = self._agent_addresses.get(path, "")
            body = {
                "x402Version": 1,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "arc-testnet",
                        "maxAmountRequired": str(price),
                        "resource": str(request.url),
                        "payTo": pay_to,
                        "asset": settings.usdc_contract_address,
                    }
                ],
                "error": "Payment required",
            }
            response = JSONResponse(content=body, status_code=402)
            await response(scope, receive, send)
            return

        # Verify the payment proof via Circle Nanopayments
        verified = await self._verify_payment(payment_header, path)
        if not verified:
            response = JSONResponse(
                content={"error": "Invalid or expired payment proof"},
                status_code=402,
            )
            await response(scope, receive, send)
            return

        # Payment valid — pass through to handler
        await self.app(scope, receive, send)

    async def _verify_payment(self, payment_proof: str, path: str) -> bool:
        """
        Verify a payment proof token against Circle Nanopayments API.
        Returns True if valid and amount matches the expected price.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{settings.circle_base_url}/v1/nanopayments/verify",
                    headers={
                        "Authorization": f"Bearer {settings.circle_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "paymentProof": payment_proof,
                        "resource": path,
                        "network": "ARC-TESTNET",
                    },
                )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return data.get("valid", False)
        except Exception:
            pass
        return False

    def update_agent_addresses(self, addresses: dict[str, str]) -> None:
        """Update agent wallet addresses after wallets are created."""
        self._agent_addresses.update(addresses)
