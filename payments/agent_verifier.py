"""
ERC-8004 Agent Verifier.

Before the orchestrator sends any USDC payment to a specialist agent,
it calls this verifier to check that the agent's wallet address is
registered and active in the AgentRegistry contract on Arc.

The AgentRegistry.vy contract is queried via web3.py.
"""

import structlog
from web3 import Web3

from settings import settings

log = structlog.get_logger()

# Minimal ABI — only the functions we call
AGENT_REGISTRY_ABI = [
    {
        "name": "is_verified",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "agent_address", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "register_agent",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agent_address", "type": "address"},
            {"name": "agent_name",    "type": "string"},
        ],
        "outputs": [],
    },
    {
        "name": "get_agent",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "agent_address", "type": "address"}],
        "outputs": [
            {"name": "agent_name",    "type": "string"},
            {"name": "registered_at", "type": "uint256"},
            {"name": "is_active",     "type": "bool"},
            {"name": "total_earned",  "type": "uint256"},
        ],
    },
]


class AgentVerifier:
    def __init__(self) -> None:
        self._w3 = Web3(Web3.HTTPProvider(settings.arc_rpc_url))
        self._contract = None
        if settings.agent_registry_address:
            self._contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.agent_registry_address),
                abi=AGENT_REGISTRY_ABI,
            )

    def _ready(self) -> bool:
        return self._contract is not None

    async def is_verified(self, agent_address: str) -> bool:
        """
        Returns True if the agent is registered and active in AgentRegistry.vy.
        Falls back to True if registry contract is not yet deployed (dev mode).
        """
        if not self._ready():
            log.warning("agent_registry_not_deployed", note="skipping verification — dev mode")
            return True
        try:
            checksum_addr = Web3.to_checksum_address(agent_address)
            return self._contract.functions.is_verified(checksum_addr).call()
        except Exception as exc:
            log.error("agent_verification_failed", address=agent_address, error=str(exc))
            return False

    async def register_agent(
        self,
        agent_address: str,
        agent_name: str,
        sender_private_key: str,
    ) -> str:
        """
        Register an agent on-chain in AgentRegistry.vy.
        Returns the transaction hash.
        Called once per agent at system startup.
        """
        if not self._ready():
            log.warning("agent_registry_not_deployed", note="skipping registration — dev mode")
            return "0x0"

        account = self._w3.eth.account.from_key(sender_private_key)
        checksum_addr = Web3.to_checksum_address(agent_address)

        tx = self._contract.functions.register_agent(
            checksum_addr, agent_name
        ).build_transaction({
            "from":     account.address,
            "nonce":    self._w3.eth.get_transaction_count(account.address),
            "gas":      200_000,
            "gasPrice": self._w3.eth.gas_price,
        })
        signed = self._w3.eth.account.sign_transaction(tx, private_key=sender_private_key)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        log.info("agent_registered_on_chain", agent=agent_name, tx=tx_hash.hex())
        return tx_hash.hex()

    async def get_agent_info(self, agent_address: str) -> dict | None:
        if not self._ready():
            return None
        try:
            checksum_addr = Web3.to_checksum_address(agent_address)
            result = self._contract.functions.get_agent(checksum_addr).call()
            return {
                "agent_name":    result[0],
                "registered_at": result[1],
                "is_active":     result[2],
                "total_earned":  result[3],
            }
        except Exception:
            return None
