"""
Circle Nanopayments — EIP-3009 signed transfer implementation.

Flow:
  1. Orchestrator signs an EIP-3009 transferWithAuthorization message
  2. Signed payload is sent to Circle Nanopayments API
  3. Circle validates signature, adjusts internal ledger instantly (off-chain)
  4. Batch settlement to Arc L1 happens periodically
  5. Returns payment proof token used as X-Payment header in x402 flow
"""

import os
import secrets
import time
import httpx

from eth_account import Account
from eth_account.messages import encode_typed_data

from settings import settings
from models.payment import EIP3009Authorization


# USDC has 6 decimals — $0.001 = 1000 base units
USDC_DECIMALS = 6


def usdc_to_base_units(amount_usdc: float) -> int:
    """Convert human-readable USDC to 6-decimal base units."""
    return int(amount_usdc * 10**USDC_DECIMALS)


def base_units_to_usdc(base_units: int) -> float:
    """Convert 6-decimal base units back to human-readable USDC."""
    return base_units / 10**USDC_DECIMALS


def sign_eip3009_transfer(
    from_address: str,
    to_address: str,
    amount_usdc: float,
    private_key: str,
    valid_seconds: int = 300,
) -> EIP3009Authorization:
    """
    Sign an EIP-3009 transferWithAuthorization message.
    This authorizes Circle Nanopayments to debit from_address and credit to_address.
    """
    value = usdc_to_base_units(amount_usdc)
    valid_after = int(time.time()) - 10   # slight back-dating for clock skew
    valid_before = int(time.time()) + valid_seconds
    nonce = "0x" + secrets.token_hex(32)

    domain_data = {
        "name": "USD Coin",
        "version": "2",
        "chainId": settings.arc_chain_id,
        "verifyingContract": settings.usdc_contract_address,
    }

    message_types = {
        "TransferWithAuthorization": [
            {"name": "from",        "type": "address"},
            {"name": "to",          "type": "address"},
            {"name": "value",       "type": "uint256"},
            {"name": "validAfter",  "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce",       "type": "bytes32"},
        ]
    }

    message_data = {
        "from":        from_address,
        "to":          to_address,
        "value":       value,
        "validAfter":  valid_after,
        "validBefore": valid_before,
        "nonce":       bytes.fromhex(nonce[2:]),  # bytes32
    }

    structured_data = {
        "domain":      domain_data,
        "types":       message_types,
        "primaryType": "TransferWithAuthorization",
        "message":     message_data,
    }

    signed = Account.sign_typed_data(private_key, full_message=structured_data)

    return EIP3009Authorization(
        from_address=from_address,
        to_address=to_address,
        value=value,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=nonce,
        v=signed.v,
        r=hex(signed.r),
        s=hex(signed.s),
    )


class NanopaymentsClient:
    """
    Client for submitting EIP-3009 signed transfers to Circle Nanopayments API.
    Returns a payment proof token used as the X-Payment header in x402 retries.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.circle_base_url,
            headers={
                "Authorization": f"Bearer {settings.circle_api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def transfer(
        self,
        auth: EIP3009Authorization,
    ) -> dict:
        """
        Submit a signed EIP-3009 authorization to Circle Nanopayments.
        Returns the API response containing the payment proof token.
        """
        payload = {
            "network":        "ARC-TESTNET",
            "usdcContract":   settings.usdc_contract_address,
            "from":           auth.from_address,
            "to":             auth.to_address,
            "value":          str(auth.value),
            "validAfter":     str(auth.valid_after),
            "validBefore":    str(auth.valid_before),
            "nonce":          auth.nonce,
            "signature": {
                "v": auth.v,
                "r": auth.r,
                "s": auth.s,
            },
        }
        resp = await self._client.post("/v1/nanopayments/transfer", json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def pay(
        self,
        from_address: str,
        to_address: str,
        amount_usdc: float,
        private_key: str,
    ) -> str:
        """
        Convenience: sign + submit in one call.
        Returns the payment proof token string.
        """
        auth = sign_eip3009_transfer(from_address, to_address, amount_usdc, private_key)
        result = await self.transfer(auth)
        return result.get("paymentProof", result.get("id", ""))
