"""
Circle Bridge Kit — USDC cross-chain bridging to Arc.

Bridges USDC from Base / Ethereum / Polygon → Arc testnet
using Circle's cross-chain transfer API.

Docs: https://developers.circle.com/w3s/docs/cross-chain-transfer
"""

import httpx
import uuid
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings


# Supported source chains
SUPPORTED_SOURCES = {"BASE", "ETH", "POLYGON", "ARB"}


class CircleBridgeClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.circle_base_url,
            headers={
                "Authorization": f"Bearer {settings.circle_api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    async def bridge_to_arc(
        self,
        source_chain: str,
        amount_usdc: float,
        from_wallet_id: str,
        to_wallet_id: str,
    ) -> dict:
        """
        Initiate a cross-chain USDC transfer from source_chain → Arc testnet.

        Args:
            source_chain: "BASE" | "ETH" | "POLYGON" | "ARB"
            amount_usdc:  Amount in USDC (e.g. 1.0 for $1.00)
            from_wallet_id: Source Circle wallet ID
            to_wallet_id:   Destination Circle wallet ID on Arc

        Returns:
            dict with transfer_id and estimated completion time
        """
        if source_chain not in SUPPORTED_SOURCES:
            raise ValueError(f"Unsupported source chain: {source_chain}. Use one of {SUPPORTED_SOURCES}")

        idempotency_key = str(uuid.uuid4())
        payload = {
            "idempotencyKey": idempotency_key,
            "source": {
                "type": "wallet",
                "id": from_wallet_id,
            },
            "destination": {
                "type": "wallet",
                "id": to_wallet_id,
                "chain": "ARC-TESTNET",
            },
            "amount": {
                "amount": f"{amount_usdc:.6f}",
                "currency": "USDC",
            },
        }
        resp = await self._client.post("/v1/transfers", json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=10))
    async def get_transfer_status(self, transfer_id: str) -> dict:
        """Poll transfer status until complete or failed."""
        resp = await self._client.get(f"/v1/transfers/{transfer_id}")
        resp.raise_for_status()
        return resp.json().get("data", {})
