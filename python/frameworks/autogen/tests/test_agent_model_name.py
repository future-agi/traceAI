"""Agent spans name the model that current autogen-ext model clients request.

autogen-ext 0.7.5 model clients have no ``model`` attribute. They keep the
requested model in ``_create_args`` and their constructor arguments, API key
included, in ``_raw_config``. The fakes below copy only that storage, because
autogen-ext itself is not a test dependency of this package.
"""

from types import SimpleNamespace

import pytest
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient, CreateResult, RequestUsage

from traceai_autogen._v04_wrapper import extract_agent_info

from .test_sdk_contract import spans

API_KEY = "sk-test-PLACEHOLDER"
MODEL_INFO = {"vision": False, "function_calling": False, "json_output": False,
              "family": "unknown", "structured_output": False}

# OpenAIChatCompletionClient and AzureOpenAIChatCompletionClient. The resolved
# alias is used only for a model-mismatch warning; every request is sent with "gpt-4o".
OPENAI = {
    "_create_args": {"model": "gpt-4o"},
    "_raw_config": {"model": "gpt-4o", "api_key": API_KEY},
    "_resolved_model": "gpt-4o-2024-08-06",
}
# Only the constructor config names the model, next to the key.
CONSTRUCTOR_CONFIG_ONLY = {
    "_raw_config": {"model": "gpt-4o-mini", "api_key": API_KEY},
}


def agent_with(client):
    return SimpleNamespace(name="assistant", _model_client=client)


@pytest.mark.parametrize("storage,expected", [
    pytest.param(OPENAI, "gpt-4o", id="openai"),
    pytest.param({
        "_create_args": {"model": "claude-3-5-sonnet-20241022"},
        "_raw_config": {"model": "claude-3-5-sonnet-20241022", "api_key": API_KEY},
    }, "claude-3-5-sonnet-20241022", id="anthropic"),
    pytest.param({
        "_model_name": "llama3.2",
        "_resolved_model": "llama3.2",
        "_create_args": {"model": "llama3.2"},
    }, "llama3.2", id="ollama"),
    pytest.param(CONSTRUCTOR_CONFIG_ONLY, "gpt-4o-mini", id="constructor-config-only"),
])
def test_current_clients_report_requested_model_and_never_the_key(storage, expected):
    info = extract_agent_info(agent_with(SimpleNamespace(**storage)))
    assert info.get("model_name") == expected
    assert API_KEY not in repr(info)


@pytest.mark.parametrize("attribute", ["model", "_model"])
def test_clients_exposing_a_model_attribute_keep_working(attribute):
    client = SimpleNamespace(**{attribute: "gpt-4"})
    assert extract_agent_info(agent_with(client))["model_name"] == "gpt-4"


@pytest.mark.parametrize("client", [
    # ReplayChatCompletionClient keeps model info but no model name.
    pytest.param(SimpleNamespace(_model_info=dict(MODEL_INFO)), id="replay"),
    pytest.param(SimpleNamespace(_create_args={"temperature": 0}), id="create-args-without-model"),
])
def test_client_without_a_model_name_adds_none(client):
    assert "model_name" not in extract_agent_info(agent_with(client))


class CurrentShapeClient(ChatCompletionClient):
    """Offline client that stores its model the way autogen-ext 0.7.5 does."""

    def __init__(self, **storage):
        for name, value in storage.items():
            setattr(self, name, value)

    async def create(self, messages, **kwargs):
        return CreateResult(
            content="offline reply", finish_reason="stop", cached=False,
            usage=RequestUsage(prompt_tokens=7, completion_tokens=3),
        )

    async def create_stream(self, messages, **kwargs):
        yield await self.create(messages, **kwargs)

    async def close(self):
        pass

    def actual_usage(self):
        return RequestUsage(prompt_tokens=7, completion_tokens=3)

    total_usage = actual_usage

    def count_tokens(self, messages, **kwargs):
        return 7

    def remaining_tokens(self, messages, **kwargs):
        return 100

    @property
    def model_info(self):
        return dict(MODEL_INFO)

    @property
    def capabilities(self):
        return self.model_info


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize("storage,expected", [
    pytest.param(OPENAI, "gpt-4o", id="openai"),
    pytest.param(CONSTRUCTOR_CONFIG_ONLY, "gpt-4o-mini", id="constructor-config-only"),
])
async def test_real_agent_spans_carry_requested_model_and_never_the_key(spans, storage, expected, streaming):
    exporter = spans[0]
    assistant = AssistantAgent("assistant", model_client=CurrentShapeClient(**storage))
    messages = [TextMessage(content="hello", source="user")]
    if streaming:
        items = [item async for item in assistant.on_messages_stream(messages, CancellationToken())]
        response = items[-1]
    else:
        response = await assistant.on_messages(messages, CancellationToken())
    assert isinstance(response, Response)
    assert response.chat_message.content == "offline reply"
    finished = exporter.get_finished_spans()
    agents = [span for span in finished if span.name.startswith("autogen.agent.")]
    # on_messages delegates to on_messages_stream, so a call records both.
    assert len(agents) == (1 if streaming else 2)
    for span in agents:
        assert span.attributes.get("gen_ai.request.model") == expected
    for span in finished:
        assert API_KEY not in span.to_json()
