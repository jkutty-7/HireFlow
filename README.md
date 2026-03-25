# HireFlow — Agentic Talent Intelligence Engine

> Autonomous AI hiring pipeline powered by multi-agent orchestration, Circle Nanopayments on Arc L1, and real-time candidate intelligence.

---

## What It Does

HireFlow replaces a full recruitment team with a swarm of specialized AI agents. A recruiter pastes a job description — HireFlow autonomously finds, enriches, emails, and ranks the best-fit candidates, with every agent-to-agent action paid for in real USDC on Arc L1.

```
Recruiter → POST /api/search
               ↓
         JD Enhancement Agent (Kimi K2.5)    ← NEW: expands brief JDs
               ↓
         JD Parser Agent (Kimi K2.5)
               ↓
         Apollo Agent  →  GitHub Agent + Hunter Agent  (true parallel)
               ↓
         Scoring Agent (Claude Sonnet 4.6)   ← semantic skill matching
               ↓
         Talent Intelligence Agent (Claude Sonnet 4.6)  ← NEW: insights + interview questions
               ↓
         Ranked candidates + intelligence report + payment feed over WebSocket
```

---

## Architecture

### Agents

| Agent | Model | Role |
|---|---|---|
| JD Enhancement | Kimi K2.5 (NVIDIA NIM) | Expands abbreviated JDs, adds implied skills and search titles |
| JD Parser | Kimi K2.5 (NVIDIA NIM) | Extracts skills, seniority, titles from free-text JD |
| Apollo Agent | Kimi K2.5 | Searches + enriches candidates via Apollo.io; retries with relaxed params on thin results |
| GitHub Agent | Kimi K2.5 | Fetches repos, languages, activity score (0-100) |
| Hunter Agent | Kimi K2.5 | Finds + verifies professional emails using real company domains |
| Scoring Agent | Claude Sonnet 4.6 | Structured per-skill semantic matching + deterministic composite scoring |
| Talent Intelligence | Claude Sonnet 4.6 | Post-pool analysis: top-3 summary, red flags, interview questions per candidate |
| Orchestrator | LangGraph | State machine coordinating all agents |

### Candidate Scoring

Scoring uses two LLM calls per candidate for accuracy:

1. **Structured skill match** — per-skill semantic comparison returns `matched/unmatched` with reasoning. `skill_match_pct` is computed from the count in Python, not delegated to the LLM.
2. **Seniority + justification** — 4-5 sentence assessment covering strengths, skill gaps, seniority fit, and an actionable interviewer note.

Composite score is computed deterministically in Python:

```
All inputs normalised to 0-100 before weighting:

composite_score =
    skill_match_pct  × 0.40   (structured semantic matching)
  + github_score     × 0.30   (log-scale stars + weighted events + language match)
  + seniority_fit    × 0.20   (match=100, over=50, under=25 normalised)
  + email_validity   × 0.10   (verified=100, unverified=50, missing=0 normalised)
```

### GitHub Scoring

```
stars_score    = log(1 + total_stars) / log(1001) × 50   (log scale — fair for any level)
language_score = (matching_languages / required) × 30     (alias-normalised: js=javascript, ts=typescript)
activity_score = min(weighted_events × 1.5, 20)           (PR×3, Issue×2, Push×1 — quality over frequency)
github_score   = stars_score + language_score + activity_score
```

### Talent Intelligence Report

After scoring, `TalentIntelligenceAgent` produces a report stored on every search:

- **Top-3 summary** — executive paragraph on the strongest candidates and why
- **Search quality score** (0-100) — how good was this candidate pool?
- **Red flags** — job-hopping (avg tenure < 12 months), overqualified, location mismatch, thin pool
- **JD improvement suggestions** — if results were poor, what to change in the JD
- **Interview plans** — 3 targeted questions per top-5 candidate, based on their specific skill gaps

### LinkedIn Signals (from Apollo data)

Employment history from Apollo enrichment is analysed to surface:

- `avg_tenure_months` — average time per role across career
- `is_job_hopper` — avg tenure < 12 months with 3+ roles
- `career_trajectory` — ascending / lateral / descending / unknown

These feed directly into the Talent Intelligence red flags.

### Payment Flow

Every agent action is metered and paid in USDC:

| Action | Cost |
|---|---|
| JD Enhancement | $0.002 |
| JD Parse | $0.002 |
| Apollo Search | $0.001 |
| Apollo Enrich | $0.003 |
| GitHub Profile | $0.001 |
| GitHub Repos | $0.001 |
| Hunter Find | $0.002 |
| Hunter Verify | $0.001 |
| Score Candidate | $0.003 |
| Talent Intelligence | $0.005 |

Typical full search (25 candidates) costs **$0.30–$0.40 USDC** — vs $500–$2000 for a human recruiter.

### Smart Contracts (Vyper on Arc)

| Contract | Purpose |
|---|---|
| `AgentRegistry.vy` | ERC-8004 inspired on-chain agent identity |
| `PaymentEscrow.vy` | Locks search budget, enables agent withdrawals |
| `PaymentRouter.vy` | Routes USDC with per-action price cap enforcement |

### Tech Stack

- **Backend:** FastAPI + LangGraph + Python 3.12
- **LLMs:** Kimi K2.5 via NVIDIA NIM + Claude Sonnet 4.6 (Anthropic)
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
│   ├── jd_enhancement_agent.py  # JD expansion before parsing  ← NEW
│   ├── jd_parser.py           # JD → ParsedJD
│   ├── apollo_agent.py        # Apollo search + enrichment + retry
│   ├── github_agent.py        # GitHub profile + scoring
│   ├── hunter_agent.py        # Email find + verify
│   ├── scoring_agent.py       # Claude Sonnet 4.6 structured scoring
│   ├── talent_intelligence_agent.py  # Post-scoring insights  ← NEW
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
│   ├── apollo.py              # Apollo.io REST client + employment analysis
│   ├── github.py              # GitHub API client + improved scoring
│   ├── hunter.py              # Hunter.io API client
│   ├── circle_wallets.py      # Circle Wallets API
│   ├── circle_bridge.py       # Circle Bridge Kit
│   └── circle_gateway.py      # Circle Gateway
│
├── models/
│   ├── job.py                 # ParsedJD, EnhancedJD
│   ├── candidate.py           # CandidateRaw/Enriched/Scored
│   ├── intelligence.py        # TalentIntelligenceReport  ← NEW
│   ├── payment.py             # PaymentEvent, EIP3009Authorization
│   └── search.py              # SearchRequest/Status/Result
│
├── routes/
│   ├── search.py              # POST /api/search, GET results, GET intelligence
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
python -c "from eth_account import Account; [print(Account.create().key.hex()) for _ in range(8)]"
```

Assign the 8 printed keys to the `*_PRIVATE_KEY` variables in `.env` (6 original + `JD_ENHANCEMENT_PRIVATE_KEY` + `TALENT_INTELLIGENCE_PRIVATE_KEY`).

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

Returns ranked candidates with composite scores, skill match detail, skill gaps, justifications, emails, and GitHub data.

### Get Intelligence Report

```http
GET /api/search/{search_id}/intelligence
```

Returns the `TalentIntelligenceReport` for the search — top-3 summary, red flags, search quality score, and interview questions for each of the top 5 candidates.

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
