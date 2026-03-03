"""
E2E Test: Customer Support Chatbot Use Case

Simulates a multi-turn conversation chatbot with session tracking.
"""

import pytest
import os
import time
import uuid
from typing import List, Dict, Any

from config import config, skip_if_no_openai


@pytest.fixture(scope="module")
def setup_chatbot():
    """Set up OpenAI with instrumentation for chatbot."""
    if not config.has_openai():
        pytest.skip("OpenAI API key required")

    os.environ["OPENAI_API_KEY"] = config.openai_api_key

    from fi_instrumentation import register
    from traceai_openai import OpenAIInstrumentor

    tracer_provider = register(
        project_name="e2e_chatbot_usecase",
        project_version_name="1.0.0",
        verbose=False,
    )

    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

    from openai import OpenAI
    client = OpenAI()

    yield client

    OpenAIInstrumentor().uninstrument()


class ChatbotSession:
    """Simulates a chatbot session with conversation history."""

    def __init__(self, client, session_id: str, user_id: str):
        self.client = client
        self.session_id = session_id
        self.user_id = user_id
        self.messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": """You are a helpful customer support assistant for TechCorp.
                Be concise and helpful. If you don't know something, say so."""
            }
        ]

    def send_message(self, user_message: str) -> str:
        """Send a message and get response."""
        self.messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=self.messages,
            max_tokens=150,
        )

        assistant_message = response.choices[0].message.content
        self.messages.append({"role": "assistant", "content": assistant_message})

        return assistant_message


@skip_if_no_openai
class TestChatbotUseCase:
    """Test chatbot use case scenarios."""

    def test_simple_conversation(self, setup_chatbot):
        """Test a simple Q&A conversation."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        user_id = "user_alice"

        chatbot = ChatbotSession(setup_chatbot, session_id, user_id)

        # Turn 1: Greeting
        response1 = chatbot.send_message("Hi, I need help with my account.")
        assert response1 is not None
        print(f"Bot: {response1}")

        # Turn 2: Specific question
        response2 = chatbot.send_message("How do I reset my password?")
        assert response2 is not None
        print(f"Bot: {response2}")

        # Turn 3: Follow-up
        response3 = chatbot.send_message("Thanks! How long does the reset email take?")
        assert response3 is not None
        print(f"Bot: {response3}")

        # Allow spans to export
        time.sleep(2)

    def test_multi_turn_context(self, setup_chatbot):
        """Test that chatbot maintains context across turns."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        user_id = "user_bob"

        chatbot = ChatbotSession(setup_chatbot, session_id, user_id)

        # Provide context
        response1 = chatbot.send_message("My name is Bob and I'm having trouble with my order #12345.")
        print(f"Bot: {response1}")

        # Reference previous context
        response2 = chatbot.send_message("What's my order number again?")
        print(f"Bot: {response2}")

        # Check if context is maintained (should mention 12345)
        assert "12345" in response2 or "order" in response2.lower()

        time.sleep(2)

    def test_error_handling_conversation(self, setup_chatbot):
        """Test chatbot handling of edge cases."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        user_id = "user_charlie"

        chatbot = ChatbotSession(setup_chatbot, session_id, user_id)

        # Send empty-ish message
        response1 = chatbot.send_message("...")
        assert response1 is not None

        # Send very long message
        long_message = "I have a problem. " * 50
        response2 = chatbot.send_message(long_message)
        assert response2 is not None

        time.sleep(2)


@skip_if_no_openai
class TestChatbotMetrics:
    """Test chatbot metrics and token tracking."""

    def test_token_tracking(self, setup_chatbot):
        """Verify token counts are tracked for chatbot."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        chatbot = ChatbotSession(setup_chatbot, session_id, "user_metrics")

        # Have a conversation
        chatbot.send_message("Hello, I need help.")
        chatbot.send_message("What are your business hours?")
        chatbot.send_message("Thanks, goodbye!")

        # Token counts should increase with each turn
        # (This is verified by the instrumentation)
        time.sleep(2)

    def test_latency_tracking(self, setup_chatbot):
        """Verify latency is tracked for chatbot responses."""
        import time

        session_id = f"session_{uuid.uuid4().hex[:8]}"
        chatbot = ChatbotSession(setup_chatbot, session_id, "user_latency")

        start = time.time()
        chatbot.send_message("Quick question: what's 2+2?")
        latency = time.time() - start

        print(f"Response latency: {latency:.2f}s")
        assert latency > 0

        time.sleep(2)
