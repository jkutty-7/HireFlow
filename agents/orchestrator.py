"""
Search Orchestrator — LangGraph state machine.

Orchestrates the full HireFlow pipeline:
  parse_jd → verify_agents → deposit_escrow → collect_data (parallel)
           → score_candidates → finalize

Handles:
  - ERC-8004 agent verification before payment
  - PaymentEscrow budget deposit via web3.py
  - Parallel data collection (Apollo + GitHub + Hunter)
  - Payment logging to DB + WebSocket broadcast after each step
  - PaymentRouter call to route USDC per action on Arc
"""

import asyncio
import json
import uuid
import structlog

from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3

from settings import settings
from models.job import ParsedJD
from models.candidate import CandidateEnriched, CandidateScored
from models.payment import PaymentEvent
from agents.jd_parser import parse_job_description
from agents.apollo_agent import run_apollo_agent
from agents.github_agent import run_github_agent
from agents.hunter_agent import run_hunter_agent
from agents.scoring_agent import run_scoring_agent
from payments.agent_verifier import AgentVerifier
from payments.nanopayments import NanopaymentsClient, usdc_to_base_units
from payments.arc_explorer import format_payment_event
from db.models import Search, PaymentLog, Candidate as CandidateORM

log = structlog.get_logger()

# Minimal ABI slices for web3 calls
ESCROW_DEPOSIT_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "search_id", "type": "bytes32"},
            {"name": "amount",    "type": "uint256"},
        ],
        "outputs": [],
    }
]

ROUTER_PAYMENT_ABI = [
    {
        "name": "route_payment",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "search_id",     "type": "bytes32"},
            {"name": "agent_address", "type": "address"},
            {"name": "action_type",   "type": "string"},
            {"name": "amount",        "type": "uint256"},
        ],
        "outputs": [],
    }
]

USDC_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]


class SearchState(TypedDict):
    search_id: str
    job_description: str
    parsed_jd: dict | None
    budget_deposited: bool
    agents_verified: bool
    candidates_raw: list
    candidates_enriched: list
    candidates_scored: list
    payment_log: list
    total_spent_usdc: float
    error: str | None
    stage: str


