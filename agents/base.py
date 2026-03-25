"""
Base agent factory for Kimi K2.5 (ChatNVIDIA) agents.
All data collection agents (JD Parser, Apollo, GitHub, Hunter, Orchestrator) use this.
Only the Scoring Agent uses Claude Sonnet 4.5 (see scoring_agent.py).
"""

from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langgraph.prebuilt import create_react_agent

from settings import settings


KIMI_MODEL = "moonshotai/kimi-k2.5"


def create_kimi_agent(tools: list, system_prompt: str, timeout: int | None = None):
    """
    Factory: creates a LangGraph ReAct agent backed by Kimi K2.5 via NVIDIA NIM.

    Args:
        tools:         List of LangChain tools the agent can call
        system_prompt: System-level instructions injected as state_modifier
        timeout:       HTTP request timeout in seconds (None = library default ~300s)

    Returns:
        Compiled LangGraph agent (CompiledGraph)
    """
    llm_kwargs: dict = {
        "model": KIMI_MODEL,
        "api_key": settings.nvidia_api_key,
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    if timeout is not None:
        llm_kwargs["timeout"] = timeout

    llm = ChatNVIDIA(**llm_kwargs)
    return create_react_agent(
        model=llm,
        tools=tools,
        state_modifier=system_prompt,
    )
