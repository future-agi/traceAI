"""Opt-in live check: a real LangChain agent (real model call, one tool) plus a failing run.

Run by hand against a Future AGI collector, with the same environment as the
README. Needs an OpenAI-compatible endpoint (OPENAI_API_KEY / OPENAI_BASE_URL)
and `pip install langchain langchain-openai`. Not run in CI.

Usage: python e2e/real_langchain_run.py [ok|fail|both]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import langsmith  # noqa: E402
from app import init_tracing  # noqa: E402
from langchain.agents import create_agent  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

MODEL = os.environ.get("E2E_MODEL", "gpt-5.6-terra")


@tool
def lookup_policy(topic: str) -> str:
    """Return the refund policy for a topic."""
    return "Refunds are accepted within 30 days of purchase."


@tool
def broken_lookup(topic: str) -> str:
    """Look up a policy (always fails)."""
    raise RuntimeError("policy backend unavailable")


def run(agent_tools, question: str) -> None:
    agent = create_agent(ChatOpenAI(model=MODEL, temperature=0), tools=agent_tools)
    try:
        out = agent.invoke({"messages": [{"role": "user", "content": question}]})
        print(out["messages"][-1].content)
    except Exception as exc:  # the failing tool run is exported with ERROR status
        print("run failed:", type(exc).__name__)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    provider = init_tracing()
    client = langsmith.Client()
    langsmith.configure(client=client)
    if mode in ("ok", "both"):
        run([lookup_policy], os.environ.get("E2E_QUESTION", "Use the lookup_policy tool for 'refunds' and tell me the refund window."))
    if mode in ("fail", "both"):
        run([broken_lookup], "Use the broken_lookup tool for 'refunds'.")
    client.flush()
    provider.shutdown()


if __name__ == "__main__":
    main()
