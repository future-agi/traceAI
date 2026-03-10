# traceAI Documentation

Welcome to the traceAI documentation. This guide covers installation, configuration, and usage of traceAI for instrumenting your AI applications.

## Documentation Structure

### Getting Started
- [Installation](getting-started/installation.md) - How to install traceAI
- [Python Quickstart](getting-started/quickstart-python.md) - Get started with Python
- [TypeScript Quickstart](getting-started/quickstart-typescript.md) - Get started with TypeScript

### Configuration
- [TraceConfig](configuration/trace-config.md) - Privacy and data redaction settings
- [Environment Variables](configuration/environment-variables.md) - All configuration options
- [Evaluation Tags](configuration/eval-tags.md) - AI evaluation system

### Python SDK
- [Core Concepts](python/core-concepts.md) - Understanding fi_instrumentation
- [Context Managers](python/context-managers.md) - Adding metadata to spans

### TypeScript SDK
- [fi-core Package](typescript/fi-core.md) - Core TypeScript library
- [Instrumentations](typescript/instrumentations.md) - Framework instrumentations

### Examples
- [Python Examples](examples/python/) - Python code walkthroughs
- [TypeScript Examples](examples/typescript/) - TypeScript code walkthroughs

## Quick Links

| Resource | Description |
|----------|-------------|
| [Main README](../README.md) | Project overview and supported frameworks |
| [Python SDK](../python/README.md) | Python-specific documentation |
| [External Docs](https://docs.futureagi.com/) | Full documentation site |
| [Cookbooks](https://docs.futureagi.com/cookbook) | Step-by-step tutorials |

## Key Concepts

### What is traceAI?

traceAI provides drop-in OpenTelemetry instrumentation for AI applications. It automatically captures:

- **LLM Calls**: Prompts, completions, token usage, model parameters
- **Agent Workflows**: Task execution, tool calls, reasoning steps
- **Retrieval**: Document fetching, embeddings, vector searches
- **Errors**: Detailed error context and stack traces

### How It Works

```
Your Application
       │
       ▼
┌─────────────────┐
│  traceAI SDK    │  ← Instruments your framework
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  OpenTelemetry  │  ← Standard tracing API
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  OTLP Exporter  │  ← Sends traces
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Future AGI /   │  ← Visualize & analyze
│  Any OTel       │
│  Backend        │
└─────────────────┘
```

### Span Types

traceAI captures different types of spans:

| Span Kind | Description | Example |
|-----------|-------------|---------|
| `LLM` | Language model calls | Chat completions, text generation |
| `AGENT` | Agent orchestration | CrewAI tasks, AutoGen agents |
| `TOOL` | Tool/function calls | API calls, code execution |
| `RETRIEVER` | Document retrieval | RAG queries, vector search |
| `EMBEDDING` | Embedding generation | Text to vector conversion |
| `CHAIN` | Workflow chains | LangChain chains, pipelines |
| `RERANKER` | Result reranking | Search result ordering |

## Getting Help

- [GitHub Issues](https://github.com/future-agi/traceAI/issues) - Report bugs or request features
- [Slack Community](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) - Ask questions and get help
- [Stack Overflow](https://stackoverflow.com/questions/tagged/traceai) - Community Q&A
