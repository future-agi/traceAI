"""AG2 Classic scenarios shared by the in-process tests and the contract app.

Every scenario talks to a :class:`FakeOpenAI` on 127.0.0.1. No vendor API.
"""

from __future__ import annotations

from typing import Any, Dict

SECRET_PROMPT = "PRIVATE-PROMPT-7f3a: what is the weather in Paris?"
TOOL_SECRET_CITY = "PRIVATE-CITY-91c2"


def _terminates(message: Dict[str, Any]) -> bool:
    return "TERMINATE" in str(message.get("content") or "")


def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return "sunny in {0}".format(city)


def broken_tool(city: str) -> str:
    """A tool that always fails."""
    raise RuntimeError("tool exploded for {0}".format(city))


def two_agent_tool_chat(tracing: Any, fake: Any, *, message: str = SECRET_PROMPT) -> Any:
    """user -> assistant, the assistant calls ``get_weather`` once, then stops."""
    from autogen import ConversableAgent

    assistant = ConversableAgent(
        "assistant",
        llm_config=fake.llm_config(),
        human_input_mode="NEVER",
        is_termination_msg=_terminates,
    )
    user = ConversableAgent(
        "user",
        llm_config=False,
        human_input_mode="NEVER",
        max_consecutive_auto_reply=3,
        is_termination_msg=_terminates,
    )
    assistant.register_for_llm(description="Get the weather")(get_weather)
    user.register_for_execution()(get_weather)
    tracing.instrument_agent(assistant)
    tracing.instrument_agent(user)
    return user.initiate_chat(assistant, message=message, max_turns=3, silent=True)


def broken_tool_chat(tracing: Any, fake: Any) -> Any:
    """The assistant calls a tool that raises; upstream catches it."""
    from autogen import ConversableAgent

    assistant = ConversableAgent(
        "assistant_b",
        llm_config=fake.llm_config(),
        human_input_mode="NEVER",
        is_termination_msg=_terminates,
    )
    user = ConversableAgent(
        "user_b",
        llm_config=False,
        human_input_mode="NEVER",
        max_consecutive_auto_reply=2,
        is_termination_msg=_terminates,
    )
    assistant.register_for_llm(description="Always fails")(broken_tool)
    user.register_for_execution()(broken_tool)
    tracing.instrument_agent(assistant)
    tracing.instrument_agent(user)
    return user.initiate_chat(assistant, message="call the broken tool", max_turns=2, silent=True)


def failing_llm_chat(tracing: Any, fake: Any) -> str:
    """The model endpoint rejects the request (HTTP 400); the error reaches the caller.

    The prompt also carries ``SECRET_PROMPT`` so the error path is checked for
    content leaks. Returns the exception class name the caller saw.
    """
    from autogen import ConversableAgent

    from _fake_openai import LLM_FAIL_TRIGGER

    assistant = ConversableAgent("assistant_f", llm_config=fake.llm_config(), human_input_mode="NEVER")
    user = ConversableAgent("user_f", llm_config=False, human_input_mode="NEVER", max_consecutive_auto_reply=1)
    tracing.instrument_agent(assistant)
    tracing.instrument_agent(user)
    try:
        user.initiate_chat(
            assistant, message="{0} {1}".format(LLM_FAIL_TRIGGER, SECRET_PROMPT), max_turns=1, silent=True
        )
    except Exception as exc:  # the caller sees the model error
        return type(exc).__name__
    raise AssertionError("the fake model was expected to reject the request")


def group_chat(tracing: Any, fake: Any, *, message: str = "Write a haiku about tracing") -> Any:
    """writer -> GroupChatManager with LLM ("auto") speaker selection."""
    from autogen import ConversableAgent
    from autogen.agentchat.groupchat import GroupChat, GroupChatManager

    writer = ConversableAgent("writer", llm_config=fake.llm_config(), human_input_mode="NEVER")
    critic = ConversableAgent("critic", llm_config=fake.llm_config(), human_input_mode="NEVER")
    groupchat = GroupChat(
        agents=[writer, critic],
        messages=[],
        max_round=2,
        speaker_selection_method="auto",
    )
    manager = GroupChatManager(groupchat=groupchat, llm_config=fake.llm_config())
    tracing.instrument_agent(manager)
    return writer.initiate_chat(manager, message=message, max_turns=1, silent=True)
