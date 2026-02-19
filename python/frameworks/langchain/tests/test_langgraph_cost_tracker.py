"""Tests for LangGraph cost tracker module."""

import pytest
from unittest.mock import MagicMock


class TestNodeCost:
    """Test NodeCost class."""

    def test_initialization(self):
        """Test NodeCost initialization."""
        from traceai_langchain._langgraph._cost_tracker import NodeCost

        cost = NodeCost("chat_node", "gpt-4")

        assert cost.node_name == "chat_node"
        assert cost.model == "gpt-4"
        assert cost.input_tokens == 0
        assert cost.output_tokens == 0
        assert cost.total_cost_usd == 0.0

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._cost_tracker import NodeCost

        cost = NodeCost("node1", "gpt-4")
        cost.input_tokens = 100
        cost.output_tokens = 50
        cost.total_cost_usd = 0.006

        result = cost.to_dict()

        assert result["node_name"] == "node1"
        assert result["model"] == "gpt-4"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["total_cost_usd"] == 0.006


class TestCostTracker:
    """Test CostTracker class."""

    def test_initialization(self):
        """Test CostTracker initialization."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        assert tracker._node_costs == []
        assert tracker._total_cost_usd == 0.0

    def test_initialization_custom_pricing(self):
        """Test with custom pricing."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        custom_pricing = {
            "custom-model": {"input": 0.01, "output": 0.02}
        }

        tracker = CostTracker(pricing=custom_pricing)

        assert "custom-model" in tracker._pricing

    def test_track_llm_usage(self):
        """Test tracking LLM usage."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        cost = tracker.track_llm_usage(
            node_name="chat_node",
            model="gpt-4",
            input_tokens=1000,
            output_tokens=500,
        )

        assert cost.node_name == "chat_node"
        assert cost.model == "gpt-4"
        assert cost.input_tokens == 1000
        assert cost.output_tokens == 500
        assert cost.total_cost_usd > 0

        assert len(tracker._node_costs) == 1
        assert tracker._total_input_tokens == 1000
        assert tracker._total_output_tokens == 500

    def test_track_llm_usage_with_span(self):
        """Test tracking with span."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        tracker.track_llm_usage(
            node_name="node1",
            model="gpt-4",
            input_tokens=100,
            output_tokens=50,
            span=mock_span,
        )

        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()

    def test_track_llm_usage_cached_tokens(self):
        """Test tracking with cached tokens."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        cost = tracker.track_llm_usage(
            node_name="node1",
            model="gpt-4",
            input_tokens=1000,
            output_tokens=100,
            cached_tokens=500,  # 500 of input is cached (free)
        )

        assert cost.cached_tokens == 500
        # Cost should be lower due to cached tokens
        # Only 500 effective input tokens

    def test_cost_calculation_gpt4(self):
        """Test cost calculation for GPT-4."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        cost = tracker.track_llm_usage(
            node_name="test",
            model="gpt-4",
            input_tokens=1000,  # $0.03 per 1K = $0.03
            output_tokens=1000,  # $0.06 per 1K = $0.06
        )

        # Total should be $0.09
        assert abs(cost.total_cost_usd - 0.09) < 0.0001

    def test_cost_calculation_gpt35_turbo(self):
        """Test cost calculation for GPT-3.5 Turbo."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        cost = tracker.track_llm_usage(
            node_name="test",
            model="gpt-3.5-turbo",
            input_tokens=1000,  # $0.0005 per 1K = $0.0005
            output_tokens=1000,  # $0.0015 per 1K = $0.0015
        )

        # Total should be $0.002
        assert abs(cost.total_cost_usd - 0.002) < 0.0001

    def test_unknown_model_fallback(self):
        """Test cost calculation for unknown model."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        cost = tracker.track_llm_usage(
            node_name="test",
            model="unknown-model-xyz",
            input_tokens=1000,
            output_tokens=1000,
        )

        # Should use default fallback pricing
        assert cost.total_cost_usd > 0

    def test_get_node_costs(self):
        """Test getting node costs."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        tracker.track_llm_usage("node1", "gpt-4", 100, 50)
        tracker.track_llm_usage("node2", "gpt-4", 200, 100)
        tracker.track_llm_usage("node1", "gpt-4", 150, 75)

        all_costs = tracker.get_node_costs()
        assert len(all_costs) == 3

        node1_costs = tracker.get_node_costs("node1")
        assert len(node1_costs) == 2

    def test_get_cost_by_model(self):
        """Test getting costs by model."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        tracker.track_llm_usage("node1", "gpt-4", 100, 50)
        tracker.track_llm_usage("node2", "gpt-3.5-turbo", 200, 100)
        tracker.track_llm_usage("node3", "gpt-4", 150, 75)

        by_model = tracker.get_cost_by_model()

        assert "gpt-4" in by_model
        assert "gpt-3.5-turbo" in by_model
        assert by_model["gpt-4"]["call_count"] == 2
        assert by_model["gpt-3.5-turbo"]["call_count"] == 1

    def test_get_cost_by_node(self):
        """Test getting costs by node."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        tracker.track_llm_usage("chat", "gpt-4", 100, 50)
        tracker.track_llm_usage("chat", "gpt-4", 150, 75)
        tracker.track_llm_usage("analyzer", "gpt-3.5-turbo", 200, 100)

        by_node = tracker.get_cost_by_node()

        assert "chat" in by_node
        assert "analyzer" in by_node
        assert by_node["chat"]["call_count"] == 2
        assert by_node["analyzer"]["call_count"] == 1
        assert "gpt-4" in by_node["chat"]["models_used"]

    def test_get_stats(self):
        """Test getting overall statistics."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        tracker.track_llm_usage("node1", "gpt-4", 1000, 500)
        tracker.track_llm_usage("node2", "gpt-4", 2000, 1000)
        tracker.track_llm_usage("node3", "gpt-3.5-turbo", 500, 250)

        stats = tracker.get_stats()

        assert stats["total_calls"] == 3
        assert stats["total_input_tokens"] == 3500
        assert stats["total_output_tokens"] == 1750
        assert stats["unique_nodes"] == 3
        assert stats["unique_models"] == 2
        assert stats["most_expensive_node"] is not None

    def test_get_stats_no_data(self):
        """Test getting stats with no data."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()
        stats = tracker.get_stats()
        assert "error" in stats

    def test_update_pricing(self):
        """Test updating pricing."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()
        tracker.update_pricing("new-model", 0.05, 0.10)

        assert tracker._pricing["new-model"]["input"] == 0.05
        assert tracker._pricing["new-model"]["output"] == 0.10

    def test_reset(self):
        """Test resetting the tracker."""
        from traceai_langchain._langgraph._cost_tracker import CostTracker

        tracker = CostTracker()

        tracker.track_llm_usage("node1", "gpt-4", 1000, 500)
        tracker.track_llm_usage("node2", "gpt-4", 2000, 1000)

        assert len(tracker._node_costs) == 2
        assert tracker._total_cost_usd > 0

        tracker.reset()

        assert len(tracker._node_costs) == 0
        assert tracker._total_cost_usd == 0.0
        assert tracker._total_input_tokens == 0


class TestDefaultPricing:
    """Test default pricing values."""

    def test_openai_models_have_pricing(self):
        """Test that OpenAI models have pricing."""
        from traceai_langchain._langgraph._cost_tracker import DEFAULT_PRICING

        assert "gpt-4" in DEFAULT_PRICING
        assert "gpt-4-turbo" in DEFAULT_PRICING
        assert "gpt-4o" in DEFAULT_PRICING
        assert "gpt-3.5-turbo" in DEFAULT_PRICING

    def test_anthropic_models_have_pricing(self):
        """Test that Anthropic models have pricing."""
        from traceai_langchain._langgraph._cost_tracker import DEFAULT_PRICING

        assert "claude-3-opus" in DEFAULT_PRICING
        assert "claude-3-sonnet" in DEFAULT_PRICING
        assert "claude-3-haiku" in DEFAULT_PRICING

    def test_pricing_has_input_output(self):
        """Test that pricing has input and output keys."""
        from traceai_langchain._langgraph._cost_tracker import DEFAULT_PRICING

        for model, pricing in DEFAULT_PRICING.items():
            assert "input" in pricing, f"{model} missing input pricing"
            assert "output" in pricing, f"{model} missing output pricing"
