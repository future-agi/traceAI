"""Real-world scenario tests for AutoGen instrumentation.

These tests verify the instrumentation works correctly in realistic use cases
without requiring actual AutoGen or API calls.
"""

import asyncio
import json
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from traceai_autogen._v04_wrapper import (
    safe_serialize,
    extract_agent_info,
    extract_team_info,
    extract_message_info,
    extract_usage_from_result,
    wrap_agent_on_messages,
    wrap_team_run,
    wrap_tool_execution,
)
from traceai_autogen._attributes import (
    AutoGenSpanKind,
    AutoGenAttributes,
    get_model_provider,
)


class TestChatbotScenario:
    """Test scenarios for a simple chatbot."""

    def test_single_turn_conversation(self):
        """Test a single question-answer exchange."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def chatbot_response(self, messages, cancellation_token=None):
            # Simulate processing time
            response = MagicMock()
            response.chat_message = MagicMock()
            response.chat_message.content = "Paris is the capital of France."
            return response

        wrapped = wrap_agent_on_messages(chatbot_response, tracer)

        agent = MagicMock()
        agent.name = "chatbot"
        type(agent).__name__ = "AssistantAgent"
        agent._model_client = MagicMock()
        agent._model_client.model = "gpt-4o"
        agent._tools = []

        message = MagicMock()
        message.content = "What is the capital of France?"
        message.source = "user"

        async def run_test():
            result = await wrapped(agent, [message])
            return result

        result = asyncio.run(run_test())

        assert result.chat_message.content == "Paris is the capital of France."
        tracer.start_as_current_span.assert_called_once()

    def test_multi_turn_conversation(self):
        """Test a multi-turn conversation with history."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        conversation_state = {"turn": 0}

        async def chatbot_response(self, messages, cancellation_token=None):
            conversation_state["turn"] += 1
            response = MagicMock()
            response.chat_message = MagicMock()
            response.chat_message.content = f"Response for turn {conversation_state['turn']}"
            return response

        wrapped = wrap_agent_on_messages(chatbot_response, tracer)

        agent = MagicMock()
        agent.name = "chatbot"
        type(agent).__name__ = "AssistantAgent"
        agent._model_client = None
        agent._tools = []

        async def run_test():
            # Turn 1
            msg1 = MagicMock(content="Hello", source="user")
            result1 = await wrapped(agent, [msg1])

            # Turn 2 (with history)
            msg2 = MagicMock(content="How are you?", source="user")
            result2 = await wrapped(agent, [msg1, result1.chat_message, msg2])

            # Turn 3
            msg3 = MagicMock(content="Goodbye", source="user")
            result3 = await wrapped(agent, [msg1, result1.chat_message, msg2, result2.chat_message, msg3])

            return [result1, result2, result3]

        results = asyncio.run(run_test())

        assert len(results) == 3
        assert conversation_state["turn"] == 3
        assert tracer.start_as_current_span.call_count == 3


