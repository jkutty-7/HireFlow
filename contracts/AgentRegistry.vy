# @version ^0.4.0
# @title AgentRegistry — ERC-8004 inspired on-chain agent identity registry
# @notice Every HireFlow agent registers here before receiving USDC payments.
#         The orchestrator calls is_verified() before every Nanopayment transfer.
# @dev Deployed on Arc testnet. Owner can register and deactivate agents.

# ─── Events ──────────────────────────────────────────────────────────────────

event AgentRegistered:
    agent_address: indexed(address)
    agent_name:    String[64]
    registered_at: uint256

event AgentDeactivated:
    agent_address: indexed(address)

event AgentEarningsUpdated:
    agent_address: indexed(address)
    amount:        uint256
    total_earned:  uint256

# ─── Storage ─────────────────────────────────────────────────────────────────

struct AgentInfo:
    agent_name:    String[64]
    registered_at: uint256
    is_active:     bool
    total_earned:  uint256   # cumulative USDC earned (in base units, 6 decimals)

owner:              public(address)
agents:             public(HashMap[address, AgentInfo])
registered_agents:  public(DynArray[address, 100])

# ─── Constructor ─────────────────────────────────────────────────────────────

@deploy
def __init__():
    self.owner = msg.sender

# ─── Owner Functions ─────────────────────────────────────────────────────────

@external
def register_agent(agent_address: address, agent_name: String[64]):
    """
    Register a new agent. Called at HireFlow system startup for each agent.
    Only the contract owner (deployer) can register agents.
    """
    assert msg.sender == self.owner, "Only owner can register agents"
    assert agent_address != empty(address), "Invalid agent address"
    assert not self.agents[agent_address].is_active, "Agent already registered"

    self.agents[agent_address] = AgentInfo(
        agent_name=agent_name,
        registered_at=block.timestamp,
        is_active=True,
        total_earned=0,
    )
    self.registered_agents.append(agent_address)
    log AgentRegistered(agent_address, agent_name, block.timestamp)


@external
def deactivate_agent(agent_address: address):
    """
    Deactivate a compromised or retired agent.
    Orchestrator will refuse to pay deactivated agents.
    """
    assert msg.sender == self.owner, "Only owner can deactivate agents"
    assert self.agents[agent_address].is_active, "Agent not active"
    self.agents[agent_address].is_active = False
    log AgentDeactivated(agent_address)


@external
def record_earning(agent_address: address, amount: uint256):
    """
    Record USDC earned by an agent. Called by PaymentRouter after each payment.
    Builds on-chain reputation via total_earned field.
    """
    assert msg.sender == self.owner, "Only owner can record earnings"
    assert self.agents[agent_address].is_active, "Agent not active"
    self.agents[agent_address].total_earned += amount
    log AgentEarningsUpdated(
        agent_address,
        amount,
        self.agents[agent_address].total_earned
    )

# ─── View Functions ──────────────────────────────────────────────────────────

@view
@external
def is_verified(agent_address: address) -> bool:
    """
    Returns True if the agent is registered and currently active.
    Called by orchestrator before every payment.
    """
    return self.agents[agent_address].is_active


@view
@external
def get_agent(agent_address: address) -> (String[64], uint256, bool, uint256):
    """
    Returns full agent info: (name, registered_at, is_active, total_earned).
    """
    info: AgentInfo = self.agents[agent_address]
    return (info.agent_name, info.registered_at, info.is_active, info.total_earned)


@view
@external
def get_all_agents() -> DynArray[address, 100]:
    """Return list of all ever-registered agent addresses."""
    return self.registered_agents
