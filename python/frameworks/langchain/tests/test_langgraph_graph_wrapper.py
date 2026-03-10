"""Tests for LangGraph graph wrapper module."""

import pytest
from unittest.mock import MagicMock, patch

from traceai_langchain._langgraph._graph_wrapper import (
    GraphWrapper,
    GraphTopology,
    ConditionalEdgeTracker,
)
from traceai_langchain._langgraph._state_tracker import StateTransitionTracker


class TestGraphTopology:
    """Test GraphTopology class."""

    def test_initialization(self):
        """Test GraphTopology initialization."""
        topology = GraphTopology()
        assert topology.nodes == []
        assert topology.edges == []
        assert topology.conditional_edges == []
        assert topology.entry_point is None
        assert topology.end_nodes == set()

    def test_to_dict(self):
        """Test converting topology to dictionary."""
        topology = GraphTopology()
        topology.nodes = ["node1", "node2"]
        topology.edges = [("node1", "node2")]
        topology.entry_point = "node1"
        topology.end_nodes = {"node2"}

        result = topology.to_dict()

        assert result["nodes"] == ["node1", "node2"]
        assert result["edges"] == [("node1", "node2")]
        assert result["entry_point"] == "node1"
        assert "node2" in result["end_nodes"]

    def test_to_json(self):
        """Test converting topology to JSON."""
        topology = GraphTopology()
        topology.nodes = ["a", "b"]
        topology.entry_point = "a"

        json_str = topology.to_json()

        assert '"nodes": ["a", "b"]' in json_str
        assert '"entry_point": "a"' in json_str


class TestConditionalEdgeTracker:
    """Test ConditionalEdgeTracker class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()

    def test_initialization(self):
        """Test ConditionalEdgeTracker initialization."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)
        assert tracker._tracer == self.mock_tracer
        assert tracker._decisions == []

    def test_wrap_condition(self):
        """Test wrapping a condition function."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)

        def condition_func(state):
            return "branch_a" if state.get("value") > 5 else "branch_b"

        branch_mapping = {
            "branch_a": "node_a",
            "branch_b": "node_b",
        }

        wrapped = tracker.wrap_condition(
            source_node="decision_node",
            condition_func=condition_func,
            branch_mapping=branch_mapping,
        )

        assert callable(wrapped)

    def test_wrap_condition_records_decision(self):
        """Test that wrapped condition records decisions."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)

        def condition_func(state):
            return "go_right"

        branch_mapping = {"go_right": "right_node", "go_left": "left_node"}

        with patch("traceai_langchain._langgraph._graph_wrapper.trace_api") as mock_trace:
            mock_span = MagicMock()
            mock_span.is_recording.return_value = True
            mock_trace.get_current_span.return_value = mock_span

            wrapped = tracker.wrap_condition(
                source_node="router",
                condition_func=condition_func,
                branch_mapping=branch_mapping,
            )

            result = wrapped({"input": "test"})

        assert result == "go_right"
        decisions = tracker.get_decisions()
        assert len(decisions) == 1
        assert decisions[0]["source_node"] == "router"
        assert decisions[0]["result"] == "go_right"
        assert decisions[0]["target_node"] == "right_node"

    def test_wrap_condition_handles_exception(self):
        """Test that wrapped condition handles exceptions."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)

        def failing_condition(state):
            raise ValueError("Condition error")

        with patch("traceai_langchain._langgraph._graph_wrapper.trace_api") as mock_trace:
            mock_span = MagicMock()
            mock_span.is_recording.return_value = True
            mock_trace.get_current_span.return_value = mock_span

            wrapped = tracker.wrap_condition(
                source_node="failing_router",
                condition_func=failing_condition,
                branch_mapping={},
            )

            with pytest.raises(ValueError, match="Condition error"):
                wrapped({"input": "test"})

    def test_get_decisions(self):
        """Test getting recorded decisions."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)

        with patch("traceai_langchain._langgraph._graph_wrapper.trace_api") as mock_trace:
            mock_span = MagicMock()
            mock_span.is_recording.return_value = True
            mock_trace.get_current_span.return_value = mock_span

            wrapped = tracker.wrap_condition(
                source_node="router1",
                condition_func=lambda s: "a",
                branch_mapping={"a": "node_a"},
            )
            wrapped({})

            wrapped = tracker.wrap_condition(
                source_node="router2",
                condition_func=lambda s: "b",
                branch_mapping={"b": "node_b"},
            )
            wrapped({})

        decisions = tracker.get_decisions()
        assert len(decisions) == 2

    def test_reset(self):
        """Test resetting recorded decisions."""
        tracker = ConditionalEdgeTracker(self.mock_tracer)

        with patch("traceai_langchain._langgraph._graph_wrapper.trace_api") as mock_trace:
            mock_span = MagicMock()
            mock_span.is_recording.return_value = True
            mock_trace.get_current_span.return_value = mock_span

            wrapped = tracker.wrap_condition(
                source_node="router",
                condition_func=lambda s: "a",
                branch_mapping={"a": "node_a"},
            )
            wrapped({})

        assert len(tracker.get_decisions()) == 1

        tracker.reset()
        assert len(tracker.get_decisions()) == 0


