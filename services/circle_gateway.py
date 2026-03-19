"""
Circle Gateway — unified cross-chain USDC balance view.

Used in the frontend to show real-time wallet balances after
Bridge Kit deposits arrive on Arc.

Docs: https://developers.circle.com/w3s/docs/circle-gateway
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings


class CircleGatewayClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.circle_base_url,
            headers={
                "Authorization": f"Bearer {settings.circle_api_key}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_unified_balance(self, wallet_id: str) -> dict:
        """
        Returns the unified USDC balance accessible cross-chain for a wallet.
        Endpoint: GET /v1/gateway/balances?walletId={id}
        """
        resp = await self._client.get(
            "/v1/gateway/balances",
            params={"walletId": wallet_id},
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def get_usdc_balance(self, wallet_id: str) -> float:
        """Convenience wrapper — returns just the USDC float amount."""
        data = await self.get_unified_balance(wallet_id)
        for balance in data.get("balances", []):
            if balance.get("currency") == "USDC":
                return float(balance.get("amount", "0"))
        return 0.0
