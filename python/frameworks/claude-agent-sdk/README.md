# TraceAI Claude Agent SDK Instrumentation

OpenTelemetry instrumentation for the Claude Agent SDK (formerly Claude Code SDK).

## Installation

```bash
pip install traceai-claude-agent-sdk
```

## Quick Start

```python
from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

# Initialize instrumentation
ClaudeAgentInstrumentor().instrument()

# Use Claude Agent SDK as normal - tracing is automatic
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

async def main():
    async for message in query(
        prompt="What files are in this directory?",
        options=ClaudeAgentOptions(allowed_tools=["Glob", "Read"])
    ):
        print(message)

asyncio.run(main())
```

## Features

### Comprehensive Tracing

- **Conversation Spans**: Root spans for each query/conversation
- **Assistant Turn Spans**: Individual assistant response turns
- **Tool Execution Spans**: Every tool call (built-in, MCP, custom)
- **Subagent Spans**: Task tool and subagent coordination
- **Session Tracking**: Resume and fork operations

### Span Kinds

| Kind | Description |
|------|-------------|
| `conversation` | Root span for a query/conversation |
| `assistant_turn` | Individual assistant response turn |
| `tool_execution` | Tool invocation |
| `subagent` | Subagent (Task tool) execution |
| `mcp_tool` | MCP server tool call |

### Semantic Attributes

The instrumentation captures comprehensive attributes:

**Conversation Level:**
- `claude_agent.prompt` - User prompt
- `claude_agent.model` - Model used (claude-3-opus, etc.)
- `claude_agent.session_id` - Session identifier
- `claude_agent.permission_mode` - Permission mode setting

**Tool Level:**
- `claude_agent.tool.name` - Tool name
- `claude_agent.tool.use_id` - Unique tool use ID
- `claude_agent.tool.input` - Tool input parameters
- `claude_agent.tool.output` - Tool result
- `claude_agent.tool.source` - "builtin", "mcp", or "custom"

**Usage/Cost:**
- `gen_ai.usage.input_tokens` - Input token count
- `gen_ai.usage.output_tokens` - Output token count
- `claude_agent.cost.total_usd` - Total cost in USD

## Configuration

### With Custom Tracer Provider

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, BatchSpanProcessor

# Set up custom provider
provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument with custom provider
from traceai_claude_agent_sdk import ClaudeAgentInstrumentor
ClaudeAgentInstrumentor().instrument(tracer_provider=provider)
```

### Using Convenience Function

```python
from traceai_claude_agent_sdk import instrument_claude_agent_sdk

instrumentor = instrument_claude_agent_sdk()
```

## Subagent Tracing

The instrumentation automatically tracks subagent (Task tool) executions with proper parent-child span relationships:

```python
# Subagents are traced automatically when using Task tool
# Span hierarchy:
#   claude_agent.conversation (root)
#     └── claude_agent.assistant_turn
#           └── claude_agent.tool (Task)
#                 └── claude_agent.subagent.Explore
#                       └── claude_agent.tool (Read)
```

Features:
- **Nested subagent tracking** - Full hierarchy preserved
- **Cost aggregation** - Costs roll up through the hierarchy
- **Parent-child linking** - `parent_tool_use_id` for correlation

## Session Tracking

Session continuity is tracked across queries with resume and fork support:

```python
# Session tracking happens automatically
# Attributes captured:
# - claude_agent.session.id
# - claude_agent.session.is_new
# - claude_agent.session.is_resumed
# - claude_agent.session.previous_id (for resumed sessions)
# - claude_agent.session.fork_from (for forked sessions)
```

Features:
- **Resume linking** - Trace links between resumed sessions
- **Fork tracking** - Branches tracked with source reference
- **Metrics carryover** - Aggregated metrics across session chain

## MCP Server Support

MCP (Model Context Protocol) server tools are automatically traced:

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

options = ClaudeAgentOptions(
    mcp_servers={
        "playwright": {"command": "npx", "args": ["-y", "@anthropic/mcp-playwright"]},
    }
)

# MCP tools will have span kind "mcp_tool" and include server attribution
# Attributes:
# - claude_agent.mcp.server_name
# - claude_agent.mcp.server_command
# - claude_agent.mcp.tool_name (parsed from mcp__server__tool format)
```

## Development

### Running Tests

```bash
cd python/frameworks/claude-agent-sdk
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

### Test Coverage

- 141 unit tests covering:
  - Semantic attributes and span kinds (21 tests)
  - Hook injection and tool tracing (17 tests)
  - Client wrapper functionality (32 tests)
  - Instrumentor lifecycle (16 tests)
  - Subagent tracking (20 tests)
  - Session tracking (17 tests)
  - MCP server tracking (18 tests)

## License

MIT
