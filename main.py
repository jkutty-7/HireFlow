"""
HireFlow FastAPI Application Entrypoint.

Startup:
  1. Create DB tables
  2. Ensure all agent wallets exist (Circle Wallets API)
  3. Register agents on-chain (AgentRegistry.vy) if not already done
  4. Start WebSocket manager for live payment feed

Endpoints:
  POST /api/search             — start hiring pipeline
  GET  /api/search/{id}/status — poll search status
  GET  /api/search/{id}/results— get ranked candidates
  GET  /api/payments/{id}/feed — payment transaction log
  GET  /api/wallets/balances   — all agent wallet balances
  POST /api/bridge             — bridge USDC to Arc
  WS   /ws/{search_id}         — live payment feed stream
"""

import asyncio
import json
import structlog
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from eth_account import Account

from settings import settings
from db.database import create_tables
from payments.x402_middleware import X402PaymentMiddleware
from routes import search as search_router
from routes import wallets as wallets_router
from routes import payments as payments_router

# Agent name → derived Ethereum address (from private key in .env)
AGENT_ADDRESSES: dict[str, str] = {}


def _init_agent_addresses() -> None:
    """
    Derive wallet addresses from private keys already in .env.
    No Circle API call needed — address = public key of private key.
    """
    key_map = {
        "orchestrator":  settings.orchestrator_private_key,
        "jd_parser":     settings.jd_parser_private_key,
        "apollo_agent":  settings.apollo_private_key,
        "github_agent":  settings.github_private_key,
        "hunter_agent":  settings.hunter_private_key,
        "scoring_agent": settings.scoring_private_key,
    }
    for agent_name, key in key_map.items():
        if key and key != "0x" and len(key) >= 64:
            try:
                AGENT_ADDRESSES[agent_name] = Account.from_key(key).address
            except Exception:
                pass
    log.info("agent_addresses_derived", count=len(AGENT_ADDRESSES))

log = structlog.get_logger()

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
)
logging.basicConfig(level=getattr(logging, settings.log_level))


# ─── WebSocket Manager ────────────────────────────────────────────────────────

class WebSocketManager:
    """
    Manages WebSocket connections per search_id.
    Broadcasts live payment events to all connected frontend clients.
    """

    def __init__(self):
        # search_id → list of active WebSocket connections
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, search_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(search_id, []).append(ws)
        log.info("ws_connected", search_id=search_id)

    def disconnect(self, search_id: str, ws: WebSocket):
        conns = self._connections.get(search_id, [])
        if ws in conns:
            conns.remove(ws)
        log.info("ws_disconnected", search_id=search_id)

    async def broadcast(self, search_id: str, data: dict):
        """Send a payment event to all clients watching this search."""
        conns = self._connections.get(search_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(search_id, ws)


ws_manager = WebSocketManager()


# ─── App Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("hireflow_starting", environment=settings.environment)

    # 1. Create database tables
    await create_tables()
    log.info("db_tables_ready")

    # 2. Derive agent wallet addresses from private keys (no Circle API needed at startup)
    _init_agent_addresses()

    log.info("hireflow_ready")
    yield

    log.info("hireflow_shutting_down")


# ─── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="HireFlow — Agentic Talent Intelligence Engine",
    description="Autonomous AI hiring agent with Circle Nanopayments on Arc L1",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — configurable via CORS_ORIGINS env var (comma-separated in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# x402 payment wall middleware for agent endpoints
app.add_middleware(X402PaymentMiddleware)

# ─── Routers ─────────────────────────────────────────────────────────────────

app.include_router(search_router.router)
app.include_router(wallets_router.router)
app.include_router(payments_router.router)


# ─── WebSocket Endpoint ──────────────────────────────────────────────────────

@app.websocket("/ws/{search_id}")
async def websocket_payment_feed(websocket: WebSocket, search_id: str):
    """
    WebSocket endpoint for live payment feed.
    Frontend connects here immediately after POST /api/search.
    Receives PaymentEvent JSON objects in real-time as USDC flows between agents.
    """
    await ws_manager.connect(search_id, websocket)
    try:
        while True:
            # Keep connection alive — frontend sends pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(search_id, websocket)


# ─── Health Check ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "environment": settings.environment,
        "arc_chain_id": settings.arc_chain_id,
    }


@app.get("/")
async def root():
    return {
        "app": "HireFlow",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment != "production",
        log_level=settings.log_level.lower(),
    )
