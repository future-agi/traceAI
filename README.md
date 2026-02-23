<div align="center">

![traceAI Logo](Logo.png)

# traceAI

**OpenTelemetry-native instrumentation for AI applications**  
*Standardized observability across LLMs, agents, and frameworks*

[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![TypeScript](https://img.shields.io/badge/typescript-5.0%2B-blue)](https://www.typescriptlang.org/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-1.0+-purple)](https://opentelemetry.io/)

[Documentation](https://docs.futureagi.com/) • [Examples](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability) • [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) • [PyPI Packages](#python) • [npm Packages](#typescript)

</div>

---

## 🚀 What is traceAI?

**traceAI** provides drop-in OpenTelemetry instrumentation for popular AI frameworks and LLM providers. It automatically captures traces, spans, and attributes from your AI workflows—whether you're using OpenAI, Anthropic, LangChain, LlamaIndex, or 20+ other frameworks.

- **Zero-config tracing** for OpenAI, Anthropic, LangChain, LlamaIndex, and more
- **OpenTelemetry-native** — works with any OTel-compatible backend (Jaeger, Datadog, Future AGI, etc.)
- **Semantic conventions** for LLM calls, agents, tools, and retrieval
- **Python + TypeScript** support with consistent APIs

---

## Table of Contents

- [Key Features](#-key-features)
- [Quickstart](#-quickstart)
  - [Python](#python-quickstart)
  - [TypeScript](#typescript-quickstart)
- [Supported Frameworks](#-supported-frameworks)
  - [Python](#python)
  - [TypeScript](#typescript)
- [Compatibility Matrix](#-compatibility-matrix)
- [Architecture](#-architecture)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [Resources](#-resources)
- [Connect With Us](#-connect-with-us)

## ✨ Key Features

| Feature | Description |
|---------|-------------|
| 🎯 **Standardized Tracing** | Maps AI workflows to consistent OpenTelemetry spans & attributes |
| 🔌 **Zero-Config Setup** | Drop-in instrumentation with minimal code changes |
| 🌐 **Multi-Framework** | 20+ integrations across Python & TypeScript |
| 📊 **Vendor Agnostic** | Works with any OpenTelemetry-compatible backend |
| 🔍 **Rich Context** | Captures prompts, completions, tokens, model params, tool calls, and more |
| ⚡ **Production Ready** | Async support, streaming, error handling, and performance optimized |

---

## 🎯 Quickstart

### Python Quickstart

**1. Install**
```bash
pip install traceai-openai
```

**2. Instrument your application**
```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
import openai

# Set up environment variables
os.environ["FI_API_KEY"] = "<your-api-key>"
os.environ["FI_SECRET_KEY"] = "<your-secret-key>"
os.environ["OPENAI_API_KEY"] = "<your-openai-key>"

# Register tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my_ai_app"
)

# Instrument OpenAI
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# Use OpenAI as normal - tracing happens automatically!
response = openai.chat.completions.create(
    model="gpt-4.1",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

> **💡 Tip:** Swap `traceai-openai` for any supported framework (e.g., `traceai-langchain`, `traceai-anthropic`)

---

### TypeScript Quickstart

**1. Install**
```bash
npm install @traceai/openai @traceai/fi-core
```

**2. Instrument your application**
```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import OpenAI from "openai";

// Register tracer provider
const tracerProvider = register({
  projectName: "my_ai_app",
  projectType: ProjectType.OBSERVE,
});

// Register OpenAI instrumentation (before creating client!)
registerInstrumentations({
  tracerProvider,
  instrumentations: [new OpenAIInstrumentation()],
});

// Use OpenAI as normal - tracing happens automatically!
const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const response = await openai.chat.completions.create({
  model: "gpt-4.1",
  messages: [{ role: "user", content: "Hello!" }],
});
```

> **💡 Tip:** Works with Anthropic, LangChain, Vercel AI SDK, and more TypeScript frameworks

---

## 📦 Supported Frameworks

### Python

| Package | Description | Version |
|---------|-------------|----------|
| `traceAI-openai` | traceAI Instrumentation for OpenAI. | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai)](https://pypi.org/project/traceAI-openai)|
| `traceAI-anthropic` | traceAI Instrumentation for Anthropic. | [![PyPI](https://img.shields.io/pypi/v/traceAI-anthropic)](https://pypi.org/project/traceAI-anthropic)|
| `traceAI-llamaindex` | traceAI Instrumentation for LlamaIndex. | [![PyPI](https://img.shields.io/pypi/v/traceAI-llamaindex)](https://pypi.org/project/traceAI-llamaindex)|
| `traceAI-langchain` | traceAI Instrumentation for LangChain. | [![PyPI](https://img.shields.io/pypi/v/traceAI-langchain)](https://pypi.org/project/traceAI-langchain)|
| `traceAI-mistralai` | traceAI Instrumentation for MistralAI. | [![PyPI](https://img.shields.io/pypi/v/traceAI-mistralai)](https://pypi.org/project/traceAI-mistralai)|
| `traceAI-vertexai` | traceAI Instrumentation for VertexAI. | [![PyPI](https://img.shields.io/pypi/v/traceAI-vertexai)](https://pypi.org/project/traceAI-vertexai)|
| `traceAI-crewai` | traceAI Instrumentation for CrewAI. | [![PyPI](https://img.shields.io/pypi/v/traceAI-crewai)](https://pypi.org/project/traceAI-crewai)|
| `traceAI-haystack` | traceAI Instrumentation for Haystack. | [![PyPI](https://img.shields.io/pypi/v/traceAI-haystack)](https://pypi.org/project/traceAI-haystack)|
| `traceAI-litellm` | traceAI Instrumentation for liteLLM. | [![PyPI](https://img.shields.io/pypi/v/traceAI-litellm)](https://pypi.org/project/traceAI-litellm)|
| `traceAI-groq` | traceAI Instrumentation for Groq. | [![PyPI](https://img.shields.io/pypi/v/traceAI-groq)](https://pypi.org/project/traceAI-groq)|
| `traceAI-autogen` | traceAI Instrumentation for Autogen. | [![PyPI](https://img.shields.io/pypi/v/traceAI-autogen)](https://pypi.org/project/traceAI-autogen)|
| `traceAI-guardrails` | traceAI Instrumentation for Guardrails. | [![PyPI](https://img.shields.io/pypi/v/traceAI-guardrails)](https://pypi.org/project/traceAI-guardrails)|
| `traceAI-openai-agents` | traceAI Instrumentation for OpenAI Agents. | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai-agents)](https://pypi.org/project/traceAI-openai-agents)|
| `traceAI-smolagents` | traceAI Instrumentation for SmolAgents. | [![PyPI](https://img.shields.io/pypi/v/traceAI-smolagents)](https://pypi.org/project/traceAI-smolagents)|
| `traceAI-dspy` | traceAI Instrumentation for DSPy. | [![PyPI](https://img.shields.io/pypi/v/traceAI-dspy)](https://pypi.org/project/traceAI-dspy)|
| `traceAI-bedrock` | traceAI Instrumentation for AWS Bedrock. | [![PyPI](https://img.shields.io/pypi/v/traceAI-bedrock)](https://pypi.org/project/traceAI-bedrock)|
| `traceAI-instructor` | traceAI Instrumentation for Instructor. | [![PyPI](https://img.shields.io/pypi/v/traceAI-instructor)](https://pypi.org/project/traceAI-instructor)|
| `traceAI-google-genai` | traceAI Instrumentation for Google Generative AI. | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-genai)](https://pypi.org/project/traceAI-google-genai)|
| `traceAI-google-adk` | traceAI Instrumentation for Google ADK. | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-adk)](https://pypi.org/project/traceAI-google-adk)|
| `traceAI-pipecat` | traceAI Instrumentation for Pipecat. | [![PyPI](https://img.shields.io/pypi/v/traceAI-pipecat)](https://pypi.org/project/traceAI-pipecat)|
| `traceAI-portkey` | traceAI Instrumentation for Portkey. | [![PyPI](https://img.shields.io/pypi/v/traceAI-portkey)](https://pypi.org/project/traceAI-portkey)|
| `traceAI-mcp` | traceAI Instrumentation for Model Context Protocol. | [![PyPI](https://img.shields.io/pypi/v/traceAI-mcp)](https://pypi.org/project/traceAI-mcp)|
| `traceAI-livekit` | traceAI Instrumentation for LiveKit (Real-time). | [![PyPI](https://img.shields.io/pypi/v/traceAI-livekit)](https://pypi.org/project/traceAI-livekit)|
| `traceAI-pinecone` | traceAI Instrumentation for Pinecone vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-pinecone)](https://pypi.org/project/traceAI-pinecone)|
| `traceAI-chromadb` | traceAI Instrumentation for ChromaDB vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-chromadb)](https://pypi.org/project/traceAI-chromadb)|
| `traceAI-qdrant` | traceAI Instrumentation for Qdrant vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-qdrant)](https://pypi.org/project/traceAI-qdrant)|
| `traceAI-weaviate` | traceAI Instrumentation for Weaviate vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-weaviate)](https://pypi.org/project/traceAI-weaviate)|
| `traceAI-milvus` | traceAI Instrumentation for Milvus vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-milvus)](https://pypi.org/project/traceAI-milvus)|
| `traceAI-lancedb` | traceAI Instrumentation for LanceDB vector database. | [![PyPI](https://img.shields.io/pypi/v/traceAI-lancedb)](https://pypi.org/project/traceAI-lancedb)|
| `traceAI-mongodb` | traceAI Instrumentation for MongoDB Atlas Vector Search. | [![PyPI](https://img.shields.io/pypi/v/traceAI-mongodb)](https://pypi.org/project/traceAI-mongodb)|
| `traceAI-pgvector` | traceAI Instrumentation for pgvector PostgreSQL extension. | [![PyPI](https://img.shields.io/pypi/v/traceAI-pgvector)](https://pypi.org/project/traceAI-pgvector)|
| `traceAI-redis` | traceAI Instrumentation for Redis Vector Search. | [![PyPI](https://img.shields.io/pypi/v/traceAI-redis)](https://pypi.org/project/traceAI-redis)|

### TypeScript

| Package | Description | Version |
|---------|-------------|----------|
| `@traceai/openai` | traceAI Instrumentation for OpenAI. | [![npm](https://img.shields.io/npm/v/@traceai/openai)](https://www.npmjs.com/package/@traceai/openai)|
| `@traceai/anthropic` | traceAI Instrumentation for Anthropic. | [![npm](https://img.shields.io/npm/v/@traceai/anthropic)](https://www.npmjs.com/package/@traceai/anthropic)|
| `@traceai/langchain` | traceAI Instrumentation for LangChain. | [![npm](https://img.shields.io/npm/v/@traceai/langchain)](https://www.npmjs.com/package/@traceai/langchain)|
| `@traceai/llamaindex` | traceAI Instrumentation for LlamaIndex. | [![npm](https://img.shields.io/npm/v/@traceai/llamaindex)](https://www.npmjs.com/package/@traceai/llamaindex)|
| `@traceai/bedrock` | traceAI Instrumentation for AWS Bedrock. | [![npm](https://img.shields.io/npm/v/@traceai/bedrock)](https://www.npmjs.com/package/@traceai/bedrock)|
| `@traceai/vercel` | traceAI Instrumentation for Vercel AI SDK. | [![npm](https://img.shields.io/npm/v/@traceai/vercel)](https://www.npmjs.com/package/@traceai/vercel)|
| `@traceai/mastra` | traceAI Instrumentation for Mastra. | [![npm](https://img.shields.io/npm/v/@traceai/mastra)](https://www.npmjs.com/package/@traceai/mastra)|
| `@traceai/mcp` | traceAI Instrumentation for Model Context Protocol. | [![npm](https://img.shields.io/npm/v/@traceai/mcp)](https://www.npmjs.com/package/@traceai/mcp)|

---

## 🔧 Compatibility Matrix

| Category | Supported Frameworks | Python | TypeScript |
|----------|---------------------|--------|------------|
| **LLM Providers** | OpenAI | ✅ | ✅ |
| | Anthropic | ✅ | ✅ |
| | AWS Bedrock | ✅ | ✅ |
| | Google Vertex AI | ✅ | - |
| | Google Generative AI | ✅ | - |
| | Mistral AI | ✅ | - |
| | Groq | ✅ | - |
| | LiteLLM | ✅ | - |
| **Agent Frameworks** | LangChain | ✅ | ✅ |
| | LlamaIndex | ✅ | ✅ |
| | CrewAI | ✅ | - |
| | AutoGen | ✅ | - |
| | OpenAI Agents | ✅ | - |
| | Smol Agents | ✅ | - |
| | Mastra | - | ✅ |
| **Tools & Libraries** | Haystack | ✅ | - |
| | DSPy | ✅ | - |
| | Guardrails AI | ✅ | - |
| | Instructor | ✅ | - |
| | Portkey | ✅ | - |
| | Pipecat | ✅ | - |
| | LiveKit | ✅ | - |
| | Vercel AI SDK | - | ✅ |
| **Standards** | Model Context Protocol (MCP) | ✅ | ✅ |
| **Vector Databases** | Pinecone | ✅ | - |
| | ChromaDB | ✅ | - |
| | Qdrant | ✅ | - |
| | Weaviate | ✅ | - |
| | Milvus | ✅ | - |
| | LanceDB | ✅ | - |
| | MongoDB Atlas Vector | ✅ | - |
| | pgvector | ✅ | - |
| | Redis Vector | ✅ | - |

> **Legend:** ✅ Supported | - Not yet available

---

## 🏗️ Architecture

traceAI is built on top of OpenTelemetry and follows standard OTel instrumentation patterns. This means you get:

**🔌 Full OpenTelemetry Compatibility**
- Works with any OTel-compatible backend
- Standard OTLP exporters (HTTP/gRPC)
- Compatible with existing OTel setups

**⚙️ Bring Your Own Configuration**

You can use traceAI with your own OpenTelemetry setup:

<details>
<summary><b>Python: Custom TracerProvider & Exporters</b></summary>

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from traceai_openai import OpenAIInstrumentor

# Set up your own tracer provider
tracer_provider = TracerProvider()
trace.set_tracer_provider(tracer_provider)

# Add custom exporters (example with Future AGI)
# HTTP endpoint
otlp_exporter = OTLPSpanExporter(
    endpoint="https://api.futureagi.com/tracer/v1/traces",
    headers={
        "X-API-KEY": "your-api-key",
        "X-SECRET-KEY": "your-secret-key"
    }
)
# Or use gRPC: OTLPSpanExporter(endpoint="grpc://grpc.futureagi.com:443", ...)
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

# Instrument with traceAI
OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
```
</details>

<details>
<summary><b>TypeScript: Custom TracerProvider, Span Processors & Headers</b></summary>

```typescript
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { OpenAIInstrumentation } from "@traceai/openai";

// Create custom tracer provider
const provider = new NodeTracerProvider({
  resource: new Resource({
    "service.name": "my-ai-service",
  }),
});

// Add custom OTLP exporter with headers (example with Future AGI)
// HTTP endpoint
const exporter = new OTLPTraceExporter({
  url: "https://api.futureagi.com/tracer/v1/traces",
  headers: {
    "X-API-KEY": process.env.FI_API_KEY!,
    "X-SECRET-KEY": process.env.FI_SECRET_KEY!,
  },
});
// Or use gRPC: new OTLPTraceExporter({ url: "grpc://grpc.futureagi.com:443", ... })

// Add span processor
provider.addSpanProcessor(new BatchSpanProcessor(exporter));
provider.register();

// Register traceAI instrumentation
registerInstrumentations({
  tracerProvider: provider,
  instrumentations: [new OpenAIInstrumentation()],
});
```
</details>

**📊 What Gets Captured**

traceAI automatically captures rich telemetry data:
- **Prompts & Completions**: Full request/response content
- **Token Usage**: Input, output, and total tokens
- **Model Parameters**: Temperature, top_p, max_tokens, etc.
- **Tool Calls**: Function/tool names, arguments, and results
- **Streaming**: Individual chunks with delta tracking
- **Errors**: Detailed error context and stack traces
- **Timing**: Latency at each step of the AI workflow

All data follows [OpenTelemetry Semantic Conventions for GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

---

## 🗺️ Roadmap

See our **[ROADMAP.md](ROADMAP.md)** for planned features and integrations:

### Coming Soon
| Category | Integrations |
|----------|-------------|
| **LLM Providers** | Ollama, Cohere, Together AI, HuggingFace |
| **Agent Frameworks** | LangGraph, Claude Agent SDK, Pydantic AI |
| **Enterprise SDKs** | Java (LangChain4j, Spring AI), Go |
| **Platform Features** | LLM Playground, Datasets & Experiments |

### Recently Added
| Category | Integrations |
|----------|-------------|
| **Vector DBs** | Pinecone, ChromaDB, Qdrant, Weaviate, Milvus, LanceDB, MongoDB Atlas, pgvector, Redis |

### Technical Documentation

For implementation details and architecture:
- [knowledge_base/INDEX.md](knowledge_base/INDEX.md) - Technical knowledge base
- [docs/SCHEMA_SEMANTIC_CONVENTIONS.md](docs/SCHEMA_SEMANTIC_CONVENTIONS.md) - Schema reference

---

## 🤝 Contributing

We welcome contributions from the community! 

**📖 Read our [Contributing Guide](CONTRIBUTING.md)** for detailed instructions on:
- Setting up your development environment (Python & TypeScript)
- Running tests and code quality checks
- Submitting pull requests
- Adding new framework integrations

**Quick Start:**
1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

**Found a bug?** Please [open an issue](https://github.com/future-agi/traceAI/issues/new) with:
- Framework version
- traceAI version
- Minimal reproduction code
- Expected vs actual behavior

**Request a feature?** Please [open an issue](https://github.com/future-agi/traceAI/issues/new) with:
- Use case or problem you're trying to solve
- Proposed solution or feature description
- Any relevant examples or mockups
- Priority level (nice-to-have vs critical)

---

## 📚 Resources

| Resource | Description |
|----------|-------------|
| 🌐 [Website](https://www.futureagi.com/) | Learn more about Future AGI |
| 📖 [Documentation](https://docs.futureagi.com/) | Complete guides and API reference |
| 👨‍🍳 [Cookbooks](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability) | Step-by-step implementation examples |
| 🗺️ [Roadmap](ROADMAP.md) | Planned features and integrations |
| 📝 [Changelog](CHANGELOG.md) | All release notes and updates |
| 📚 [Knowledge Base](knowledge_base/INDEX.md) | Technical documentation and architecture |
| 🤝 [Contributing Guide](CONTRIBUTING.md) | How to contribute to traceAI |
| 💬 [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) | Join our community |
| 🐛 [Issues](https://github.com/future-agi/traceAI/issues) | Report bugs or request features |

---

## 🌍 Connect With Us

<div align="center">

[![Website](https://img.shields.io/badge/Website-futureagi.com-blue?style=for-the-badge)](https://www.futureagi.com/)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Follow-0077B5?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/company/futureagi)
[![Twitter](https://img.shields.io/badge/Twitter-Follow-1DA1F2?style=for-the-badge&logo=x)](https://x.com/FutureAGI_)
[![Reddit](https://img.shields.io/badge/Reddit-Join-FF4500?style=for-the-badge&logo=reddit)](https://www.reddit.com/user/Future_AGI/submitted/)
[![Substack](https://img.shields.io/badge/Substack-Subscribe-FF6719?style=for-the-badge)](https://substack.com/@futureagi)

</div>

---

<div align="center">

**Built with ❤️ by the Future AGI team**

[⭐ Star us on GitHub](https://github.com/future-agi/traceAI) | [🐛 Report Bug](https://github.com/future-agi/traceAI/issues) | [💡 Request Feature](https://github.com/future-agi/traceAI/issues)

</div>
