"""Cost tracking for LangGraph workflows.

Tracks per-node and per-graph costs for LLM usage.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opentelemetry.trace import Span

from traceai_langchain._langgraph._attributes import LangGraphAttributes
from traceai_langchain._langgraph._state_tracker import safe_json_dumps


# Default pricing per 1K tokens (USD)
DEFAULT_PRICING = {
    # OpenAI
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4-turbo-preview": {"input": 0.01, "output": 0.03},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "gpt-3.5-turbo-16k": {"input": 0.003, "output": 0.004},
    # Anthropic
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    # Google
    "gemini-pro": {"input": 0.00025, "output": 0.0005},
    "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
    # Mistral
    "mistral-large": {"input": 0.004, "output": 0.012},
    "mistral-medium": {"input": 0.0027, "output": 0.0081},
    "mistral-small": {"input": 0.001, "output": 0.003},
}


class NodeCost:
    """Cost information for a single node execution."""

    def __init__(
        self,
        node_name: str,
        model: Optional[str] = None,
    ):
        self.node_name = node_name
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.total_cost_usd = 0.0
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "node_name": self.node_name,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_cost_usd": self.total_cost_usd,
            "timestamp": self.timestamp,
        }


class CostTracker:
    """Track costs per node, per superstep, and per graph execution.

    Provides per-node cost breakdown which is a competitive advantage
    over implementations that only track total costs.
    """

    def __init__(self, pricing: Optional[Dict[str, Dict[str, float]]] = None):
        """Initialize the cost tracker.

        Args:
            pricing: Custom pricing dictionary. If not provided, uses defaults.
                     Format: {"model_name": {"input": cost_per_1k, "output": cost_per_1k}}
        """
        self._pricing = pricing or DEFAULT_PRICING
        self._node_costs: List[NodeCost] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_usd = 0.0

    def track_llm_usage(
        self,
        node_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        span: Optional[Span] = None,
    ) -> NodeCost:
        """Track LLM usage for a node.

        Args:
            node_name: Name of the node
            model: Model name/ID
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cached_tokens: Number of cached tokens (if any)
            span: Current span to record attributes on

        Returns:
            NodeCost with calculated costs
        """
        node_cost = NodeCost(node_name, model)
        node_cost.input_tokens = input_tokens
        node_cost.output_tokens = output_tokens
        node_cost.cached_tokens = cached_tokens

        # Calculate cost
        cost = self._calculate_cost(model, input_tokens, output_tokens, cached_tokens)
        node_cost.total_cost_usd = cost

        # Update totals
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._total_cost_usd += cost

        self._node_costs.append(node_cost)

        # Record on span
        if span and span.is_recording():
            span.set_attribute(LangGraphAttributes.COST_NODE, node_name)
            span.set_attribute(LangGraphAttributes.COST_MODEL, model)
            span.set_attribute(LangGraphAttributes.COST_INPUT_TOKENS, input_tokens)
            span.set_attribute(LangGraphAttributes.COST_OUTPUT_TOKENS, output_tokens)
            span.set_attribute(LangGraphAttributes.COST_TOTAL_USD, cost)

            if cached_tokens > 0:
                span.set_attribute(LangGraphAttributes.COST_CACHED_TOKENS, cached_tokens)

            span.add_event("llm_cost", {
                "node": node_name,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            })

        return node_cost

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        """Calculate cost for token usage.

        Args:
            model: Model name
            input_tokens: Input token count
            output_tokens: Output token count
            cached_tokens: Cached token count (usually free)

        Returns:
            Cost in USD
        """
        # Try exact match first
        if model in self._pricing:
            pricing = self._pricing[model]
        else:
            # Try partial match
            pricing = None
            model_lower = model.lower()
            for key in self._pricing:
                if key.lower() in model_lower or model_lower in key.lower():
                    pricing = self._pricing[key]
                    break

            if not pricing:
                # Default to a reasonable estimate
                pricing = {"input": 0.001, "output": 0.002}

        # Calculate cost (tokens / 1000 * price_per_1k)
        # Cached tokens are usually free
        effective_input = max(0, input_tokens - cached_tokens)
        input_cost = (effective_input / 1000) * pricing["input"]
        output_cost = (output_tokens / 1000) * pricing["output"]

        return round(input_cost + output_cost, 6)

    def get_node_costs(self, node_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get costs for nodes.

        Args:
            node_name: Optional filter by node name

        Returns:
            List of node cost dictionaries
        """
        if node_name:
            return [c.to_dict() for c in self._node_costs if c.node_name == node_name]
        return [c.to_dict() for c in self._node_costs]

    def get_cost_by_model(self) -> Dict[str, Dict[str, Any]]:
        """Get costs grouped by model.

        Returns:
            Dictionary with model as key and cost summary as value
        """
        by_model: Dict[str, Dict[str, Any]] = {}

        for cost in self._node_costs:
            model = cost.model or "unknown"
            if model not in by_model:
                by_model[model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_cost_usd": 0.0,
                    "call_count": 0,
                }

            by_model[model]["input_tokens"] += cost.input_tokens
            by_model[model]["output_tokens"] += cost.output_tokens
            by_model[model]["total_cost_usd"] += cost.total_cost_usd
            by_model[model]["call_count"] += 1

        return by_model

    def get_cost_by_node(self) -> Dict[str, Dict[str, Any]]:
        """Get costs grouped by node.

        Returns:
            Dictionary with node name as key and cost summary as value
        """
        by_node: Dict[str, Dict[str, Any]] = {}

        for cost in self._node_costs:
            if cost.node_name not in by_node:
                by_node[cost.node_name] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_cost_usd": 0.0,
                    "call_count": 0,
                    "models_used": set(),
                }

            by_node[cost.node_name]["input_tokens"] += cost.input_tokens
            by_node[cost.node_name]["output_tokens"] += cost.output_tokens
            by_node[cost.node_name]["total_cost_usd"] += cost.total_cost_usd
            by_node[cost.node_name]["call_count"] += 1
            if cost.model:
                by_node[cost.node_name]["models_used"].add(cost.model)

        # Convert sets to lists for JSON serialization
        for node in by_node:
            by_node[node]["models_used"] = list(by_node[node]["models_used"])

        return by_node

    def get_stats(self) -> Dict[str, Any]:
        """Get overall cost statistics.

        Returns:
            Dictionary with cost statistics
        """
        if not self._node_costs:
            return {"error": "No cost data available"}

        by_model = self.get_cost_by_model()
        by_node = self.get_cost_by_node()

        # Find most expensive node
        most_expensive_node = None
        max_node_cost = 0.0
        for node, data in by_node.items():
            if data["total_cost_usd"] > max_node_cost:
                max_node_cost = data["total_cost_usd"]
                most_expensive_node = node

        return {
            "total_calls": len(self._node_costs),
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cost_usd": round(self._total_cost_usd, 6),
            "unique_nodes": len(by_node),
            "unique_models": len(by_model),
            "cost_by_model": by_model,
            "cost_by_node": by_node,
            "most_expensive_node": most_expensive_node,
            "most_expensive_node_cost_usd": round(max_node_cost, 6),
        }

    def update_pricing(self, model: str, input_price: float, output_price: float) -> None:
        """Update pricing for a model.

        Args:
            model: Model name
            input_price: Price per 1K input tokens in USD
            output_price: Price per 1K output tokens in USD
        """
        self._pricing[model] = {"input": input_price, "output": output_price}

    def reset(self) -> None:
        """Reset cost tracking data."""
        self._node_costs.clear()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_usd = 0.0
