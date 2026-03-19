# @version ^0.4.0
# @title PaymentRouter — Agent Payment Routing with Price Cap Enforcement
# @notice Routes USDC from PaymentEscrow to specialist agents after each
#         completed action. Enforces per-action price caps so no agent
#         can overcharge even if compromised. Emits an on-chain event
#         per payment — visible on Arc Block Explorer.
# @dev Called by the FastAPI backend orchestrator after x402 verification.

# ─── Interfaces ──────────────────────────────────────────────────────────────

interface IAgentRegistry:
    def is_verified(agent_address: address) -> bool: view

interface IPaymentEscrow:
    def credit_agent(search_id: bytes32, agent_address: address, amount: uint256): nonpayable
    def finalize_search(search_id: bytes32): nonpayable

# ─── Events ──────────────────────────────────────────────────────────────────

event PaymentRouted:
    search_id:     indexed(bytes32)
    agent:         indexed(address)
    action_type:   String[32]
    amount:        uint256
    timestamp:     uint256

event PriceCapSet:
    action_type: String[32]
    max_price:   uint256

# ─── Storage ─────────────────────────────────────────────────────────────────

owner:            public(address)
registry_address: public(address)
escrow_address:   public(address)

# action_type string → max allowed USDC (6 decimals)
# e.g. "apollo_search" → 1000 (= $0.001000)
action_prices: public(HashMap[String[32], uint256])

# Total payments routed per search (for analytics)
search_total_paid: public(HashMap[bytes32, uint256])

# ─── Constructor ─────────────────────────────────────────────────────────────

@deploy
def __init__(registry_address: address, escrow_address: address):
    self.owner = msg.sender
    self.registry_address = registry_address
    self.escrow_address = escrow_address

    # Initialize price caps (in USDC base units — 6 decimals)
    # $0.001 = 1000, $0.002 = 2000, $0.003 = 3000
    self.action_prices["jd_parse"]        = 2000   # $0.002
    self.action_prices["apollo_search"]   = 1000   # $0.001
    self.action_prices["apollo_enrich"]   = 3000   # $0.003
    self.action_prices["github_profile"]  = 1000   # $0.001
    self.action_prices["github_repos"]    = 1000   # $0.001
    self.action_prices["hunter_domain"]   = 2000   # $0.002
    self.action_prices["hunter_find"]     = 2000   # $0.002
    self.action_prices["hunter_verify"]   = 1000   # $0.001
    self.action_prices["score_candidate"] = 3000   # $0.003

# ─── Owner Functions ─────────────────────────────────────────────────────────

@external
def set_price_cap(action_type: String[32], max_price: uint256):
    """Update the price cap for an action type. Owner only."""
    assert msg.sender == self.owner, "Only owner"
    self.action_prices[action_type] = max_price
    log PriceCapSet(action_type, max_price)

# ─── Core Payment Routing ─────────────────────────────────────────────────────

@external
def route_payment(
    search_id:     bytes32,
    agent_address: address,
    action_type:   String[32],
    amount:        uint256,
):
    """
    Route a USDC payment from escrow to an agent after task completion.

    Steps:
      1. Verify price cap not exceeded
      2. Verify agent is ERC-8004 registered and active
      3. Credit agent's earnings in PaymentEscrow
      4. Emit PaymentRouted event (visible on Arc Block Explorer)

    Called by the FastAPI backend after x402 payment proof is verified.
    """
    # 1. Price cap enforcement
    cap: uint256 = self.action_prices[action_type]
    assert cap > 0, "Unknown action type"
    assert amount <= cap, "Amount exceeds price cap"

    # 2. Agent identity verification (ERC-8004)
    registry: IAgentRegistry = IAgentRegistry(self.registry_address)
    assert registry.is_verified(agent_address), "Agent not registered or inactive"

    # 3. Credit agent in escrow
    escrow: IPaymentEscrow = IPaymentEscrow(self.escrow_address)
    escrow.credit_agent(search_id, agent_address, amount)

    # 4. Track total for this search
    self.search_total_paid[search_id] += amount

    # 5. Emit event — this is what shows on Arc Block Explorer
    log PaymentRouted(search_id, agent_address, action_type, amount, block.timestamp)


@external
def finalize_search(search_id: bytes32):
    """
    Mark a search as complete via escrow, enabling agent withdrawals
    and recruiter refund.
    """
    assert msg.sender == self.owner, "Only owner"
    escrow: IPaymentEscrow = IPaymentEscrow(self.escrow_address)
    escrow.finalize_search(search_id)

# ─── View Functions ──────────────────────────────────────────────────────────

@view
@external
def get_price_cap(action_type: String[32]) -> uint256:
    return self.action_prices[action_type]


@view
@external
def get_search_total(search_id: bytes32) -> uint256:
    return self.search_total_paid[search_id]
