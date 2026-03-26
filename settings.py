from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── LLM Providers ───────────────────────────────────────
    nvidia_api_key: str = Field(..., description="NVIDIA NIM API key for Kimi K2.5")
    anthropic_api_key: str = Field(..., description="Anthropic API key for Claude Sonnet 4.5")

    # ─── Candidate Data APIs ─────────────────────────────────
    apollo_api_key: str = Field(..., description="Apollo.io master API key")
    github_token: str = Field(..., description="GitHub personal access token")
    hunter_api_key: str = Field(..., description="Hunter.io API key")

    # ─── Circle Infrastructure ───────────────────────────────
    circle_api_key: str = Field(..., description="Circle developer API key")
    arc_rpc_url: str = Field(default="https://rpc.testnet.arc.network")
    arc_chain_id: int = Field(default=5042002)
    usdc_contract_address: str = Field(
        default="0x0000000000000000000000000000000000000001",
        description="USDC contract address on Arc testnet",
    )

    # ─── Smart Contracts ─────────────────────────────────────
    agent_registry_address: str = Field(default="")
    payment_escrow_address: str = Field(default="")
    payment_router_address: str = Field(default="")

    # ─── Circle Wallet IDs ───────────────────────────────────
    orchestrator_wallet_id: str = Field(default="")
    apollo_agent_wallet_id: str = Field(default="")
    github_agent_wallet_id: str = Field(default="")
    hunter_agent_wallet_id: str = Field(default="")
    scoring_agent_wallet_id: str = Field(default="")
    jd_parser_wallet_id: str = Field(default="")
    jd_enhancement_wallet_id: str = Field(default="")
    talent_intelligence_wallet_id: str = Field(default="")
    github_source_agent_wallet_id: str = Field(default="")

    # ─── Wallet Private Keys (EIP-3009 signing) ──────────────
    orchestrator_private_key: str = Field(default="0x")
    apollo_private_key: str = Field(default="0x")
    github_private_key: str = Field(default="0x")
    hunter_private_key: str = Field(default="0x")
    scoring_private_key: str = Field(default="0x")
    jd_parser_private_key: str = Field(default="0x")
    jd_enhancement_private_key: str = Field(default="0x")
    talent_intelligence_private_key: str = Field(default="0x")
    github_source_private_key: str = Field(default="0x")

    # ─── Database ────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://hireflow:hireflow@localhost:5432/hireflow"
    )

    # ─── App ─────────────────────────────────────────────────
    environment: str = Field(default="testnet")
    log_level: str = Field(default="INFO")
    secret_key: str = Field(default="change-me-to-a-random-32-char-string")

    # ─── Derived / Computed ───────────────────────────────────
    @property
    def db_url(self) -> str:
        """DATABASE_URL with SSL appended for Render."""
        url = self.database_url
        if "render.com" in url and "ssl" not in url:
            url += "?ssl=require"
        return url

    @property
    def is_testnet(self) -> bool:
        return self.environment == "testnet"

    @property
    def circle_base_url(self) -> str:
        return "https://api.circle.com"

    @property
    def apollo_base_url(self) -> str:
        return "https://api.apollo.io"

    @property
    def github_base_url(self) -> str:
        return "https://api.github.com"

    @property
    def hunter_base_url(self) -> str:
        return "https://api.hunter.io"

    # Per-action USDC prices (in USDC units, 6 decimals stored as float for display)
    action_prices: dict = {
        "/apollo/search": 0.001,
        "/apollo/enrich": 0.003,
        "/github/profile": 0.001,
        "/github/repos": 0.001,
        "/hunter/find": 0.002,
        "/hunter/verify": 0.001,
        "/score/candidate": 0.003,
        "/jd/parse": 0.002,
        "/jd/enhance": 0.002,
        "/talent/intelligence": 0.005,
        "/github/repo_search": 0.001,
        "/github/repo_scan": 0.001,
    }


settings = Settings()
