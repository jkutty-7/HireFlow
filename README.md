# HireFlow — Agentic Talent Intelligence Engine

> Autonomous AI hiring pipeline powered by multi-agent orchestration, Circle Nanopayments on Arc L1, and real-time candidate intelligence.

---

## What It Does

HireFlow replaces a full recruitment team with a swarm of specialized AI agents. A recruiter pastes a job description — HireFlow autonomously finds, enriches, emails, and ranks the best-fit candidates, with every agent-to-agent action paid for in real USDC on Arc L1.

```
Recruiter → POST /api/search
               ↓
         JD Parser Agent (Kimi K2.5)
               ↓
         Apollo Agent  →  GitHub Agent  →  Hunter Agent  (parallel)
               ↓
         Scoring Agent (Claude Sonnet 4.5)
               ↓
         Ranked candidates + payment feed over WebSocket
```

---

## Architecture

### Agents

| Agent | Model | Role |
|---|---|---|
| JD Parser | Kimi K2.5 (NVIDIA NIM) | Extracts skills, seniority, titles from free-text JD |
| Apollo Agent | Kimi K2.5 | Searches + enriches candidates via Apollo.io |
| GitHub Agent | Kimi K2.5 | Fetches repos, languages, activity score (0-100) |
| Hunter Agent | Kimi K2.5 | Finds + verifies professional emails |
| Scoring Agent | Claude Sonnet 4.5 | Multi-factor AI scoring with justification |
| Orchestrator | LangGraph | State machine coordinating all agents |

### Payment Flow

Every agent action is metered and paid in USDC:

| Action | Cost |
|---|---|
| JD Parse | $0.002 |
| Apollo Search | $0.001 |
| Apollo Enrich | $0.003 |
| GitHub Profile | $0.001 |
| GitHub Repos | $0.001 |
| Hunter Find | $0.002 |
| Hunter Verify | $0.001 |
| Score Candidate | $0.003 |

Typical full search (25 candidates) costs **$0.25–$0.35 USDC** — vs $500–$2000 for a human recruiter.

### Smart Contracts (Vyper on Arc)

| Contract | Purpose |
|---|---|
| `AgentRegistry.vy` | ERC-8004 inspired on-chain agent identity |
| `PaymentEscrow.vy` | Locks search budget, enables agent withdrawals |
| `PaymentRouter.vy` | Routes USDC with per-action price cap enforcement |

### Tech Stack

- **Backend:** FastAPI + LangGraph + Python 3.12
- **LLMs:** Kimi K2.5 via NVIDIA NIM + Claude Sonnet 4.5 (Anthropic)
- **Blockchain:** Vyper contracts + web3.py + EIP-3009 signed payments
- **Payments:** Circle Nanopayments + x402 payment wall middleware
- **Data APIs:** Apollo.io + GitHub REST API + Hunter.io
- **Database:** PostgreSQL (async via SQLAlchemy + asyncpg)
- **Real-time:** WebSocket live payment feed

---

## Project Structure

