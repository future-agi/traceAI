"""Multi-agent coordination tracking for LangGraph.

Tracks message passing and coordination between agents in multi-agent systems,
including supervisor routing decisions.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind

from traceai_langchain._langgraph._attributes import LangGraphAttributes
from traceai_langchain._langgraph._state_tracker import safe_json_dumps


class AgentMessage:
    """Represents a message between agents."""

    def __init__(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str = "task",
        correlation_id: Optional[str] = None,
    ):
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.message_type = message_type
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.content_preview: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "message_type": self.message_type,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "content_preview": self.content_preview,
        }


class SupervisorDecision:
    """Represents a supervisor routing decision."""

    def __init__(
        self,
        supervisor_name: str,
        selected_agent: str,
        available_agents: List[str],
        reason: Optional[str] = None,
    ):
        self.supervisor_name = supervisor_name
        self.selected_agent = selected_agent
        self.available_agents = available_agents
        self.reason = reason
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.state_context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "supervisor_name": self.supervisor_name,
            "selected_agent": self.selected_agent,
            "available_agents": self.available_agents,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class AgentExecution:
    """Tracks an agent's execution within a multi-agent system."""

    def __init__(
        self,
        agent_name: str,
        agent_type: str = "worker",
    ):
        self.agent_name = agent_name
        self.agent_type = agent_type  # "supervisor", "worker", "tool_agent"
        self.execution_count = 0
        self.total_duration_ms = 0.0
        self.messages_sent = 0
        self.messages_received = 0
        self.tasks_completed = 0
        self.errors = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "execution_count": self.execution_count,
            "total_duration_ms": self.total_duration_ms,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "tasks_completed": self.tasks_completed,
            "errors": self.errors,
        }


