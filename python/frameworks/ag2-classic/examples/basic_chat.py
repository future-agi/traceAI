"""Two-agent AG2 Classic chat with a tool, traced to Future AGI.

Requires FI_API_KEY, FI_SECRET_KEY and OPENAI_API_KEY in the environment.
This example calls a real model; the package tests use a loopback fake instead.
"""

import os

from autogen import ConversableAgent
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

from traceai_ag2_classic import setup


def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"sunny in {city}"


def main() -> None:
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name=os.getenv("FI_PROJECT_NAME", "ag2-classic-example"),
    )

    llm_config = {"config_list": [{"model": "gpt-4o-mini", "api_key": os.environ["OPENAI_API_KEY"]}]}
    assistant = ConversableAgent(
        "assistant",
        llm_config=llm_config,
        human_input_mode="NEVER",
        is_termination_msg=lambda m: "TERMINATE" in (m.get("content") or ""),
        system_message="Use the weather tool, answer, then say TERMINATE.",
    )
    user = ConversableAgent("user", llm_config=False, human_input_mode="NEVER", max_consecutive_auto_reply=3)
    assistant.register_for_llm(description="Get the weather")(get_weather)
    user.register_for_execution()(get_weather)

    # Content (messages, tool arguments/results) is off unless capture_content=True.
    setup(tracer_provider=trace_provider, agents=[assistant, user])

    user.initiate_chat(assistant, message="What is the weather in Paris?", max_turns=3)
    trace_provider.force_flush()


if __name__ == "__main__":
    main()
