# @version ^0.4.0
# @title PaymentEscrow — Search Budget Locking Contract
# @notice Orchestrator deposits the full search budget here at search start.
#         PaymentRouter credits agent earnings internally.
#         Agents withdraw after completing their tasks.
#         Unused budget is refundable to the recruiter.
# @dev Uses USDC ERC-20 on Arc testnet. Tested locally with Titanoboa.

from ethereum.ercs import IERC20

# ─── Events ──────────────────────────────────────────────────────────────────

event BudgetDeposited:
    search_id:  indexed(bytes32)
    recruiter:  indexed(address)
    amount:     uint256

event AgentCredited:
    search_id:     indexed(bytes32)
    agent_address: indexed(address)
    amount:        uint256

event AgentWithdrew:
    search_id:     indexed(bytes32)
    agent_address: indexed(address)
    amount:        uint256

event RecruiterRefunded:
    search_id: indexed(bytes32)
    recruiter: indexed(address)
    amount:    uint256

event SearchFinalized:
    search_id: indexed(bytes32)

# ─── Storage ─────────────────────────────────────────────────────────────────

struct SearchBudget:
    deposited_amount:  uint256
    spent_amount:      uint256
    recruiter_address: address
    is_complete:       bool

owner:          public(address)
usdc_token:     public(address)
payment_router: public(address)

# search_id → budget info
budgets:        public(HashMap[bytes32, SearchBudget])

# search_id → agent_address → earned amount
agent_earnings: public(HashMap[bytes32, HashMap[address, uint256]])

# search_id → agent_address → withdrawn
agent_withdrew: public(HashMap[bytes32, HashMap[address, bool]])

# ─── Constructor ─────────────────────────────────────────────────────────────

@deploy
def __init__(usdc_token: address, payment_router: address):
    self.owner = msg.sender
    self.usdc_token = usdc_token
    self.payment_router = payment_router

# ─── Owner Functions ─────────────────────────────────────────────────────────

@external
def set_payment_router(new_router: address):
    assert msg.sender == self.owner, "Only owner"
    self.payment_router = new_router

# ─── Orchestrator Functions ───────────────────────────────────────────────────

@external
def deposit(search_id: bytes32, amount: uint256):
    """
    Orchestrator deposits the full search budget at the start of each search.
    Requires prior USDC approval: usdc.approve(escrow_address, amount)
    """
    assert amount > 0, "Amount must be > 0"
    assert self.budgets[search_id].deposited_amount == 0, "Search already funded"

    usdc: IERC20 = IERC20(self.usdc_token)
    assert usdc.transferFrom(msg.sender, self, amount), "USDC transfer failed"

    self.budgets[search_id] = SearchBudget(
        deposited_amount=amount,
        spent_amount=0,
        recruiter_address=msg.sender,
        is_complete=False,
    )
    log BudgetDeposited(search_id, msg.sender, amount)

# ─── PaymentRouter Functions ─────────────────────────────────────────────────

@external
def credit_agent(search_id: bytes32, agent_address: address, amount: uint256):
    """
    Credit an agent's earnings for this search.
    Only callable by the PaymentRouter contract.
    """
    assert msg.sender == self.payment_router, "Only PaymentRouter can credit agents"
    budget: SearchBudget = self.budgets[search_id]
    assert budget.deposited_amount > 0, "Search not funded"
    assert not budget.is_complete, "Search already finalized"
    assert budget.spent_amount + amount <= budget.deposited_amount, "Exceeds budget"

    self.agent_earnings[search_id][agent_address] += amount
    self.budgets[search_id].spent_amount += amount
    log AgentCredited(search_id, agent_address, amount)

@external
def finalize_search(search_id: bytes32):
    """Mark a search as complete so agents can withdraw and recruiter can refund."""
    assert msg.sender == self.payment_router or msg.sender == self.owner, "Unauthorized"
    assert not self.budgets[search_id].is_complete, "Already finalized"
    self.budgets[search_id].is_complete = True
    log SearchFinalized(search_id)

# ─── Agent Withdrawal ────────────────────────────────────────────────────────

@external
def withdraw(search_id: bytes32):
    """
    Agent calls this after search is finalized to collect earned USDC.
    """
    assert self.budgets[search_id].is_complete, "Search not finalized yet"
    assert not self.agent_withdrew[search_id][msg.sender], "Already withdrew"

    earned: uint256 = self.agent_earnings[search_id][msg.sender]
    assert earned > 0, "Nothing to withdraw"

    self.agent_withdrew[search_id][msg.sender] = True
    usdc: IERC20 = IERC20(self.usdc_token)
    assert usdc.transfer(msg.sender, earned), "USDC transfer failed"
    log AgentWithdrew(search_id, msg.sender, earned)

# ─── Recruiter Refund ────────────────────────────────────────────────────────

@external
def refund(search_id: bytes32):
    """
    Recruiter reclaims unused budget after search is finalized.
    """
    budget: SearchBudget = self.budgets[search_id]
    assert budget.is_complete, "Search not finalized yet"
    assert msg.sender == budget.recruiter_address, "Only recruiter can refund"

    unused: uint256 = budget.deposited_amount - budget.spent_amount
    assert unused > 0, "No unused budget to refund"

    # Mark as fully spent to prevent double-refund
    self.budgets[search_id].spent_amount = budget.deposited_amount

    usdc: IERC20 = IERC20(self.usdc_token)
    assert usdc.transfer(budget.recruiter_address, unused), "USDC transfer failed"
    log RecruiterRefunded(search_id, budget.recruiter_address, unused)

# ─── View Functions ──────────────────────────────────────────────────────────

@view
@external
def get_budget(search_id: bytes32) -> (uint256, uint256, address, bool):
    """Returns (deposited, spent, recruiter, is_complete)."""
    b: SearchBudget = self.budgets[search_id]
    return (b.deposited_amount, b.spent_amount, b.recruiter_address, b.is_complete)


@view
@external
def get_agent_earned(search_id: bytes32, agent_address: address) -> uint256:
    return self.agent_earnings[search_id][agent_address]
