"""
Search Orchestrator — LangGraph state machine.

Orchestrates the full HireFlow pipeline:
  enhance_jd → parse_jd → verify_agents → deposit_escrow
  → collect_data (Apollo first, then GitHub + Hunter in true parallel)
  → score_candidates → talent_intelligence → finalize

Phase 2 fixes:
  - Bug 1: True parallel GitHub + Hunter (merge by apollo_id after both complete)
  - Bug 2: Composite score now computed in Python (not delegated to LLM)
  - Bug 3: JDParseError propagated — pipeline fails fast on bad JD parse
  - Bug 4: Hunter uses Apollo organization_domain (fixed in service layer)
  - Bug 5: All payment amounts sourced from settings.action_prices
  - Bug 6: Agent verification actually enforced in production
  - New:   enhance_jd node (JD Enhancement Agent)
  - New:   talent_intelligence node (Talent Intelligence Agent)
  - Perf:  Batch DB commits per node (not per payment)
  - Perf:  Apollo retry on thin results (< 8 candidates)
"""

import asyncio
import json
import uuid
import structlog

from typing import TypedDict
from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3

from settings import settings
from models.job import ParsedJD
from models.candidate import CandidateEnriched, CandidateScored
from models.payment import PaymentEvent
from agents.jd_enhancement_agent import enhance_job_description
from agents.jd_parser import parse_job_description, JDParseError
from agents.apollo_agent import run_apollo_agent
from agents.github_agent import run_github_agent
from agents.github_source_agent import run_github_source_agent
from agents.hunter_agent import run_hunter_agent
from agents.scoring_agent import run_scoring_agent
from agents.talent_intelligence_agent import run_talent_intelligence_agent
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
    additional_titles: list          # from JD Enhancement Agent
    parsed_jd: dict | None
    budget_deposited: bool
    agents_verified: bool
    candidates_raw: list
    candidates_enriched: list
    candidates_scored: list
    intelligence_report: dict | None
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

        graph.add_node("enhance_jd",         self._enhance_jd_node)
        graph.add_node("parse_jd",           self._parse_jd_node)
        graph.add_node("verify_agents",      self._verify_agents_node)
        graph.add_node("deposit_escrow",     self._deposit_escrow_node)
        graph.add_node("collect_data",       self._collect_data_node)
        graph.add_node("score_candidates",   self._score_candidates_node)
        graph.add_node("talent_intelligence", self._talent_intelligence_node)
        graph.add_node("finalize",           self._finalize_node)

        graph.set_entry_point("enhance_jd")
        graph.add_edge("enhance_jd",          "parse_jd")

        # After parse_jd: short-circuit to finalize on JD parse error
        graph.add_conditional_edges(
            "parse_jd",
            lambda state: "finalize" if state.get("error") else "verify_agents",
        )

        # After verify_agents: short-circuit to finalize if agents unverified in production
        graph.add_conditional_edges(
            "verify_agents",
            lambda state: "finalize" if state.get("error") else "deposit_escrow",
        )

        graph.add_edge("deposit_escrow",     "collect_data")
        graph.add_edge("collect_data",       "score_candidates")
        graph.add_edge("score_candidates",   "talent_intelligence")
        graph.add_edge("talent_intelligence", "finalize")
        graph.add_edge("finalize",           END)

        return graph.compile()

    # ─── Graph Nodes ──────────────────────────────────────────────────────────

    async def _enhance_jd_node(self, state: SearchState) -> dict:
        log.info("stage_enhance_jd", search_id=state["search_id"])

        enhanced = await enhance_job_description(state["job_description"])

        payment_records = []
        if enhanced.enhancement_applied:
            payment_records.append(dict(
                action_type="jd_enhance",
                paying_agent="user",
                receiving_agent="jd_enhancement_agent",
                amount_usdc=settings.action_prices["/jd/enhance"],
            ))

        await self._batch_record_payments(state["search_id"], payment_records)

        spent = state["total_spent_usdc"]
        if enhanced.enhancement_applied:
            spent += settings.action_prices["/jd/enhance"]

        return {
            "job_description": enhanced.enhanced_text,
            "additional_titles": enhanced.additional_titles,
            "total_spent_usdc": spent,
            "stage": "parse_jd",
        }

    async def _parse_jd_node(self, state: SearchState) -> dict:
        log.info("stage_parse_jd", search_id=state["search_id"])

        try:
            parsed = await parse_job_description(state["job_description"])
        except JDParseError as exc:
            log.error("jd_parse_failed_pipeline", error=str(exc), search_id=state["search_id"])
            # Update DB status to failed
            from sqlalchemy import update
            await self._db.execute(
                update(Search)
                .where(Search.id == uuid.UUID(state["search_id"]))
                .values(status="failed")
            )
            await self._db.commit()
            return {"error": f"JD parsing failed: {exc}", "stage": "finalize"}

        # Merge additional titles from enhancement agent
        if state.get("additional_titles"):
            parsed.titles = list(set(parsed.titles + state["additional_titles"]))

        payment_records = [dict(
            action_type="jd_parse",
            paying_agent="user",
            receiving_agent="jd_parser",
            amount_usdc=settings.action_prices["/jd/parse"],
        )]
        await self._batch_record_payments(state["search_id"], payment_records)

        return {
            "parsed_jd": parsed.model_dump(),
            "stage": "verify_agents",
            "total_spent_usdc": state["total_spent_usdc"] + settings.action_prices["/jd/parse"],
        }

    async def _verify_agents_node(self, state: SearchState) -> dict:
        log.info("stage_verify_agents", search_id=state["search_id"])
        agent_names = ["apollo_agent", "github_agent", "hunter_agent", "scoring_agent"]
        unverified: list[str] = []

        for agent_name in agent_names:
            address = self._wallet_manager.get_address(agent_name)
            if address:
                verified = await self._verifier.is_verified(address)
                if not verified:
                    log.warning("agent_not_verified", agent=agent_name, address=address)
                    unverified.append(agent_name)

        if unverified and settings.environment == "production":
            # Hard stop in production — unverified agents must not receive payments
            error_msg = f"Unverified agents: {unverified}. Register on-chain before searching."
            log.error("verification_failed_aborting", agents=unverified)
            from sqlalchemy import update
            await self._db.execute(
                update(Search)
                .where(Search.id == uuid.UUID(state["search_id"]))
                .values(status="failed")
            )
            await self._db.commit()
            return {"agents_verified": False, "error": error_msg, "stage": "finalize"}

        return {"agents_verified": True, "stage": "deposit_escrow"}

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
            orchestrator_key = self._wallet_manager.get_private_key("orchestrator")
            account = self._w3.eth.account.from_key(orchestrator_key)

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
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.get("status") == 0:
                log.error("escrow_deposit_reverted", tx=tx_hash.hex())
            else:
                log.info("escrow_deposited", amount_usdc=budget_usdc, tx=tx_hash.hex())

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

        # ── Step 1: Apollo + GitHub Repo Source in TRUE parallel ───────────────
        apollo_result, github_source_result = await asyncio.gather(
            run_apollo_agent(parsed_jd),
            run_github_source_agent(parsed_jd),
            return_exceptions=True,
        )

        apollo_candidates: list[CandidateEnriched] = (
            apollo_result if not isinstance(apollo_result, Exception) else []
        )
        github_source_candidates: list[CandidateEnriched] = (
            github_source_result if not isinstance(github_source_result, Exception) else []
        )

        if isinstance(apollo_result, Exception):
            log.error("apollo_agent_failed", error=str(apollo_result))
        if isinstance(github_source_result, Exception):
            log.warning("github_source_agent_failed", error=str(github_source_result))

        # ── Step 2: Merge Apollo + GitHub Source, deduplicate by github_username ─
        existing_github_usernames = {
            c.github_username for c in apollo_candidates if c.github_username
        }
        new_from_repos = [
            c for c in github_source_candidates
            if c.github_username not in existing_github_usernames
        ]
        all_candidates = apollo_candidates + new_from_repos

        log.info(
            "collect_data_merged",
            apollo=len(apollo_candidates),
            github_source=len(github_source_candidates),
            new_unique_from_repos=len(new_from_repos),
            total=len(all_candidates),
        )

        if not all_candidates:
            log.warning("collect_data_empty_results", search_id=state["search_id"])
            return {
                "candidates_enriched": [],
                "total_spent_usdc": state["total_spent_usdc"],
                "stage": "score_candidates",
            }

        # ── Step 3: GitHub enrichment + Hunter in TRUE parallel ────────────────
        # run_github_agent skips candidates that already have github_data (repo-sourced)
        github_results, hunter_results = await asyncio.gather(
            run_github_agent(all_candidates, parsed_jd),
            run_hunter_agent(all_candidates),
            return_exceptions=True,
        )

        if isinstance(github_results, Exception):
            log.error("github_agent_failed", error=str(github_results))
            github_results = all_candidates

        if isinstance(hunter_results, Exception):
            log.error("hunter_agent_failed", error=str(hunter_results))
            hunter_results = all_candidates

        # ── Step 4: Merge GitHub and Hunter results ────────────────────────────
        # Apollo candidates: merge by apollo_id
        # GitHub-source candidates: merge by github_username (no apollo_id)
        hunter_by_apollo = {c.apollo_id: c for c in hunter_results if c.apollo_id}
        hunter_by_github = {c.github_username: c for c in hunter_results if c.github_username and not c.apollo_id}

        merged: list[CandidateEnriched] = []
        for c in github_results:
            if c.apollo_id and c.apollo_id in hunter_by_apollo:
                h = hunter_by_apollo[c.apollo_id]
                c.email = h.email
                c.email_confidence = h.email_confidence
                c.email_status = h.email_status
            elif c.github_username and c.github_username in hunter_by_github and not c.email:
                h = hunter_by_github[c.github_username]
                c.email = h.email
                c.email_confidence = h.email_confidence
                c.email_status = h.email_status
            merged.append(c)

        # ── Step 5: Record payments (batch commit once) ────────────────────────
        payment_records = []
        spent = state["total_spent_usdc"]

        # GitHub Repo Source payments: search + per-repo scan
        if not isinstance(github_source_result, Exception) and github_source_candidates:
            payment_records.append(dict(
                action_type="github_repo_search",
                paying_agent="orchestrator",
                receiving_agent="github_source_agent",
                amount_usdc=settings.action_prices["/github/repo_search"],
            ))
            spent += settings.action_prices["/github/repo_search"]

        for candidate in merged:
            if candidate.source == "apollo":
                # Apollo: search + enrich
                payment_records.append(dict(
                    action_type="apollo_search",
                    paying_agent="orchestrator",
                    receiving_agent="apollo_agent",
                    amount_usdc=settings.action_prices["/apollo/search"],
                ))
                payment_records.append(dict(
                    action_type="apollo_enrich",
                    paying_agent="orchestrator",
                    receiving_agent="apollo_agent",
                    amount_usdc=settings.action_prices["/apollo/enrich"],
                ))
                spent += settings.action_prices["/apollo/search"] + settings.action_prices["/apollo/enrich"]
            elif candidate.source == "github_repo":
                # Repo scan fee per candidate sourced from GitHub
                payment_records.append(dict(
                    action_type="github_repo_scan",
                    paying_agent="orchestrator",
                    receiving_agent="github_source_agent",
                    amount_usdc=settings.action_prices["/github/repo_scan"],
                ))
                spent += settings.action_prices["/github/repo_scan"]

            # GitHub enrichment: only for Apollo candidates that got new data
            if candidate.github_data and candidate.source == "apollo":
                payment_records.append(dict(
                    action_type="github_profile",
                    paying_agent="orchestrator",
                    receiving_agent="github_agent",
                    amount_usdc=settings.action_prices["/github/profile"],
                ))
                payment_records.append(dict(
                    action_type="github_repos",
                    paying_agent="orchestrator",
                    receiving_agent="github_agent",
                    amount_usdc=settings.action_prices["/github/repos"],
                ))
                spent += settings.action_prices["/github/profile"] + settings.action_prices["/github/repos"]

            # Hunter: only if email was found
            if candidate.email and candidate.email_confidence is not None:
                payment_records.append(dict(
                    action_type="hunter_find",
                    paying_agent="orchestrator",
                    receiving_agent="hunter_agent",
                    amount_usdc=settings.action_prices["/hunter/find"],
                ))
                payment_records.append(dict(
                    action_type="hunter_verify",
                    paying_agent="orchestrator",
                    receiving_agent="hunter_agent",
                    amount_usdc=settings.action_prices["/hunter/verify"],
                ))
                spent += settings.action_prices["/hunter/find"] + settings.action_prices["/hunter/verify"]

        await self._batch_record_payments(state["search_id"], payment_records)

        return {
            "candidates_enriched": [c.model_dump() for c in merged],
            "total_spent_usdc": spent,
            "stage": "score_candidates",
        }

    async def _score_candidates_node(self, state: SearchState) -> dict:
        log.info("stage_score_candidates", search_id=state["search_id"])
        parsed_jd = ParsedJD(**state["parsed_jd"])

        enriched = [CandidateEnriched(**c) for c in state["candidates_enriched"]]
        scored = await run_scoring_agent(enriched, parsed_jd)

        payment_records = []
        spent = state["total_spent_usdc"]
        for _ in scored:
            payment_records.append(dict(
                action_type="score_candidate",
                paying_agent="orchestrator",
                receiving_agent="scoring_agent",
                amount_usdc=settings.action_prices["/score/candidate"],
            ))
            spent += settings.action_prices["/score/candidate"]

        await self._batch_record_payments(state["search_id"], payment_records)

        return {
            "candidates_scored": [c.model_dump() for c in scored],
            "total_spent_usdc": spent,
            "stage": "talent_intelligence",
        }

    async def _talent_intelligence_node(self, state: SearchState) -> dict:
        log.info("stage_talent_intelligence", search_id=state["search_id"])

        intelligence_report = None
        spent = state["total_spent_usdc"]

        try:
            parsed_jd = ParsedJD(**state["parsed_jd"])
            scored = [CandidateScored(**c) for c in state["candidates_scored"]]

            report = await run_talent_intelligence_agent(
                scored, parsed_jd, state["search_id"]
            )
            intelligence_report = report.model_dump(mode="json")

            payment_records = [dict(
                action_type="talent_intelligence",
                paying_agent="orchestrator",
                receiving_agent="talent_intelligence_agent",
                amount_usdc=settings.action_prices["/talent/intelligence"],
            )]
            await self._batch_record_payments(state["search_id"], payment_records)
            spent += settings.action_prices["/talent/intelligence"]

        except Exception as exc:
            # Non-critical: log and continue — report is additive value only
            log.error("talent_intelligence_failed", error=str(exc), search_id=state["search_id"])

        return {
            "intelligence_report": intelligence_report,
            "total_spent_usdc": spent,
            "stage": "finalize",
        }

    async def _finalize_node(self, state: SearchState) -> dict:
        log.info("stage_finalize", search_id=state["search_id"])

        from sqlalchemy import update
        from datetime import datetime, timezone

        search_uuid = uuid.UUID(state["search_id"])

        # If pipeline errored before scoring, just mark as failed and return
        if state.get("error"):
            await self._db.execute(
                update(Search)
                .where(Search.id == search_uuid)
                .values(status="failed", completed_at=datetime.now(timezone.utc))
            )
            await self._db.commit()
            return {"stage": "complete"}

        # Save scored candidates to DB
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
                skill_match_detail=c.get("skill_match_detail"),
                skill_gaps=c.get("skill_gaps"),
                source=c.get("source", "apollo"),
                source_repos=c.get("source_repos") or None,
            )
            self._db.add(orm_candidate)

        await self._db.flush()  # flush candidates before commit

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
                intelligence_report=state.get("intelligence_report"),
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

    async def _batch_record_payments(
        self,
        search_id: str,
        records: list[dict],
    ) -> None:
        """Persist a batch of payment records to DB in a single commit and broadcast each."""
        if not records:
            return

        search_uuid = uuid.UUID(search_id)
        for rec in records:
            orm_record = PaymentLog(
                search_id=search_uuid,
                action_type=rec["action_type"],
                paying_agent=rec["paying_agent"],
                receiving_agent=rec["receiving_agent"],
                amount_usdc=rec["amount_usdc"],
                status="confirmed",
            )
            self._db.add(orm_record)

        # Single commit for the whole batch
        await self._db.commit()

        # Broadcast each event to WebSocket clients
        if self._broadcast:
            for rec in records:
                event = PaymentEvent(
                    search_id=search_id,
                    action_type=rec["action_type"],
                    paying_agent=rec["paying_agent"],
                    receiving_agent=rec["receiving_agent"],
                    amount_usdc=rec["amount_usdc"],
                    status="confirmed",
                )
                try:
                    await self._broadcast(search_id, event.model_dump(mode="json"))
                except Exception:
                    pass

    async def run(self, search_id: str, job_description: str) -> SearchState:
        """Execute the full pipeline for a search. Returns final state."""
        initial_state: SearchState = {
            "search_id":           search_id,
            "job_description":     job_description,
            "additional_titles":   [],
            "parsed_jd":           None,
            "budget_deposited":    False,
            "agents_verified":     False,
            "candidates_raw":      [],
            "candidates_enriched": [],
            "candidates_scored":   [],
            "intelligence_report": None,
            "payment_log":         [],
            "total_spent_usdc":    0.0,
            "error":               None,
            "stage":               "enhance_jd",
        }
        final_state = await self._graph.ainvoke(initial_state)
        return final_state

    async def close(self) -> None:
        await self._nano.close()
