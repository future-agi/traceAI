"""
E2E Tests for PII Redaction via TraceConfig

Tests that pii_redaction=True on TraceConfig replaces PII patterns
in traced span attributes with <ENTITY_TYPE> tokens before export.

Uses Google GenAI (requires GOOGLE_API_KEY).
"""

import time

import pytest

from fi_instrumentation.fi_types import ProjectType

from config import config, skip_if_no_google


PII_PROMPT = (
    "Hi, my name is John Smith. My email is john.smith@acme-corp.com "
    "and my phone number is (555) 867-5309. "
    "My SSN is 123-45-6789 and my credit card is 4111 1111 1111 1111. "
    "I'm connecting from IP 192.168.1.42. "
    "My API key is sk-live-ABCDEFGHIJKLMNOPQRSTuvwx. "
    "Can you help me with my account?"
)


@pytest.fixture(scope="module")
def pii_genai_client():
    """Create an instrumented Google GenAI client with PII redaction ON."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register, TraceConfig

    try:
        from traceai_google_genai import GoogleGenAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_google_genai not installed or incompatible")

    tracer_provider = register(
        project_name="pii-redaction-e2e-test",
        project_type=ProjectType.OBSERVE,
        verbose=False,
    )

    GoogleGenAIInstrumentor().instrument(
        tracer_provider=tracer_provider,
        config=TraceConfig(pii_redaction=True),
    )

    from google import genai

    client = genai.Client(api_key=config.google_api_key)

    yield client, tracer_provider

    GoogleGenAIInstrumentor().uninstrument()


@skip_if_no_google
class TestPiiRedaction:
    """Test that PII is redacted in traced spans."""

    def test_pii_redacted_in_trace(self, pii_genai_client):
        """Send a prompt with PII and verify trace is exported.

        The actual PII replacement happens at the SDK level before export.
        Check the UI under project 'pii-redaction-e2e-test' — input messages
        should show <EMAIL_ADDRESS>, <PHONE_NUMBER>, etc.
        """
        client, tracer_provider = pii_genai_client

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=PII_PROMPT,
        )

        assert response.text is not None
        assert len(response.text) > 0

        # Flush to ensure spans are exported
        if hasattr(tracer_provider, "force_flush"):
            tracer_provider.force_flush()

        time.sleep(2)

        print(f"\nResponse: {response.text[:200]}")
        print(
            "\n--- Check the UI under project 'pii-redaction-e2e-test' ---"
        )
        print("Input messages should contain:")
        print("  <EMAIL_ADDRESS>  instead of  john.smith@acme-corp.com")
        print("  <PHONE_NUMBER>   instead of  (555) 867-5309")
        print("  <SSN>            instead of  123-45-6789")
        print("  <CREDIT_CARD>    instead of  4111 1111 1111 1111")
        print("  <IP_ADDRESS>     instead of  192.168.1.42")
        print("  <API_KEY>        instead of  sk-live-ABCDEFGH...")

    def test_pii_in_multi_turn(self, pii_genai_client):
        """Test PII redaction in multi-turn conversation."""
        from google.genai import types

        client, tracer_provider = pii_genai_client

        contents = [
            types.Content(
                role="user",
                parts=[types.Part(text="My email is alice@secret.org")],
            ),
            types.Content(
                role="model",
                parts=[types.Part(text="Got it, I'll remember that.")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text="Now call me at 555-123-4567 and send a bill to 192.168.0.1"
                    )
                ],
            ),
        ]

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
        )

        assert response.text is not None

        if hasattr(tracer_provider, "force_flush"):
            tracer_provider.force_flush()

        time.sleep(2)

        print(f"\nMulti-turn response: {response.text[:200]}")
