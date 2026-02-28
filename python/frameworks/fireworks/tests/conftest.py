"""Pytest fixtures for Fireworks tests."""

from unittest.mock import MagicMock, PropertyMock
import pytest


@pytest.fixture
def mock_openai_response():
    """Create a mock OpenAI-compatible response."""
    response = MagicMock()
    response.id = "chatcmpl-123"
    response.model = "accounts/fireworks/models/llama-v3p1-8b-instruct"
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.role = "assistant"
    response.choices[0].message.content = "Hello! How can I help you?"
    response.choices[0].finish_reason = "stop"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 8
    response.usage.total_tokens = 18
    response.model_dump = MagicMock(return_value={
        "id": "chatcmpl-123",
        "model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "choices": [{"message": {"role": "assistant", "content": "Hello! How can I help you?"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    })
    return response


@pytest.fixture
def mock_stream_chunks():
    """Create mock streaming chunks."""
    chunks = []
    for i, content in enumerate(["Hello", "!", " How", " can", " I", " help", "?"]):
        chunk = MagicMock()
        chunk.id = "chatcmpl-123"
        chunk.model = "accounts/fireworks/models/llama-v3p1-8b-instruct"
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta = MagicMock()
        chunk.choices[0].delta.content = content
        chunk.choices[0].finish_reason = None if i < 6 else "stop"
        chunk.usage = None
        chunk.model_dump = MagicMock(return_value={"id": f"chunk-{i}", "content": content})
        chunks.append(chunk)

    # Last chunk has usage
    chunks[-1].usage = MagicMock()
    chunks[-1].usage.prompt_tokens = 10
    chunks[-1].usage.completion_tokens = 7
    chunks[-1].usage.total_tokens = 17
    return chunks


@pytest.fixture
def mock_fireworks_client():
    """Create a mock client configured for Fireworks."""
    client = MagicMock()
    client._client = MagicMock()
    type(client._client).base_url = PropertyMock(return_value="https://api.fireworks.ai/inference/v1")
    return client


@pytest.fixture
def mock_non_fireworks_client():
    """Create a mock client NOT configured for Fireworks."""
    client = MagicMock()
    client._client = MagicMock()
    type(client._client).base_url = PropertyMock(return_value="https://api.openai.com/v1")
    return client
