"""
Arc Block Explorer — transaction link generation and lookup.

Used to produce clickable Arc Block Explorer links for every
payment transaction so judges can verify on-chain payments.
"""

from settings import settings


# Arc testnet block explorer base URL
ARC_EXPLORER_BASE = "https://explorer.arc.network"


def tx_link(tx_hash: str) -> str:
    """Return a clickable Arc Block Explorer URL for a transaction hash."""
    return f"{ARC_EXPLORER_BASE}/tx/{tx_hash}"


def address_link(address: str) -> str:
    """Return a clickable Arc Block Explorer URL for an address."""
    return f"{ARC_EXPLORER_BASE}/address/{address}"


def format_payment_event(
    tx_hash: str,
    action_type: str,
    amount_usdc: float,
    paying_agent: str,
    receiving_agent: str,
) -> dict:
    """
    Format a payment event for the WebSocket live feed and frontend display.
    """
    return {
        "tx_hash":        tx_hash,
        "explorer_url":   tx_link(tx_hash),
        "action_type":    action_type,
        "amount_usdc":    amount_usdc,
        "paying_agent":   paying_agent,
        "receiving_agent": receiving_agent,
    }
