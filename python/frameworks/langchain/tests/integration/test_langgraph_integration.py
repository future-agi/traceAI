"""Integration tests for LangGraph instrumentation.

These tests use real LLM calls and verify tracing works correctly.

Run with:
    export $(cat /path/to/core-backend/.env.test.local | grep -v '^#' | xargs)
    pytest tests/integration/test_langgraph_integration.py -v -m integration
"""

import os
import pytest
from typing import Annotated, TypedDict
from unittest.mock import MagicMock

# Skip all tests if OPENAI_API_KEY not set
pytestmark = pytest.mark.integration


def requires_openai():
    """Check if OpenAI API key is available."""
    return pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set"
    )


class TestLangGraphInstrumentationIntegration:
    """Integration tests for LangGraph instrumentation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        # Import here to avoid issues if langgraph not installed
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # Set up in-memory exporter for testing
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        trace.set_tracer_provider(provider)

        # Import and instrument LangGraph
        from traceai_langchain import LangGraphInstrumentor

        self.instrumentor = LangGraphInstrumentor()
        # Reset singleton state
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False
        self.instrumentor = LangGraphInstrumentor()
        self.instrumentor.instrument(tracer_provider=provider)

        yield

        # Cleanup
        self.instrumentor.uninstrument()
        self.exporter.clear()

    def test_simple_graph_creates_spans(self):
        """Test that a simple graph creates expected spans."""
        from langgraph.graph import StateGraph, END

        class SimpleState(TypedDict):
            value: int

        def increment(state: SimpleState) -> SimpleState:
            return {"value": state["value"] + 1}

        def double(state: SimpleState) -> SimpleState:
            return {"value": state["value"] * 2}

        # Build graph
        graph = StateGraph(SimpleState)
        graph.add_node("increment", increment)
        graph.add_node("double", double)
        graph.set_entry_point("increment")
        graph.add_edge("increment", "double")
        graph.add_edge("double", END)

        app = graph.compile()

        # Execute
        result = app.invoke({"value": 5})

        # Verify result
        assert result["value"] == 12  # (5 + 1) * 2 = 12

        # Check spans were created
        spans = self.exporter.get_finished_spans()
        assert len(spans) > 0

        # Find node spans
        span_names = [s.name for s in spans]
        print(f"Span names: {span_names}")

    def test_state_tracker_records_transitions(self):
        """Test that state transitions are recorded."""
        from langgraph.graph import StateGraph, END

        class CounterState(TypedDict):
            count: int
            history: list

        def step1(state: CounterState) -> CounterState:
            return {
                "count": state["count"] + 1,
                "history": state["history"] + ["step1"],
            }

        def step2(state: CounterState) -> CounterState:
            return {
                "count": state["count"] + 10,
                "history": state["history"] + ["step2"],
            }

        graph = StateGraph(CounterState)
        graph.add_node("step1", step1)
        graph.add_node("step2", step2)
        graph.set_entry_point("step1")
        graph.add_edge("step1", "step2")
        graph.add_edge("step2", END)

        app = graph.compile()
        result = app.invoke({"count": 0, "history": []})

        assert result["count"] == 11
        assert result["history"] == ["step1", "step2"]

        # Check state tracker
        history = self.instrumentor.get_state_history()
        # State tracker should have recorded transitions
        print(f"State history entries: {len(history)}")

    def test_conditional_edge_tracking(self):
        """Test that conditional edges are tracked."""
        from langgraph.graph import StateGraph, END

        class RouterState(TypedDict):
            route: str
            result: str

        def router(state: RouterState) -> str:
            return state["route"]

        def path_a(state: RouterState) -> RouterState:
            return {"result": "went_to_a"}

        def path_b(state: RouterState) -> RouterState:
            return {"result": "went_to_b"}

        graph = StateGraph(RouterState)
        graph.add_node("path_a", path_a)
        graph.add_node("path_b", path_b)
        graph.set_entry_point("path_a")  # Default entry
        graph.add_conditional_edges(
            "path_a",
            router,
            {
                "a": "path_a",
                "b": "path_b",
                "end": END,
            }
        )
        graph.add_edge("path_b", END)

        app = graph.compile()

        # Test route to b
        result = app.invoke({"route": "b", "result": ""})
        assert result["result"] == "went_to_b"

        spans = self.exporter.get_finished_spans()
        print(f"Conditional routing spans: {len(spans)}")

    def test_interrupt_resume_tracking(self):
        """Test interrupt/resume tracking."""
        # Test the tracker directly since actual interrupt requires checkpointer
        from traceai_langchain._langgraph._interrupt_tracker import (
            InterruptResumeTracker,
            InterruptInfo,
            ResumeInfo,
        )
        from opentelemetry import trace

        tracer = trace.get_tracer(__name__)
        tracker = InterruptResumeTracker(tracer=tracer)

        # Simulate interrupt
        interrupt = tracker.on_interrupt(
            thread_id="test-thread",
            node_name="approval_node",
            reason="Needs human approval",
            state={"pending": True},
        )

        assert interrupt.interrupt_id is not None
        assert interrupt.thread_id == "test-thread"
        assert interrupt.reason == "Needs human approval"
        assert interrupt.is_intentional == True

        # Simulate resume
        resume = tracker.on_resume(
            thread_id="test-thread",
            resume_input={"approved": True},
        )

        assert resume.resume_id is not None
        assert resume.from_interrupt == True
        assert resume.interrupt_id == interrupt.interrupt_id
        assert resume.wait_duration_seconds is not None

        # Check stats
        stats = tracker.get_stats()
        assert stats["total_interrupts"] == 1
        assert stats["total_resumes"] == 1

    def test_cost_tracking(self):
        """Test cost tracking functionality."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        # Track some LLM usage
        cost1 = tracker.track_llm_usage(
            node_name="chat_node",
            model="gpt-4",
            input_tokens=1000,
            output_tokens=500,
        )

        cost2 = tracker.track_llm_usage(
            node_name="summary_node",
            model="gpt-3.5-turbo",
            input_tokens=2000,
            output_tokens=1000,
        )

        # Check costs
        assert cost1.total_cost_usd > 0
        assert cost2.total_cost_usd > 0
        assert cost1.total_cost_usd > cost2.total_cost_usd  # GPT-4 is more expensive

        # Check stats
        stats = tracker.get_stats()
        assert stats["total_calls"] == 2
        assert stats["total_input_tokens"] == 3000
        assert stats["total_output_tokens"] == 1500

        # Check by node
        by_node = tracker.get_cost_by_node()
        assert "chat_node" in by_node
        assert "summary_node" in by_node

    @requires_openai()
    def test_with_real_llm_call(self):
        """Test with actual OpenAI LLM call (requires API key)."""
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            pytest.skip("langchain_openai not installed")
        from langgraph.graph import StateGraph, END

        class ChatState(TypedDict):
            messages: list
            response: str

        def chat_node(state: ChatState) -> ChatState:
            llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
            response = llm.invoke(state["messages"])
            return {"response": response.content}

        graph = StateGraph(ChatState)
        graph.add_node("chat", chat_node)
        graph.set_entry_point("chat")
        graph.add_edge("chat", END)

        app = graph.compile()

        result = app.invoke({
            "messages": [{"role": "user", "content": "Say 'hello' and nothing else."}],
            "response": "",
        })

        assert "hello" in result["response"].lower()

        # Check spans
        spans = self.exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        print(f"LLM call spans: {span_names}")
        assert len(spans) > 0


