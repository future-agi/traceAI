from datetime import date
from typing import Optional

import requests
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from langchain import agents
from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from traceai_langchain import LangChainInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]


def setup_instrumentation():
    """Configure and initialize OpenTelemetry instrumentation."""
    trace_provider = register(
        project_name="LANGCHAIN_TEST",
        project_version_name="V4",
        eval_tags=eval_tags,
    )
    LangChainInstrumentor().instrument(tracer_provider=trace_provider)
    return trace_provider


@tool
def get_exchange_rate(
    currency_from: str = "USD",
    currency_to: str = "EUR",
    currency_date: str = "latest",
) -> dict:
    """Retrieves the exchange rate between two currencies on a specified date.

    Args:
        currency_from (str): Source currency code (default: USD)
        currency_to (str): Target currency code (default: EUR)
        currency_date (str): Date for exchange rate (default: latest)

    Returns:
        dict: Exchange rate data from Frankfurter API
    """
    url = f"https://api.frankfurter.app/{currency_date}"
    params = {"from": currency_from, "to": currency_to}
    response = requests.get(url, params=params)
    response.raise_for_status()  # Raise exception for bad status codes
    return response.json()


def create_agent(tools: list) -> agents.AgentExecutor:
    """Create and configure the LangChain agent.

    Args:
        tools (list): List of tools available to the agent

    Returns:
        agents.AgentExecutor: Configured agent executor
    """
    llm = ChatOpenAI()
    prompt = ChatPromptTemplate.from_messages(
        [
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    agent = agents.create_tool_calling_agent(llm, tools, prompt)
    return agents.AgentExecutor(agent=agent, tools=tools, verbose=True)


def main():
    """Main execution function."""
    setup_instrumentation()
    tools = [get_exchange_rate]
    agent_executor = create_agent(tools)

    result = agent_executor.invoke(
        {
            "input": "What is the exchange rate from US dollars to Swedish currency today?"
        }
    )
    return result


if __name__ == "__main__":
    main()