```
HireFlow/
├── main.py                    # FastAPI app + WebSocket manager
├── settings.py                # All config via pydantic-settings
├── requirements.txt
│
├── agents/
│   ├── base.py                # Kimi K2.5 agent factory
│   ├── jd_parser.py           # JD → ParsedJD
│   ├── apollo_agent.py        # Apollo search + enrichment
│   ├── github_agent.py        # GitHub profile + scoring
│   ├── hunter_agent.py        # Email find + verify
│   ├── scoring_agent.py       # Claude Sonnet 4.5 scoring
│   └── orchestrator.py        # LangGraph state machine
│
├── contracts/
│   ├── AgentRegistry.vy       # On-chain agent identity
│   ├── PaymentEscrow.vy       # Budget locking
│   ├── PaymentRouter.vy       # Payment routing with price caps
│   └── deploy.py              # Deployment script
│
├── payments/
│   ├── nanopayments.py        # EIP-3009 signed transfers
│   ├── x402_middleware.py     # HTTP 402 payment wall
│   ├── wallet_manager.py      # Agent wallet lifecycle
│   ├── agent_verifier.py      # ERC-8004 on-chain verification
│   └── arc_explorer.py        # Arc Block Explorer links
│
├── services/
│   ├── apollo.py              # Apollo.io REST client
│   ├── github.py              # GitHub API client
│   ├── hunter.py              # Hunter.io API client
│   ├── circle_wallets.py      # Circle Wallets API
│   ├── circle_bridge.py       # Circle Bridge Kit
│   └── circle_gateway.py      # Circle Gateway
│
├── models/
│   ├── job.py                 # ParsedJD
│   ├── candidate.py           # CandidateRaw/Enriched/Scored
│   ├── payment.py             # PaymentEvent, EIP3009Authorization
│   └── search.py              # SearchRequest/Status/Result
│
├── routes/
│   ├── search.py              # POST /api/search, GET results
│   ├── wallets.py             # Wallet balances + bridge
│   └── payments.py            # Payment feed
│
└── db/
    ├── database.py            # Async SQLAlchemy engine
    ├── models.py              # Search, Candidate, PaymentLog, AgentWallet
    └── migrations/            # Alembic async migrations
```

---

## Setup

### 1. Clone & create environment

```bash
git clone https://github.com/yourname/hireflow
cd HireFlow
python -m venv .venv
.venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Fill in `.env` — see table below for where to get each key.

### 3. Generate agent wallet keys

```bash
python -c "from eth_account import Account; [print(Account.create().key.hex()) for _ in range(6)]"
```

Assign the 6 printed keys to the `*_PRIVATE_KEY` variables in `.env`.

### 4. Set up PostgreSQL

Use [Render](https://render.com) free tier or [Neon](https://neon.tech):
- Create a free PostgreSQL database
- Copy the External connection string
- Set `DATABASE_URL=postgresql+asyncpg://...` in `.env`

### 5. Run migrations

```bash
alembic upgrade head
```

### 6. Start the API

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

## API Keys Required

| Variable | Where to get |
|---|---|
| `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com) → API Keys |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `APOLLO_API_KEY` | Apollo.io → Settings → Integrations → Master API Key |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Tokens (classic) |
| `HUNTER_API_KEY` | [hunter.io](https://hunter.io) → Dashboard → API |
| `CIRCLE_API_KEY` | [app.circle.com](https://app.circle.com) → Developer → API Keys |
| `DATABASE_URL` | Render / Neon / local PostgreSQL |

---

## API Reference

### Start a Search

```http
POST /api/search
Content-Type: application/json

{
  "job_description": "Senior Python Backend Engineer with FastAPI, PostgreSQL, AWS...",
  "max_candidates": 25
}
```

Returns `202 Accepted` with a `search_id`. Pipeline runs in the background.

### Poll Status

```http
GET /api/search/{search_id}/status
```

### Get Results

```http
GET /api/search/{search_id}/results
```

Returns ranked candidates with composite scores, justifications, emails, and GitHub data.

### Live Payment Feed (WebSocket)

```
ws://localhost:8000/ws/{search_id}
```

Streams real-time payment events as USDC flows between agents during the search.

### Payment Summary

```http
GET /api/payments/{search_id}/summary
```

Returns cost breakdown by action type with Arc Block Explorer links.

---

## Candidate Scoring Formula

```
composite_score =
    skill_match_pct  × 0.40   (Claude extracts matched skills)
  + github_score     × 0.30   (stars + languages + activity)
  + seniority_fit    × 0.20   (under=5, over=10, match=20)
  + email_validity   × 0.10   (verified=10, unverified=5, missing=0)
```

---

## Deploy Smart Contracts

After adding `ORCHESTRATOR_PRIVATE_KEY` and Arc RPC to `.env`:

```bash
python contracts/deploy.py
```

Copy the printed addresses to `.env`:
```
AGENT_REGISTRY_ADDRESS=0x...
PAYMENT_ESCROW_ADDRESS=0x...
PAYMENT_ROUTER_ADDRESS=0x...
```

---

## License

MIT