class TestTeamCollaborationScenario:
    """Test scenarios for multi-agent team collaboration."""

    def test_round_robin_team(self):
        """Test round-robin team collaboration."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def team_run(self, task, *args, **kwargs):
            # Simulate team discussion
            result = MagicMock()
            result.messages = [
                MagicMock(content="I'll start with the architecture.", source="architect"),
                MagicMock(content="Good plan. I'll implement it.", source="developer"),
                MagicMock(content="I'll write tests for it.", source="tester"),
            ]
            result.stop_reason = "max_messages"
            return result

        wrapped = wrap_team_run(team_run, tracer, "run")

        # Create proper participant mocks with string names
        architect = MagicMock()
        architect.name = "architect"
        developer = MagicMock()
        developer.name = "developer"
        tester = MagicMock()
        tester.name = "tester"

        team = MagicMock()
        type(team).__name__ = "RoundRobinGroupChat"
        team._participants = [architect, developer, tester]
        team._termination_condition = MagicMock()
        type(team._termination_condition).__name__ = "MaxMessageTermination"
        team._max_turns = 10

        async def run_test():
            result = await wrapped(team, "Build a REST API")
            return result

        result = asyncio.run(run_test())

        assert len(result.messages) == 3
        assert result.stop_reason == "max_messages"
        tracer.start_as_current_span.assert_called_once()

    def test_selector_team_routing(self):
        """Test selector-based team routing."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def team_run(self, task, *args, **kwargs):
            # Selector chose the right agent
            result = MagicMock()
            result.messages = [
                MagicMock(content="This is a frontend question. Let me handle it.", source="frontend_dev"),
                MagicMock(content="Here's the React component you need...", source="frontend_dev"),
            ]
            result.stop_reason = "TextMentionTermination"
            return result

        wrapped = wrap_team_run(team_run, tracer, "run")

        # Create proper participant mocks with string names
        backend = MagicMock()
        backend.name = "backend_dev"
        frontend = MagicMock()
        frontend.name = "frontend_dev"
        devops = MagicMock()
        devops.name = "devops"

        team = MagicMock()
        type(team).__name__ = "SelectorGroupChat"
        team._participants = [backend, frontend, devops]
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            result = await wrapped(team, "Create a React component for user profile")
            return result

        result = asyncio.run(run_test())

        # Frontend dev handled it
        assert all(msg.source == "frontend_dev" for msg in result.messages)


class TestToolUsageScenario:
    """Test scenarios for agent tool usage."""

    def test_weather_tool(self):
        """Test weather lookup tool."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def get_weather(city: str) -> str:
            weather_data = {"new york": "72°F, sunny", "london": "58°F, cloudy"}
            return weather_data.get(city.lower(), "Unknown city")

        wrapped = wrap_tool_execution(get_weather, tracer, "get_weather", "Get weather for a city")

        result = wrapped("New York")

        assert result == "72°F, sunny"
        tracer.start_as_current_span.assert_called_once()

    def test_database_query_tool(self):
        """Test database query tool."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def search_database(query: str, limit: int = 10) -> str:
            results = [
                {"id": 1, "name": "Product A", "price": 29.99},
                {"id": 2, "name": "Product B", "price": 49.99},
            ]
            return json.dumps(results[:limit])

        wrapped = wrap_tool_execution(search_database, tracer, "search_database", "Search product database")

        result = wrapped("electronics", limit=5)

        parsed = json.loads(result)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "Product A"

    def test_async_api_tool(self):
        """Test async API call tool."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def fetch_user(user_id: str) -> str:
            # Simulate API call
            await asyncio.sleep(0.01)
            return json.dumps({"id": user_id, "name": "John Doe", "email": "john@example.com"})

        wrapped = wrap_tool_execution(fetch_user, tracer, "fetch_user", "Fetch user from API")

        async def run_test():
            result = await wrapped("user123")
            return result

        result = asyncio.run(run_test())
        parsed = json.loads(result)

        assert parsed["id"] == "user123"
        assert parsed["name"] == "John Doe"

    def test_tool_error_handling(self):
        """Test tool error handling."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def divide(a: int, b: int) -> float:
            return a / b

        wrapped = wrap_tool_execution(divide, tracer, "divide", "Divide two numbers")

        with pytest.raises(ZeroDivisionError):
            wrapped(10, 0)

        # Span should still have been created
        tracer.start_as_current_span.assert_called_once()