class TestGraphWrapper:
    """Test GraphWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()
        self.mock_span = MagicMock()
        self.mock_span.is_recording.return_value = True
        self.mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=self.mock_span
        )
        self.mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        self.state_tracker = StateTransitionTracker()

    def test_initialization(self):
        """Test GraphWrapper initialization."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )
        assert wrapper._tracer == self.mock_tracer
        assert wrapper._state_tracker == self.state_tracker
        assert wrapper._topology is None
        assert wrapper._superstep_count == 0

    def test_capture_topology_basic(self):
        """Test capturing basic graph topology."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        mock_graph = MagicMock()
        mock_graph.nodes = {"node1": MagicMock(), "node2": MagicMock()}
        mock_graph.edges = {"node1": "node2"}
        mock_graph.entry_point = "node1"

        topology = wrapper.capture_topology(mock_graph)

        assert "node1" in topology.nodes
        assert "node2" in topology.nodes
        assert topology.entry_point == "node1"

    def test_capture_topology_with_conditional_edges(self):
        """Test capturing topology with conditional edges."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        mock_graph = MagicMock()
        mock_graph.nodes = {"router": MagicMock(), "a": MagicMock(), "b": MagicMock()}
        mock_graph.edges = {}
        mock_graph._conditional_edges = {
            "router": (lambda x: x, {"branch_a": "a", "branch_b": "b"})
        }
        mock_graph.entry_point = "router"

        topology = wrapper.capture_topology(mock_graph)

        assert len(topology.conditional_edges) == 1
        assert topology.conditional_edges[0]["source"] == "router"

    def test_wrap_compile(self):
        """Test wrapping compile method."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        mock_original_compile = MagicMock()
        mock_compiled = MagicMock()
        mock_compiled.invoke = MagicMock(return_value={"output": "result"})
        mock_original_compile.return_value = mock_compiled

        wrapped_compile = wrapper.wrap_compile(mock_original_compile)

        mock_graph = MagicMock()
        mock_graph.nodes = {"node1": MagicMock()}
        mock_graph.edges = {}
        mock_graph.entry_point = "node1"

        result = wrapped_compile(mock_graph)

        mock_original_compile.assert_called_once()
        assert wrapper._topology is not None

    def test_topology_property(self):
        """Test topology property."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        assert wrapper.topology is None

        mock_graph = MagicMock()
        mock_graph.nodes = {"n": MagicMock()}
        mock_graph.edges = {}
        mock_graph.entry_point = "n"

        wrapper.capture_topology(mock_graph)
        assert wrapper.topology is not None

    def test_conditional_tracker_property(self):
        """Test conditional_tracker property."""
        wrapper = GraphWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        tracker = wrapper.conditional_tracker
        assert isinstance(tracker, ConditionalEdgeTracker)