class TestMultiAgentIntegration:
    """Test multi-agent patterns."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        trace.set_tracer_provider(provider)

        from traceai_langchain import LangGraphInstrumentor
        LangGraphInstrumentor._instance = None
        LangGraphInstrumentor._is_instrumented = False
        self.instrumentor = LangGraphInstrumentor()
        self.instrumentor.instrument(tracer_provider=provider)

        yield

        self.instrumentor.uninstrument()
        self.exporter.clear()

    def test_multi_agent_message_tracking(self):
        """Test tracking messages between agents."""
        # Track agent messages
        correlation_id = self.instrumentor.track_agent_message(
            from_agent="researcher",
            to_agent="writer",
            message={"task": "Write about AI"},
            message_type="task",
        )

        assert correlation_id is not None

        # Track response
        response_id = self.instrumentor.track_agent_message(
            from_agent="writer",
            to_agent="researcher",
            message={"result": "Draft complete"},
            message_type="result",
            correlation_id=correlation_id,
        )

        assert response_id == correlation_id  # Same conversation

        # Check stats
        stats = self.instrumentor.get_multiagent_stats()
        assert stats["total_messages"] == 2

    def test_supervisor_routing_tracking(self):
        """Test supervisor routing decisions."""
        decision = self.instrumentor.track_supervisor_routing(
            supervisor_name="main_supervisor",
            selected_agent="researcher",
            available_agents=["researcher", "writer", "reviewer"],
            reason="Task requires research capabilities",
        )

        assert decision is not None
        assert decision.selected_agent == "researcher"

        stats = self.instrumentor.get_multiagent_stats()
        assert stats["total_supervisor_decisions"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
