"""Tests for LangGraph attributes module."""

import pytest

from traceai_langchain._langgraph._attributes import (
    LangGraphAttributes,
    LangGraphSpanKind,
)


class TestLangGraphAttributes:
    """Test LangGraph attribute definitions."""

    def test_graph_structure_attributes_exist(self):
        """Test that graph structure attributes are defined."""
        assert hasattr(LangGraphAttributes, "GRAPH_NAME")
        assert hasattr(LangGraphAttributes, "GRAPH_NODE_COUNT")
        assert hasattr(LangGraphAttributes, "GRAPH_EDGE_COUNT")
        assert hasattr(LangGraphAttributes, "GRAPH_TOPOLOGY")
        assert hasattr(LangGraphAttributes, "GRAPH_ENTRY_POINT")
        assert hasattr(LangGraphAttributes, "GRAPH_CONDITIONAL_EDGE_COUNT")

    def test_node_execution_attributes_exist(self):
        """Test that node execution attributes are defined."""
        assert hasattr(LangGraphAttributes, "NODE_NAME")
        assert hasattr(LangGraphAttributes, "NODE_TYPE")
        assert hasattr(LangGraphAttributes, "NODE_IS_ENTRY")
        assert hasattr(LangGraphAttributes, "NODE_IS_END")

    def test_execution_mode_attributes_exist(self):
        """Test that execution mode attributes are defined."""
        assert hasattr(LangGraphAttributes, "EXECUTION_MODE")
        assert hasattr(LangGraphAttributes, "EXECUTION_SUPERSTEP")
        assert hasattr(LangGraphAttributes, "EXECUTION_THREAD_ID")
        assert hasattr(LangGraphAttributes, "STREAM_MODE")

    def test_state_management_attributes_exist(self):
        """Test that state management attributes are defined."""
        assert hasattr(LangGraphAttributes, "STATE_INPUT")
        assert hasattr(LangGraphAttributes, "STATE_OUTPUT")
        assert hasattr(LangGraphAttributes, "STATE_UPDATES")
        assert hasattr(LangGraphAttributes, "STATE_CHANGED_FIELDS")
        assert hasattr(LangGraphAttributes, "STATE_REDUCER")
        assert hasattr(LangGraphAttributes, "STATE_DIFF")

    def test_checkpoint_attributes_exist(self):
        """Test that checkpoint attributes are defined."""
        assert hasattr(LangGraphAttributes, "CHECKPOINT_THREAD_ID")
        assert hasattr(LangGraphAttributes, "CHECKPOINT_ID")
        assert hasattr(LangGraphAttributes, "CHECKPOINT_BACKEND")
        assert hasattr(LangGraphAttributes, "CHECKPOINT_SIZE_BYTES")
        assert hasattr(LangGraphAttributes, "CHECKPOINT_FOUND")
        assert hasattr(LangGraphAttributes, "CHECKPOINT_OPERATION")

    def test_interrupt_resume_attributes_exist(self):
        """Test that interrupt/resume attributes are defined."""
        assert hasattr(LangGraphAttributes, "INTERRUPT_REASON")
        assert hasattr(LangGraphAttributes, "INTERRUPT_IS_INTENTIONAL")
        assert hasattr(LangGraphAttributes, "INTERRUPT_STATE_SNAPSHOT")
        assert hasattr(LangGraphAttributes, "INTERRUPT_NODE")
        assert hasattr(LangGraphAttributes, "RESUME_FROM_INTERRUPT")
        assert hasattr(LangGraphAttributes, "RESUME_WAIT_DURATION_SECONDS")
        assert hasattr(LangGraphAttributes, "RESUME_INPUT")

    def test_human_in_the_loop_attributes_exist(self):
        """Test that HITL attributes are defined."""
        assert hasattr(LangGraphAttributes, "HUMAN_DECISION")
        assert hasattr(LangGraphAttributes, "HUMAN_APPROVER_ID")
        assert hasattr(LangGraphAttributes, "HUMAN_METADATA")
        assert hasattr(LangGraphAttributes, "HUMAN_FEEDBACK")

    def test_multiagent_attributes_exist(self):
        """Test that multi-agent attributes are defined."""
        assert hasattr(LangGraphAttributes, "MULTIAGENT_FROM")
        assert hasattr(LangGraphAttributes, "MULTIAGENT_TO")
        assert hasattr(LangGraphAttributes, "MULTIAGENT_CORRELATION_ID")
        assert hasattr(LangGraphAttributes, "SUPERVISOR_NAME")
        assert hasattr(LangGraphAttributes, "SUPERVISOR_SELECTED_AGENT")
        assert hasattr(LangGraphAttributes, "SUPERVISOR_ROUTING_REASON")

    def test_cost_tracking_attributes_exist(self):
        """Test that cost tracking attributes are defined."""
        assert hasattr(LangGraphAttributes, "COST_NODE")
        assert hasattr(LangGraphAttributes, "COST_MODEL")
        assert hasattr(LangGraphAttributes, "COST_INPUT_TOKENS")
        assert hasattr(LangGraphAttributes, "COST_OUTPUT_TOKENS")
        assert hasattr(LangGraphAttributes, "COST_TOTAL_USD")

    def test_memory_tracking_attributes_exist(self):
        """Test that memory tracking attributes are defined."""
        assert hasattr(LangGraphAttributes, "MEMORY_STATE_SIZE_BYTES")
        assert hasattr(LangGraphAttributes, "MEMORY_GROWTH_WARNING")
        assert hasattr(LangGraphAttributes, "MEMORY_PEAK_BYTES")

    def test_attribute_values_are_strings(self):
        """Test that attribute values are properly formatted strings."""
        assert LangGraphAttributes.GRAPH_NAME == "langgraph.graph.name"
        assert LangGraphAttributes.NODE_NAME == "langgraph.node.name"
        assert LangGraphAttributes.STATE_INPUT == "langgraph.state.input"
        assert LangGraphAttributes.PERF_DURATION_MS == "langgraph.perf.duration_ms"


class TestLangGraphSpanKind:
    """Test LangGraph span kind definitions."""

    def test_span_kinds_exist(self):
        """Test that span kinds are defined."""
        assert hasattr(LangGraphSpanKind, "GRAPH")
        assert hasattr(LangGraphSpanKind, "NODE")
        assert hasattr(LangGraphSpanKind, "SUPERSTEP")
        assert hasattr(LangGraphSpanKind, "CHECKPOINT")
        assert hasattr(LangGraphSpanKind, "INTERRUPT")
        assert hasattr(LangGraphSpanKind, "SUBGRAPH")

    def test_span_kind_values(self):
        """Test span kind values."""
        assert LangGraphSpanKind.GRAPH == "LANGGRAPH"
        assert LangGraphSpanKind.NODE == "LANGGRAPH_NODE"
        assert LangGraphSpanKind.SUPERSTEP == "LANGGRAPH_SUPERSTEP"
        assert LangGraphSpanKind.CHECKPOINT == "LANGGRAPH_CHECKPOINT"
        assert LangGraphSpanKind.INTERRUPT == "LANGGRAPH_INTERRUPT"
        assert LangGraphSpanKind.SUBGRAPH == "LANGGRAPH_SUBGRAPH"
