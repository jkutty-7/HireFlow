from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid


class PaymentEvent(BaseModel):
    """Single payment event broadcast over WebSocket."""
    search_id: str
    action_type: str
    paying_agent: str
    receiving_agent: str
    amount_usdc: float
    arc_tx_hash: Optional[str] = None
    status: str = "pending"  # pending | confirmed | failed
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class WalletBalance(BaseModel):
    agent_name: str
    circle_wallet_id: str
    usdc_balance: float
    wallet_address: Optional[str] = None


class TransactionLog(BaseModel):
    id: uuid.UUID
    search_id: uuid.UUID
    action_type: str
    paying_agent: str
    receiving_agent: str
    amount_usdc: float
    arc_tx_hash: Optional[str] = None
    status: str
    created_at: datetime


class EIP3009Authorization(BaseModel):
    """Signed EIP-3009 transferWithAuthorization payload."""
    from_address: str
    to_address: str
    value: int        # USDC in base units (6 decimals), e.g. 1000 = $0.001000
    valid_after: int  # unix timestamp
    valid_before: int
    nonce: str        # hex-encoded 32-byte nonce
    v: int
    r: str
    s: str
