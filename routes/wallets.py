"""
Wallet routes — agent wallet balances and recruiter onboarding.

GET  /api/wallets/balances     — all agent USDC balances
POST /api/bridge               — bridge USDC from Base/ETH → Arc
GET  /api/bridge/{transfer_id} — check bridge transfer status
"""

import structlog

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import verify_api_key
from db.database import get_db
from services.circle_wallets import CircleWalletsClient
from services.circle_bridge import CircleBridgeClient
from services.circle_gateway import CircleGatewayClient
from models.payment import WalletBalance
from settings import settings

log = structlog.get_logger()
router = APIRouter(tags=["wallets"])


class BridgeRequest:
    def __init__(self, source_chain: str, amount_usdc: float, from_wallet_id: str, to_wallet_id: str):
        self.source_chain = source_chain
        self.amount_usdc = amount_usdc
        self.from_wallet_id = from_wallet_id
        self.to_wallet_id = to_wallet_id


from pydantic import BaseModel

class BridgeRequestBody(BaseModel):
    source_chain: str            # "BASE" | "ETH" | "POLYGON"
    amount_usdc: float
    from_wallet_id: str
    to_wallet_id: str


@router.get("/api/wallets/balances", response_model=list[WalletBalance])
async def get_all_balances(db: AsyncSession = Depends(get_db), _: str = Depends(verify_api_key)):
    """Return USDC balances for all HireFlow agent wallets."""
    from db.models import AgentWallet
    from sqlalchemy import select

    result = await db.execute(select(AgentWallet))
    wallets = result.scalars().all()

    if not wallets:
        return []

    client = CircleWalletsClient()
    try:
        balances = []
        for wallet in wallets:
            try:
                usdc = await client.get_balance(wallet.circle_wallet_id)
            except Exception:
                usdc = 0.0
            balances.append(
                WalletBalance(
                    agent_name=wallet.agent_name,
                    circle_wallet_id=wallet.circle_wallet_id,
                    usdc_balance=usdc,
                    wallet_address=wallet.wallet_address,
                )
            )
        return balances
    finally:
        await client.close()


@router.post("/api/bridge")
async def bridge_usdc_to_arc(body: BridgeRequestBody, _: str = Depends(verify_api_key)):
    """
    Bridge USDC from Base/Ethereum/Polygon → Arc testnet.
    Called during recruiter onboarding to fund their search wallet.
    Returns a transfer_id to poll for completion.
    """
    client = CircleBridgeClient()
    try:
        result = await client.bridge_to_arc(
            source_chain=body.source_chain.upper(),
            amount_usdc=body.amount_usdc,
            from_wallet_id=body.from_wallet_id,
            to_wallet_id=body.to_wallet_id,
        )
        log.info("bridge_initiated", source=body.source_chain, amount=body.amount_usdc)
        return {"transfer_id": result.get("id"), "status": result.get("status"), "data": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await client.close()


@router.get("/api/bridge/{transfer_id}")
async def get_bridge_status(transfer_id: str, _: str = Depends(verify_api_key)):
    """Poll the status of a cross-chain USDC transfer."""
    client = CircleBridgeClient()
    try:
        result = await client.get_transfer_status(transfer_id)
        return {"transfer_id": transfer_id, "status": result.get("status"), "data": result}
    finally:
        await client.close()


@router.get("/api/wallets/{wallet_id}/balance")
async def get_wallet_balance(wallet_id: str, _: str = Depends(verify_api_key)):
    """Get unified cross-chain USDC balance for a specific wallet via Circle Gateway."""
    gateway = CircleGatewayClient()
    try:
        balance = await gateway.get_usdc_balance(wallet_id)
        return {"wallet_id": wallet_id, "usdc_balance": balance}
    finally:
        await gateway.close()
