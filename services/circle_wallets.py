"""
Circle Developer-Controlled Wallets API client.
Docs: https://developers.circle.com/w3s/reference/createwallet

Flow:
  1. Create a wallet set (one-time, per environment)
  2. Create wallets inside the wallet set (one per agent)
  3. Query balances via wallet ID

Endpoints:
  POST /v1/w3s/developer/walletSets        — create wallet set
  POST /v1/w3s/developer/wallets           — create wallets
  GET  /v1/w3s/wallets/{id}/balances       — get USDC balance
  GET  /v1/w3s/wallets/{id}               — get wallet details
"""

import httpx
import uuid
from tenacity import retry, stop_after_attempt, wait_exponential

from settings import settings


class CircleWalletsClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.circle_base_url,
            headers={
                "Authorization": f"Bearer {settings.circle_api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._wallet_set_id: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def create_wallet_set(self) -> str:
        """
        Create a wallet set — the container for all HireFlow agent wallets.
        Returns the wallet set ID.
        """
        payload = {
            "idempotencyKey": "hireflow-wallet-set-v1",
            "name": "HireFlow Agents",
        }
        resp = await self._client.post("/v1/w3s/developer/walletSets", json=payload)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("walletSet", {})
        return data.get("id", "")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def create_wallet(self, agent_name: str) -> dict:
        """
        Create a new Circle developer-controlled wallet.
        Returns wallet dict with id, address, blockchain.
        """
        # Ensure we have a wallet set
        if not self._wallet_set_id:
            self._wallet_set_id = await self._get_or_create_wallet_set()

        payload = {
            "idempotencyKey": f"hireflow-{agent_name}-{uuid.uuid4()}",
            "blockchains": ["MATIC-AMOY"],   # Arc not available yet — use Polygon Amoy testnet
            "count": 1,
            "walletSetId": self._wallet_set_id,
            "metadata": [{"name": agent_name, "refId": agent_name}],
        }
        resp = await self._client.post("/v1/w3s/developer/wallets", json=payload)
        resp.raise_for_status()
        wallets = resp.json().get("data", {}).get("wallets", [])
        return wallets[0] if wallets else {}

    async def _get_or_create_wallet_set(self) -> str:
        """Get existing wallet set or create a new one."""
        # Try to list existing wallet sets first
        try:
            resp = await self._client.get("/v1/w3s/developer/walletSets")
            if resp.status_code == 200:
                sets = resp.json().get("data", {}).get("walletSets", [])
                if sets:
                    return sets[0]["id"]
        except Exception:
            pass
        return await self.create_wallet_set()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_balance(self, wallet_id: str) -> float:
        """Return USDC balance for a wallet as a float."""
        resp = await self._client.get(f"/v1/w3s/wallets/{wallet_id}/balances")
        resp.raise_for_status()
        token_balances = resp.json().get("data", {}).get("tokenBalances", [])
        for tb in token_balances:
            if tb.get("token", {}).get("symbol") == "USDC":
                return float(tb.get("amount", "0"))
        return 0.0

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def get_wallet(self, wallet_id: str) -> dict:
        """Fetch wallet details (address, blockchain, status)."""
        resp = await self._client.get(f"/v1/w3s/wallets/{wallet_id}")
        resp.raise_for_status()
        return resp.json().get("data", {}).get("wallet", {})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def list_wallets(self) -> list[dict]:
        """List all developer-controlled wallets."""
        resp = await self._client.get("/v1/w3s/wallets")
        resp.raise_for_status()
        return resp.json().get("data", {}).get("wallets", [])

    async def get_all_balances(self, wallet_ids: dict[str, str]) -> dict[str, float]:
        """
        Fetch USDC balances for all agent wallets.
        wallet_ids: { agent_name → wallet_id }
        Returns: { agent_name → usdc_balance }
        """
        import asyncio
        tasks = {name: self.get_balance(wid) for name, wid in wallet_ids.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            name: (result if isinstance(result, float) else 0.0)
            for name, result in zip(tasks.keys(), results)
        }
