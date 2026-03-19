"""
Agent Wallet Lifecycle Manager.

At startup, ensures every agent has a Circle Wallet on Arc testnet.
Wallet IDs are persisted to the database and readable from env vars
for subsequent runs (so we don't create duplicate wallets).
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from settings import settings
from services.circle_wallets import CircleWalletsClient
from db.models import AgentWallet

log = structlog.get_logger()

AGENT_NAMES = [
    "orchestrator",
    "jd_parser",
    "apollo_agent",
    "github_agent",
    "hunter_agent",
    "scoring_agent",
]


class WalletManager:
    """
    Manages Circle Wallet creation and lookup for all HireFlow agents.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._wallets_client = CircleWalletsClient()
        # In-memory cache: agent_name → { wallet_id, address }
        self._cache: dict[str, dict] = {}

    async def close(self) -> None:
        await self._wallets_client.close()

    async def ensure_all_wallets(self) -> dict[str, dict]:
        """
        Create wallets for all agents if they don't exist.
        Returns mapping: agent_name → { wallet_id, address }
        """
        for agent_name in AGENT_NAMES:
            await self._ensure_wallet(agent_name)
        return self._cache

    async def _ensure_wallet(self, agent_name: str) -> dict:
        """Get or create a wallet for a specific agent."""
        if agent_name in self._cache:
            return self._cache[agent_name]

        # Check database first
        result = await self._db.execute(
            select(AgentWallet).where(AgentWallet.agent_name == agent_name)
        )
        existing = result.scalar_one_or_none()

        if existing:
            self._cache[agent_name] = {
                "wallet_id": existing.circle_wallet_id,
                "address": existing.wallet_address,
            }
            log.info("wallet_loaded_from_db", agent=agent_name, wallet_id=existing.circle_wallet_id)
            return self._cache[agent_name]

        # Check env vars (set after first run)
        env_wallet_id = self._get_env_wallet_id(agent_name)
        if env_wallet_id:
            wallet = await self._wallets_client.get_wallet(env_wallet_id)
            address = wallet.get("address")
            await self._save_wallet_to_db(agent_name, env_wallet_id, address)
            self._cache[agent_name] = {"wallet_id": env_wallet_id, "address": address}
            return self._cache[agent_name]

        # Create new wallet via Circle API
        log.info("creating_wallet", agent=agent_name)
        wallet = await self._wallets_client.create_wallet(agent_name)
        wallet_id = wallet.get("id")
        address = wallet.get("address")
        await self._save_wallet_to_db(agent_name, wallet_id, address)
        self._cache[agent_name] = {"wallet_id": wallet_id, "address": address}
        log.info("wallet_created", agent=agent_name, wallet_id=wallet_id, address=address)
        return self._cache[agent_name]

    async def _save_wallet_to_db(
        self, agent_name: str, wallet_id: str, address: str | None
    ) -> None:
        record = AgentWallet(
            agent_name=agent_name,
            circle_wallet_id=wallet_id,
            wallet_address=address,
        )
        self._db.add(record)
        await self._db.commit()

    def _get_env_wallet_id(self, agent_name: str) -> str | None:
        mapping = {
            "orchestrator":   settings.orchestrator_wallet_id,
            "jd_parser":      settings.jd_parser_wallet_id,
            "apollo_agent":   settings.apollo_agent_wallet_id,
            "github_agent":   settings.github_agent_wallet_id,
            "hunter_agent":   settings.hunter_agent_wallet_id,
            "scoring_agent":  settings.scoring_agent_wallet_id,
        }
        return mapping.get(agent_name) or None

    def get_wallet_id(self, agent_name: str) -> str:
        return self._cache.get(agent_name, {}).get("wallet_id", "")

    def get_address(self, agent_name: str) -> str:
        return self._cache.get(agent_name, {}).get("address", "")

    def get_private_key(self, agent_name: str) -> str:
        mapping = {
            "orchestrator":  settings.orchestrator_private_key,
            "jd_parser":     settings.jd_parser_private_key,
            "apollo_agent":  settings.apollo_private_key,
            "github_agent":  settings.github_private_key,
            "hunter_agent":  settings.hunter_private_key,
            "scoring_agent": settings.scoring_private_key,
        }
        return mapping.get(agent_name, "0x")


class LocalWalletManager:
    """
    Lightweight wallet manager that derives addresses directly from
    private keys in .env — no Circle API calls needed.
    Used by the pipeline when Circle Programmable Wallets are not set up.
    """

    def __init__(self) -> None:
        from eth_account import Account
        self._keys = {
            "orchestrator":  settings.orchestrator_private_key,
            "jd_parser":     settings.jd_parser_private_key,
            "apollo_agent":  settings.apollo_private_key,
            "github_agent":  settings.github_private_key,
            "hunter_agent":  settings.hunter_private_key,
            "scoring_agent": settings.scoring_private_key,
        }
        self._addresses = {}
        for name, key in self._keys.items():
            if key and key != "0x" and len(key) >= 64:
                try:
                    self._addresses[name] = Account.from_key(key).address
                except Exception:
                    pass

    async def close(self) -> None:
        pass

    def get_wallet_id(self, agent_name: str) -> str:
        return self._addresses.get(agent_name, "")

    def get_address(self, agent_name: str) -> str:
        return self._addresses.get(agent_name, "")

    def get_private_key(self, agent_name: str) -> str:
        return self._keys.get(agent_name, "0x")