class TestRAGPipelineScenario:
    """Test scenarios for RAG (Retrieval-Augmented Generation) pipelines."""

    def test_document_retrieval_and_synthesis(self):
        """Test RAG pipeline with document retrieval."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        # Simulated document store
        documents = {
            "doc1": "Python is a programming language known for its simplicity.",
            "doc2": "Machine learning uses algorithms to learn from data.",
        }

        def search_docs(query: str) -> str:
            results = []
            for doc_id, content in documents.items():
                if any(word in content.lower() for word in query.lower().split()):
                    results.append({"id": doc_id, "content": content[:100]})
            return json.dumps(results)

        wrapped_search = wrap_tool_execution(search_docs, tracer, "search_docs", "Search documents")

        # First, retrieve documents
        search_result = wrapped_search("Python programming")
        docs = json.loads(search_result)

        assert len(docs) == 1
        assert docs[0]["id"] == "doc1"

    def test_rag_with_reranking(self):
        """Test RAG with document reranking."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def rerank_documents(query: str, documents: str) -> str:
            docs = json.loads(documents)
            # Simple reranking by relevance score
            for doc in docs:
                doc["score"] = len(set(query.lower().split()) & set(doc.get("content", "").lower().split()))
            docs.sort(key=lambda x: x["score"], reverse=True)
            return json.dumps(docs)

        wrapped = wrap_tool_execution(rerank_documents, tracer, "rerank", "Rerank documents by relevance")

        docs_input = json.dumps([
            {"id": "doc1", "content": "Python is great for AI"},  # score: 2 (python, ai)
            {"id": "doc2", "content": "Java is used in enterprise"},  # score: 0
            {"id": "doc3", "content": "Python AI machine learning"},  # score: 2 (python, ai)
        ])

        result = wrapped("Python AI", docs_input)
        reranked = json.loads(result)

        # doc1 and doc3 both have score 2, but doc1 comes first (stable sort)
        # Just check that Java doc is not first
        assert reranked[2]["id"] == "doc2"
        # And that the top result has high relevance (contains both Python and AI)
        assert reranked[0]["score"] == 2


class TestCustomerSupportScenario:
    """Test scenarios for customer support systems."""

    def test_ticket_creation_and_routing(self):
        """Test support ticket creation and routing."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def create_ticket(customer_id: str, issue: str, priority: str = "medium") -> str:
            return json.dumps({
                "ticket_id": f"TKT-{hash(issue) % 10000:04d}",
                "customer_id": customer_id,
                "priority": priority,
                "status": "created",
            })

        wrapped = wrap_tool_execution(create_ticket, tracer, "create_ticket", "Create support ticket")

        result = wrapped("C001", "Cannot login to account", "high")
        ticket = json.loads(result)

        assert ticket["customer_id"] == "C001"
        assert ticket["priority"] == "high"
        assert ticket["status"] == "created"

    def test_escalation_workflow(self):
        """Test ticket escalation workflow."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def team_run(self, task, *args, **kwargs):
            result = MagicMock()
            result.messages = [
                MagicMock(content="I'll check the customer account.", source="tier1_support"),
                MagicMock(content="This is a complex technical issue. Escalating...", source="tier1_support"),
                MagicMock(content="I'll investigate the backend systems.", source="tier2_support"),
                MagicMock(content="Found the issue. RESOLVED.", source="tier2_support"),
            ]
            result.stop_reason = "TextMentionTermination"
            return result

        wrapped = wrap_team_run(team_run, tracer, "run")

        team = MagicMock()
        type(team).__name__ = "SelectorGroupChat"
        tier1 = MagicMock()
        tier1.name = "tier1_support"
        tier2 = MagicMock()
        tier2.name = "tier2_support"
        team._participants = [tier1, tier2]
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            return await wrapped(team, "Customer C001 cannot access premium features")

        result = asyncio.run(run_test())

        # Should have messages from both tiers
        sources = [msg.source for msg in result.messages]
        assert "tier1_support" in sources
        assert "tier2_support" in sources


