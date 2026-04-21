"""
A2A Protocol semantic conventions for traceAI.

These constants define the OpenTelemetry span attribute names used when
instrumenting Google's Agent-to-Agent (A2A) Protocol. They extend the
gen_ai.* namespace used by the rest of traceAI for consistency.

Reference: https://google.github.io/A2A/
"""

# Task attributes
A2A_TASK_ID = "gen_ai.a2a.task.id"
"""Unique identifier for an A2A task (UUID string)."""

A2A_TASK_STATE = "gen_ai.a2a.task.state"
"""
Current state of the A2A task.
One of: submitted | working | input-required | completed | failed | canceled
"""

# Agent identification attributes
A2A_AGENT_URL = "gen_ai.a2a.agent.url"
"""Base URL of the remote A2A agent endpoint being called."""

A2A_AGENT_CARD_NAME = "gen_ai.a2a.agent.card.name"
"""Human-readable name from the remote agent's AgentCard discovery document."""

A2A_AGENT_CARD_VERSION = "gen_ai.a2a.agent.card.version"
"""Version string from the remote agent's AgentCard discovery document."""

# Message attributes
A2A_MESSAGE_ROLE = "gen_ai.a2a.message.role"
"""Role of the A2A message sender — either 'user' or 'agent'."""

A2A_MESSAGE_PARTS_COUNT = "gen_ai.a2a.message.parts.count"
"""Number of parts (text, file, data) contained in an A2A message."""

# Streaming and artifact attributes
A2A_STREAMING = "gen_ai.a2a.streaming"
"""Boolean flag: True if this A2A call uses server-sent event (SSE) streaming."""

A2A_ARTIFACT_TYPE = "gen_ai.a2a.artifact.type"
"""Type of the final A2A artifact received — one of: text | file | data."""

# Push notification attributes
A2A_PUSH_NOTIFICATION_URL = "gen_ai.a2a.push_notification.url"
"""Webhook URL configured for push notification delivery on this task."""

# Distributed trace propagation attributes
A2A_PROPAGATED_TRACE_ID = "gen_ai.a2a.propagated_trace_id"
"""
The W3C trace-id that was injected into the outbound A2A HTTP request via
the 'traceparent' header. Recorded here so the trace link is queryable
even if the remote agent uses a different backend.
"""

# Span kind values (mirroring FiSpanKindValues)
A2A_SPAN_KIND_CLIENT = "A2A_CLIENT"
"""Span kind for outbound A2A calls — this agent is calling a remote agent."""

A2A_SPAN_KIND_SERVER = "A2A_SERVER"
"""Span kind for inbound A2A tasks — this agent is receiving a task."""

# Task state constants (for reference in code)
TASK_STATE_SUBMITTED = "submitted"
TASK_STATE_WORKING = "working"
TASK_STATE_INPUT_REQUIRED = "input-required"
TASK_STATE_COMPLETED = "completed"
TASK_STATE_FAILED = "failed"
TASK_STATE_CANCELED = "canceled"

# Message role constants
MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_AGENT = "agent"