class MultiAgentTracker:
    """Track multi-agent coordination in LangGraph.

    Provides visibility into:
    - Message passing between agents
    - Supervisor routing decisions
    - Agent execution statistics
    - Correlation IDs for message threading
    """

    def __init__(self, tracer: trace_api.Tracer):
        """Initialize the multi-agent tracker.

        Args:
            tracer: OpenTelemetry tracer
        """
        self._tracer = tracer
        self._messages: List[AgentMessage] = []
        self._supervisor_decisions: List[SupervisorDecision] = []
        self._agent_stats: Dict[str, AgentExecution] = {}
        self._correlation_map: Dict[str, List[str]] = {}  # correlation_id -> message timestamps

    def track_agent_message(
        self,
        from_agent: str,
        to_agent: str,
        message: Any,
        message_type: str = "task",
        correlation_id: Optional[str] = None,
        span: Optional[Span] = None,
    ) -> str:
        """Track a message passed between agents.

        Args:
            from_agent: Name of the sending agent
            to_agent: Name of the receiving agent
            message: The message content
            message_type: Type of message (task, result, feedback, etc.)
            correlation_id: Optional correlation ID for threading
            span: Current span to record attributes on

        Returns:
            The correlation ID for this message
        """
        msg = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            correlation_id=correlation_id,
        )

        # Create content preview
        if message is not None:
            msg.content_preview = str(message)[:500]

        self._messages.append(msg)

        # Update agent stats
        self._ensure_agent(from_agent)
        self._ensure_agent(to_agent)
        self._agent_stats[from_agent].messages_sent += 1
        self._agent_stats[to_agent].messages_received += 1

        # Track correlation
        if msg.correlation_id not in self._correlation_map:
            self._correlation_map[msg.correlation_id] = []
        self._correlation_map[msg.correlation_id].append(msg.timestamp)

        # Record on span
        if span and span.is_recording():
            span.set_attribute(LangGraphAttributes.MULTIAGENT_FROM, from_agent)
            span.set_attribute(LangGraphAttributes.MULTIAGENT_TO, to_agent)
            span.set_attribute(LangGraphAttributes.MULTIAGENT_CORRELATION_ID, msg.correlation_id)
            span.set_attribute(LangGraphAttributes.MULTIAGENT_MESSAGE_TYPE, message_type)

            span.add_event("agent_message", {
                "from": from_agent,
                "to": to_agent,
                "type": message_type,
                "correlation_id": msg.correlation_id,
                "content_preview": msg.content_preview,
            })

        return msg.correlation_id

    def track_supervisor_routing(
        self,
        supervisor_name: str,
        selected_agent: str,
        available_agents: List[str],
        reason: Optional[str] = None,
        state: Optional[Dict[str, Any]] = None,
        span: Optional[Span] = None,
    ) -> SupervisorDecision:
        """Track a supervisor routing decision.

        Args:
            supervisor_name: Name of the supervisor agent
            selected_agent: Agent selected by the supervisor
            available_agents: All available agents
            reason: Reason for the selection
            state: State context for the decision
            span: Current span to record attributes on

        Returns:
            SupervisorDecision with captured information
        """
        decision = SupervisorDecision(
            supervisor_name=supervisor_name,
            selected_agent=selected_agent,
            available_agents=available_agents,
            reason=reason,
        )

        if state:
            decision.state_context = dict(state)

        self._supervisor_decisions.append(decision)

        # Update agent stats
        self._ensure_agent(supervisor_name, agent_type="supervisor")
        self._ensure_agent(selected_agent)

        # Record on span
        if span and span.is_recording():
            span.set_attribute(LangGraphAttributes.SUPERVISOR_NAME, supervisor_name)
            span.set_attribute(LangGraphAttributes.SUPERVISOR_SELECTED_AGENT, selected_agent)
            span.set_attribute(LangGraphAttributes.SUPERVISOR_AVAILABLE_AGENTS, available_agents)

            if reason:
                span.set_attribute(LangGraphAttributes.SUPERVISOR_ROUTING_REASON, reason)

            span.add_event("supervisor_routing", {
                "supervisor": supervisor_name,
                "selected": selected_agent,
                "available": available_agents,
                "reason": reason,
            })

        return decision

    def track_agent_execution(
        self,
        agent_name: str,
        duration_ms: float,
        success: bool = True,
        agent_type: str = "worker",
    ) -> None:
        """Track an agent's execution.

        Args:
            agent_name: Name of the agent
            duration_ms: Execution duration in milliseconds
            success: Whether execution was successful
            agent_type: Type of agent
        """
        self._ensure_agent(agent_name, agent_type)
        stats = self._agent_stats[agent_name]
        stats.execution_count += 1
        stats.total_duration_ms += duration_ms

        if success:
            stats.tasks_completed += 1
        else:
            stats.errors += 1

    def _ensure_agent(self, agent_name: str, agent_type: str = "worker") -> None:
        """Ensure an agent exists in stats tracking.

        Args:
            agent_name: Name of the agent
            agent_type: Type of agent
        """
        if agent_name not in self._agent_stats:
            self._agent_stats[agent_name] = AgentExecution(agent_name, agent_type)

    def get_messages(self, correlation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get tracked messages.

        Args:
            correlation_id: Optional filter by correlation ID

        Returns:
            List of message dictionaries
        """
        if correlation_id:
            return [m.to_dict() for m in self._messages if m.correlation_id == correlation_id]
        return [m.to_dict() for m in self._messages]

    def get_supervisor_decisions(self) -> List[Dict[str, Any]]:
        """Get supervisor routing decisions.

        Returns:
            List of decision dictionaries
        """
        return [d.to_dict() for d in self._supervisor_decisions]

    def get_agent_stats(self, agent_name: Optional[str] = None) -> Dict[str, Any]:
        """Get agent execution statistics.

        Args:
            agent_name: Optional specific agent name

        Returns:
            Dictionary with agent statistics
        """
        if agent_name:
            if agent_name in self._agent_stats:
                return self._agent_stats[agent_name].to_dict()
            return {"error": f"Agent {agent_name} not found"}

        return {
            name: stats.to_dict()
            for name, stats in self._agent_stats.items()
        }

    def get_conversation_thread(self, correlation_id: str) -> List[Dict[str, Any]]:
        """Get all messages in a conversation thread.

        Args:
            correlation_id: The correlation ID

        Returns:
            List of messages in chronological order
        """
        return self.get_messages(correlation_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get overall multi-agent statistics.

        Returns:
            Dictionary with aggregate statistics
        """
        total_messages = len(self._messages)
        total_decisions = len(self._supervisor_decisions)

        # Count message types
        message_types = {}
        for msg in self._messages:
            message_types[msg.message_type] = message_types.get(msg.message_type, 0) + 1

        # Agent selection frequency
        agent_selections = {}
        for decision in self._supervisor_decisions:
            agent = decision.selected_agent
            agent_selections[agent] = agent_selections.get(agent, 0) + 1

        # Calculate totals from agent stats
        total_executions = sum(s.execution_count for s in self._agent_stats.values())
        total_errors = sum(s.errors for s in self._agent_stats.values())

        return {
            "total_agents": len(self._agent_stats),
            "total_messages": total_messages,
            "total_supervisor_decisions": total_decisions,
            "total_executions": total_executions,
            "total_errors": total_errors,
            "message_types": message_types,
            "agent_selection_frequency": agent_selections,
            "unique_conversations": len(self._correlation_map),
        }

    def reset(self) -> None:
        """Reset the tracker."""
        self._messages.clear()
        self._supervisor_decisions.clear()
        self._agent_stats.clear()
        self._correlation_map.clear()