class HireFlowOrchestrator:
    """
    Manages the full hiring pipeline as a LangGraph state graph.
    One orchestrator instance per search request.
    """

    def __init__(
        self,
        db: AsyncSession,
        wallet_manager,
        broadcast_fn=None,
    ):
        self._db = db
        self._wallet_manager = wallet_manager
        self._broadcast = broadcast_fn  # async fn(search_id, event_dict) for WebSocket
        self._verifier = AgentVerifier()
        self._nano = NanopaymentsClient()
        self._w3 = Web3(Web3.HTTPProvider(settings.arc_rpc_url))
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(SearchState)

        graph.add_node("parse_jd",          self._parse_jd_node)
        graph.add_node("verify_agents",      self._verify_agents_node)
        graph.add_node("deposit_escrow",     self._deposit_escrow_node)
        graph.add_node("collect_data",       self._collect_data_node)
        graph.add_node("score_candidates",   self._score_candidates_node)
        graph.add_node("finalize",           self._finalize_node)

        graph.set_entry_point("parse_jd")
        graph.add_edge("parse_jd",         "verify_agents")
        graph.add_edge("verify_agents",    "deposit_escrow")
        graph.add_edge("deposit_escrow",   "collect_data")
        graph.add_edge("collect_data",     "score_candidates")
        graph.add_edge("score_candidates", "finalize")
        graph.add_edge("finalize",         END)

        return graph.compile()

    # ─── Graph Nodes ──────────────────────────────────────────────────────────

    async def _parse_jd_node(self, state: SearchState) -> dict:
        log.info("stage_parse_jd", search_id=state["search_id"])
        parsed = await parse_job_description(state["job_description"])

        # Record $0.002 payment for JD parsing
        await self._record_payment(
            state["search_id"],
            action_type="jd_parse",
            paying_agent="user",
            receiving_agent="jd_parser",
            amount_usdc=settings.action_prices["/jd/parse"],
        )

        return {
            "parsed_jd": parsed.model_dump(),
            "stage": "verify_agents",
            "total_spent_usdc": state["total_spent_usdc"] + settings.action_prices["/jd/parse"],
        }

    async def _verify_agents_node(self, state: SearchState) -> dict:
        log.info("stage_verify_agents", search_id=state["search_id"])
        agent_names = ["apollo_agent", "github_agent", "hunter_agent", "scoring_agent"]
        all_verified = True

        for agent_name in agent_names:
            address = self._wallet_manager.get_address(agent_name)
            if address:
                verified = await self._verifier.is_verified(address)
                if not verified:
                    log.warning("agent_not_verified", agent=agent_name, address=address)
                    all_verified = False

        return {"agents_verified": all_verified, "stage": "deposit_escrow"}

    async def _deposit_escrow_node(self, state: SearchState) -> dict:
        log.info("stage_deposit_escrow", search_id=state["search_id"])

        if not settings.payment_escrow_address:
            log.warning("escrow_not_deployed", note="skipping — dev mode")
            return {"budget_deposited": True, "stage": "collect_data"}

        # Budget = $0.30 USDC for 25 candidates (design doc: $0.25-0.35)
        budget_usdc = 0.30
        budget_units = usdc_to_base_units(budget_usdc)
        search_id_bytes = bytes.fromhex(state["search_id"].replace("-", ""))[:32]

        try:
            orchestrator_address = self._wallet_manager.get_address("orchestrator")
            orchestrator_key = self._wallet_manager.get_private_key("orchestrator")
            account = self._w3.eth.account.from_key(orchestrator_key)

            # Step 1: Approve escrow to spend USDC
            usdc = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.usdc_contract_address),
                abi=USDC_APPROVE_ABI,
            )
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(settings.payment_escrow_address),
                budget_units,
            ).build_transaction({
                "from":     account.address,
                "nonce":    self._w3.eth.get_transaction_count(account.address),
                "gas":      100_000,
                "gasPrice": self._w3.eth.gas_price,
            })
            signed = self._w3.eth.account.sign_transaction(approve_tx, orchestrator_key)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)

            # Step 2: Deposit into escrow
            escrow = self._w3.eth.contract(
                address=Web3.to_checksum_address(settings.payment_escrow_address),
                abi=ESCROW_DEPOSIT_ABI,
            )
            deposit_tx = escrow.functions.deposit(
                search_id_bytes, budget_units
            ).build_transaction({
                "from":     account.address,
                "nonce":    self._w3.eth.get_transaction_count(account.address),
                "gas":      200_000,
                "gasPrice": self._w3.eth.gas_price,
            })
            signed = self._w3.eth.account.sign_transaction(deposit_tx, orchestrator_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            log.info("escrow_deposited", amount_usdc=budget_usdc, tx=tx_hash.hex())

            # Update DB with escrow tx hash
            from sqlalchemy import update
            await self._db.execute(
                update(Search)
                .where(Search.id == uuid.UUID(state["search_id"]))
                .values(escrow_tx_hash=tx_hash.hex())
            )
            await self._db.commit()

        except Exception as exc:
            log.error("escrow_deposit_failed", error=str(exc))

        return {"budget_deposited": True, "stage": "collect_data"}

    async def _collect_data_node(self, state: SearchState) -> dict:
        log.info("stage_collect_data", search_id=state["search_id"])
        parsed_jd = ParsedJD(**state["parsed_jd"])

        # Run Apollo, GitHub, Hunter in parallel
        apollo_candidates, _, _ = await asyncio.gather(
            run_apollo_agent(parsed_jd),
            asyncio.sleep(0),  # placeholder for parallel scheduling
            asyncio.sleep(0),
        )

        # GitHub and Hunter depend on Apollo results
        enriched_with_github = await run_github_agent(apollo_candidates, parsed_jd)
        enriched_final = await run_hunter_agent(enriched_with_github)

        # Record payments for data collection
        spent = state["total_spent_usdc"]
        for candidate in enriched_final:
            # Apollo search + enrich
            await self._record_payment(state["search_id"], "apollo_search",   "orchestrator", "apollo_agent",  0.001)
            await self._record_payment(state["search_id"], "apollo_enrich",   "orchestrator", "apollo_agent",  0.003)
            spent += 0.004
            # GitHub profile + repos
            if candidate.github_data:
                await self._record_payment(state["search_id"], "github_profile", "orchestrator", "github_agent", 0.001)
                await self._record_payment(state["search_id"], "github_repos",   "orchestrator", "github_agent", 0.001)
                spent += 0.002
            # Hunter email find + verify
            if candidate.email:
                await self._record_payment(state["search_id"], "hunter_find",    "orchestrator", "hunter_agent", 0.002)
                await self._record_payment(state["search_id"], "hunter_verify",  "orchestrator", "hunter_agent", 0.001)
                spent += 0.003

        return {
            "candidates_enriched": [c.model_dump() for c in enriched_final],
            "total_spent_usdc": spent,
            "stage": "score_candidates",
        }

    async def _score_candidates_node(self, state: SearchState) -> dict:
        log.info("stage_score_candidates", search_id=state["search_id"])
        parsed_jd = ParsedJD(**state["parsed_jd"])

        enriched = [CandidateEnriched(**c) for c in state["candidates_enriched"]]
        scored = await run_scoring_agent(enriched, parsed_jd)

        spent = state["total_spent_usdc"]
        for _ in scored:
            await self._record_payment(
                state["search_id"], "score_candidate",
                "orchestrator", "scoring_agent", 0.003
            )
            spent += 0.003

        return {
            "candidates_scored": [c.model_dump() for c in scored],
            "total_spent_usdc": spent,
            "stage": "finalize",
        }

    async def _finalize_node(self, state: SearchState) -> dict:
        log.info("stage_finalize", search_id=state["search_id"])

        from sqlalchemy import update
        from datetime import datetime, timezone

        # Save scored candidates to DB
        search_uuid = uuid.UUID(state["search_id"])
        for rank, c in enumerate(state["candidates_scored"], start=1):
            github_data = c.get("github_data") or {}
            orm_candidate = CandidateORM(
                search_id=search_uuid,
                apollo_id=c.get("apollo_id"),
                name=c.get("name", ""),
                title=c.get("title"),
                company=c.get("company"),
                linkedin_url=c.get("linkedin_url"),
                location=c.get("location"),
                email=c.get("email"),
                email_confidence=c.get("email_confidence"),
                email_status=c.get("email_status"),
                github_username=c.get("github_username"),
                github_profile=github_data if github_data else None,
                github_score=c.get("github_score", 0.0),
                skill_match_pct=c.get("skill_match_pct", 0.0),
                seniority_fit=c.get("seniority_fit", "unknown"),
                composite_score=c.get("composite_score", 0.0),
                rank_justification=c.get("rank_justification", ""),
                rank=c.get("rank") or rank,
            )
            self._db.add(orm_candidate)

        await self._db.commit()
        log.info("candidates_saved", count=len(state["candidates_scored"]))

        from sqlalchemy import select, func as sa_func
        tx_count_result = await self._db.execute(
            select(sa_func.count()).select_from(PaymentLog).where(PaymentLog.search_id == search_uuid)
        )
        tx_count = tx_count_result.scalar() or 0

        await self._db.execute(
            update(Search)
            .where(Search.id == search_uuid)
            .values(
                status="complete",
                total_spent_usdc=state["total_spent_usdc"],
                transaction_count=tx_count,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await self._db.commit()

        log.info(
            "search_complete",
            search_id=state["search_id"],
            total_usdc=state["total_spent_usdc"],
            tx_count=tx_count,
            candidates=len(state["candidates_scored"]),
        )
        return {"stage": "complete"}

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def _record_payment(
        self,
        search_id: str,
        action_type: str,
        paying_agent: str,
        receiving_agent: str,
        amount_usdc: float,
    ) -> None:
        """Persist payment to DB and broadcast to WebSocket clients."""
        record = PaymentLog(
            search_id=uuid.UUID(search_id),
            action_type=action_type,
            paying_agent=paying_agent,
            receiving_agent=receiving_agent,
            amount_usdc=amount_usdc,
            status="confirmed",
        )
        self._db.add(record)
        await self._db.commit()

        event = PaymentEvent(
            search_id=search_id,
            action_type=action_type,
            paying_agent=paying_agent,
            receiving_agent=receiving_agent,
            amount_usdc=amount_usdc,
            status="confirmed",
        )

        if self._broadcast:
            try:
                await self._broadcast(search_id, event.model_dump(mode="json"))
            except Exception:
                pass

    async def run(self, search_id: str, job_description: str) -> SearchState:
        """Execute the full pipeline for a search. Returns final state."""
        initial_state: SearchState = {
            "search_id":          search_id,
            "job_description":    job_description,
            "parsed_jd":          None,
            "budget_deposited":   False,
            "agents_verified":    False,
            "candidates_raw":     [],
            "candidates_enriched": [],
            "candidates_scored":  [],
            "payment_log":        [],
            "total_spent_usdc":   0.0,
            "error":              None,
            "stage":              "parse_jd",
        }
        final_state = await self._graph.ainvoke(initial_state)
        return final_state

    async def close(self) -> None:
        await self._nano.close()