class TestCodeReviewScenario:
    """Test scenarios for code review workflows."""

    def test_code_analysis_tools(self):
        """Test code analysis tool chain."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        def analyze_style(code: str) -> str:
            issues = []
            lines = code.split("\n")
            for i, line in enumerate(lines, 1):
                if len(line) > 79:
                    issues.append(f"Line {i}: too long")
            return json.dumps({"issues": issues, "score": 100 - len(issues) * 10})

        def check_security(code: str) -> str:
            vulnerabilities = []
            if "eval(" in code:
                vulnerabilities.append({"severity": "HIGH", "issue": "eval() usage"})
            return json.dumps({"vulnerabilities": vulnerabilities})

        wrapped_style = wrap_tool_execution(analyze_style, tracer, "analyze_style", "Analyze code style")
        wrapped_security = wrap_tool_execution(check_security, tracer, "check_security", "Check security")

        code = "x = eval(input())\nprint(x)"

        style_result = wrapped_style(code)
        security_result = wrapped_security(code)

        style_data = json.loads(style_result)
        security_data = json.loads(security_result)

        assert style_data["score"] == 100  # No style issues
        assert len(security_data["vulnerabilities"]) == 1
        assert security_data["vulnerabilities"][0]["severity"] == "HIGH"


class TestTokenUsageTracking:
    """Test scenarios for token usage tracking."""

    def test_extract_usage_from_team_result(self):
        """Test extracting token usage from team results."""
        # Create result with usage info
        usage1 = MagicMock()
        usage1.prompt_tokens = 500
        usage1.completion_tokens = 200

        usage2 = MagicMock()
        usage2.prompt_tokens = 300
        usage2.completion_tokens = 150

        msg1 = MagicMock()
        msg1.models_usage = usage1

        msg2 = MagicMock()
        msg2.models_usage = usage2

        result = MagicMock()
        result.messages = [msg1, msg2]

        usage = extract_usage_from_result(result)

        assert usage is not None
        assert usage["input_tokens"] == 800  # 500 + 300
        assert usage["output_tokens"] == 350  # 200 + 150
        assert usage["total_tokens"] == 1150


class TestModelProviderDetection:
    """Test model provider detection in different scenarios."""

    def test_detect_openai_models(self):
        """Test OpenAI model detection."""
        assert get_model_provider("gpt-4") == "openai"
        assert get_model_provider("gpt-4o") == "openai"
        assert get_model_provider("gpt-4o-mini") == "openai"
        assert get_model_provider("gpt-3.5-turbo") == "openai"
        assert get_model_provider("o1-preview") == "openai"
        assert get_model_provider("o1-mini") == "openai"

    def test_detect_anthropic_models(self):
        """Test Anthropic model detection."""
        assert get_model_provider("claude-3-opus") == "anthropic"
        assert get_model_provider("claude-3-sonnet") == "anthropic"
        assert get_model_provider("claude-3.5-sonnet") == "anthropic"
        assert get_model_provider("claude-3-haiku") == "anthropic"

    def test_detect_google_models(self):
        """Test Google model detection."""
        assert get_model_provider("gemini-pro") == "google"
        assert get_model_provider("gemini-1.5-flash") == "google"
        assert get_model_provider("gemini-1.5-pro") == "google"

    def test_detect_other_models(self):
        """Test other model providers."""
        assert get_model_provider("mistral-large") == "mistral"
        assert get_model_provider("deepseek-coder") == "deepseek"
        assert get_model_provider("ollama/llama2") == "ollama"


class TestErrorRecoveryScenario:
    """Test error recovery scenarios."""

    def test_agent_recovers_from_tool_error(self):
        """Test agent recovering from tool errors."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        call_count = {"count": 0}

        def flaky_tool(x: int) -> str:
            call_count["count"] += 1
            if call_count["count"] < 2:
                raise RuntimeError("Temporary error")
            return f"Success: {x}"

        wrapped = wrap_tool_execution(flaky_tool, tracer, "flaky_tool", "A flaky tool")

        # First call fails
        with pytest.raises(RuntimeError):
            wrapped(1)

        # Second call succeeds
        result = wrapped(2)
        assert result == "Success: 2"

        assert call_count["count"] == 2
        assert tracer.start_as_current_span.call_count == 2

    def test_team_handles_agent_failure(self):
        """Test team handling when an agent fails."""
        tracer = MagicMock()
        span = MagicMock()
        span.__enter__ = MagicMock(return_value=span)
        span.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span = MagicMock(return_value=span)

        async def team_run_with_error(self, task, *args, **kwargs):
            raise ConnectionError("Lost connection to model provider")

        wrapped = wrap_team_run(team_run_with_error, tracer, "run")

        team = MagicMock()
        type(team).__name__ = "RoundRobinGroupChat"
        team._participants = []
        team._termination_condition = None
        team._max_turns = None

        async def run_test():
            await wrapped(team, "Some task")

        with pytest.raises(ConnectionError, match="Lost connection"):
            asyncio.run(run_test())

        # Span should still have been created
        tracer.start_as_current_span.assert_called_once()
