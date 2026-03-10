"""
E2E Tests for Portkey SDK Instrumentation

Tests Portkey instrumentation. Requires PORTKEY_API_KEY.
"""

import pytest
import time

from config import config, skip_if_no_portkey, skip_if_no_google


@pytest.fixture(scope="module")
def portkey_client():
    """Create an instrumented Portkey client."""
    if not config.has_portkey():
        pytest.skip("PORTKEY_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_portkey import PortkeyInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_portkey not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    PortkeyInstrumentor().instrument(tracer_provider=tracer_provider)

    from portkey_ai import Portkey

    client = Portkey(api_key=config.portkey_api_key)

    yield client

    PortkeyInstrumentor().uninstrument()


@skip_if_no_portkey
class TestPortkeyChatCompletion:
    """Test Portkey chat completion."""

    def test_basic_chat(self, portkey_client):
        """Test basic chat completion via Portkey."""
        response = portkey_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Say hello in one word."}
            ],
            max_tokens=20,
        )

        assert response.choices[0].message.content is not None
        time.sleep(2)
        print(f"Response: {response.choices[0].message.content}")

    def test_streaming(self, portkey_client):
        """Test streaming via Portkey."""
        stream = portkey_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "Count from 1 to 3."}
            ],
            max_tokens=30,
            stream=True,
        )

        chunks = []
        for chunk in stream:
            if chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)

        full_response = "".join(chunks)
        assert len(full_response) > 0
        print(f"Streamed: {full_response}")


@skip_if_no_portkey
class TestPortkeyErrorHandling:
    """Test error handling."""

    def test_invalid_model(self, portkey_client):
        """Test handling of invalid model."""
        with pytest.raises(Exception):
            portkey_client.chat.completions.create(
                model="invalid-model-xyz",
                messages=[{"role": "user", "content": "test"}],
            )
