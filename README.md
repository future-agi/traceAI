<div align="center">

![traceAI Logo](Logo.png)

# traceAI

**Open-source observability for AI applications - trace every LLM call, prompt, token, retrieval step, and agent decision.**

Built on [OpenTelemetry](https://opentelemetry.io/), traceAI sends structured traces to any OTel-compatible backend (Datadog, Grafana, Jaeger, Future AGI, and more). No new vendor. No new dashboard.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![TypeScript](https://img.shields.io/badge/typescript-5.0%2B-blue)](https://www.typescriptlang.org/)
[![Java](https://img.shields.io/badge/java-17%2B-orange)](https://openjdk.org/)
[![C#](https://img.shields.io/badge/C%23-.NET%2010-purple)](https://dotnet.microsoft.com/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-1.0+-purple)](https://opentelemetry.io/)

[![PyPI Downloads](https://img.shields.io/pypi/dm/fi-instrumentation-otel?label=PyPI%20downloads)](https://pypi.org/project/fi-instrumentation-otel/)
[![npm Downloads](https://img.shields.io/npm/dm/@traceai/fi-core?label=npm%20downloads)](https://www.npmjs.com/package/@traceai/fi-core)
[![NuGet Downloads](https://img.shields.io/nuget/dt/fi-instrumentation-otel?label=NuGet%20downloads)](https://www.nuget.org/packages/fi-instrumentation-otel)

[Documentation](https://docs.futureagi.com/) • [Examples](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability) • [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) • [PyPI](https://pypi.org/project/fi-instrumentation-otel/) • [npm](https://www.npmjs.com/package/@traceai/fi-core) • [NuGet](https://www.nuget.org/packages/fi-instrumentation-otel)

</div>

---

## What is traceAI?

**traceAI** is an open-source library that gives you full visibility into your AI applications. It captures every LLM call, prompt, token count, retrieval step, and agent decision as structured traces and sends them to whatever observability tool you already use.

It is built on **OpenTelemetry**, the industry standard for application observability. Your AI traces live natively in Datadog, Grafana, Jaeger, or any OTel-compatible backend.

- **Zero-config tracing** for 50+ AI frameworks across 4 languages
- **OpenTelemetry-native** — works with any OTel-compatible backend
- **Semantic conventions** for LLM calls, agents, tools, retrieval, and vector databases
- **Python, TypeScript, Java, and C#** support with consistent APIs

---

## Table of Contents

- [Key Features](#key-features)
- [Quickstart](#quickstart)
  - [Python](#python-quickstart)
  - [TypeScript](#typescript-quickstart)
  - [Java](#java-quickstart)
  - [C#](#c-quickstart)
- [Supported Frameworks](#supported-frameworks)
  - [Python](#python)
  - [TypeScript](#typescript)
  - [Java](#java)
  - [C#](#c)
- [Compatibility Matrix](#compatibility-matrix)
- [Architecture](#architecture)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Contributors](#contributors)
- [Resources](#resources)
- [Connect With Us](#connect-with-us)

## Key Features

| Feature | Description |
|---------|-------------|
| **Standardized Tracing** | Maps AI workflows to consistent OpenTelemetry spans and attributes |
| **Zero-Config Setup** | Drop-in instrumentation with minimal code changes |
| **Multi-Framework** | 50+ integrations across Python, TypeScript, Java, and C# |
| **Vendor Agnostic** | Works with any OpenTelemetry-compatible backend |
| **Rich Context** | Captures prompts, completions, tokens, model params, tool calls, and more |
| **Production Ready** | Async support, streaming, error handling, and performance optimized |

---

## Quickstart

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

> **Tip:** Swap `traceai-openai` for any supported framework (e.g., `traceai-langchain`, `traceai-anthropic`)

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

---

### Java Quickstart

**1. Add dependency** (via [JitPack](https://jitpack.io/#future-agi/traceAI))
```xml
<dependency>
    <groupId>com.github.future-agi.traceAI</groupId>
    <artifactId>traceai-java-openai</artifactId>
    <version>v1.0.0</version>
</dependency>
```

**2. Instrument your application**
```java
import ai.traceai.core.TraceAI;
import ai.traceai.openai.TracedOpenAIClient;

// Initialize tracing
TraceAI.init("my_ai_app", "your-api-key", "your-secret-key");

// Wrap your OpenAI client
var tracedClient = new TracedOpenAIClient(openAIClient);

// Use as normal - tracing happens automatically!
var response = tracedClient.chatCompletion(request);
```

---

### C# Quickstart

**1. Install**
```bash
dotnet add package fi-instrumentation-otel
```

**2. Instrument your application**
```csharp
using FIInstrumentation;

// Initialize tracing
var tracer = FITracer.Initialize(new FITracerOptions
{
    ProjectName = "my_ai_app",
    ApiKey = "your-api-key",
    SecretKey = "your-secret-key"
});

// Use the tracer with your AI calls
```

---

## Supported Frameworks

### Python

| Package | Description | Version |
|---------|-------------|----------|
| [`fi-instrumentation-otel`](https://pypi.org/project/fi-instrumentation-otel/) | Core instrumentation library | [![PyPI](https://img.shields.io/pypi/v/fi-instrumentation-otel)](https://pypi.org/project/fi-instrumentation-otel/) |

#### LLM Providers

| Package | Description | Version |
|---------|-------------|----------|
| [`traceAI-openai`](https://pypi.org/project/traceAI-openai/) | OpenAI | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai)](https://pypi.org/project/traceAI-openai/) |
| [`traceAI-anthropic`](https://pypi.org/project/traceAI-anthropic/) | Anthropic | [![PyPI](https://img.shields.io/pypi/v/traceAI-anthropic)](https://pypi.org/project/traceAI-anthropic/) |
| [`traceAI-google-genai`](https://pypi.org/project/traceAI-google-genai/) | Google Generative AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-genai)](https://pypi.org/project/traceAI-google-genai/) |
| [`traceAI-vertexai`](https://pypi.org/project/traceAI-vertexai/) | Google Vertex AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-vertexai)](https://pypi.org/project/traceAI-vertexai/) |
| [`traceAI-bedrock`](https://pypi.org/project/traceAI-bedrock/) | AWS Bedrock | [![PyPI](https://img.shields.io/pypi/v/traceAI-bedrock)](https://pypi.org/project/traceAI-bedrock/) |
| [`traceAI-mistralai`](https://pypi.org/project/traceAI-mistralai/) | Mistral AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-mistralai)](https://pypi.org/project/traceAI-mistralai/) |
| [`traceAI-groq`](https://pypi.org/project/traceAI-groq/) | Groq | [![PyPI](https://img.shields.io/pypi/v/traceAI-groq)](https://pypi.org/project/traceAI-groq/) |
| [`traceAI-litellm`](https://pypi.org/project/traceAI-litellm/) | LiteLLM | [![PyPI](https://img.shields.io/pypi/v/traceAI-litellm)](https://pypi.org/project/traceAI-litellm/) |
| [`traceAI-cohere`](https://pypi.org/project/traceAI-cohere/) | Cohere | [![PyPI](https://img.shields.io/pypi/v/traceAI-cohere)](https://pypi.org/project/traceAI-cohere/) |
| [`traceAI-ollama`](https://pypi.org/project/traceAI-ollama/) | Ollama | [![PyPI](https://img.shields.io/pypi/v/traceAI-ollama)](https://pypi.org/project/traceAI-ollama/) |
| [`traceAI-together`](https://pypi.org/project/traceAI-together/) | Together AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-together)](https://pypi.org/project/traceAI-together/) |
| [`traceAI-deepseek`](https://pypi.org/project/traceAI-deepseek/) | DeepSeek | [![PyPI](https://img.shields.io/pypi/v/traceAI-deepseek)](https://pypi.org/project/traceAI-deepseek/) |
| [`traceAI-fireworks`](https://pypi.org/project/traceAI-fireworks/) | Fireworks AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-fireworks)](https://pypi.org/project/traceAI-fireworks/) |
| [`traceAI-cerebras`](https://pypi.org/project/traceAI-cerebras/) | Cerebras | [![PyPI](https://img.shields.io/pypi/v/traceAI-cerebras)](https://pypi.org/project/traceAI-cerebras/) |
| [`traceAI-huggingface`](https://pypi.org/project/traceAI-huggingface/) | HuggingFace | [![PyPI](https://img.shields.io/pypi/v/traceAI-huggingface)](https://pypi.org/project/traceAI-huggingface/) |
| [`traceAI-xai`](https://pypi.org/project/traceAI-xai/) | xAI (Grok) | [![PyPI](https://img.shields.io/pypi/v/traceAI-xai)](https://pypi.org/project/traceAI-xai/) |
| [`traceAI-vllm`](https://pypi.org/project/traceAI-vllm/) | vLLM | [![PyPI](https://img.shields.io/pypi/v/traceAI-vllm)](https://pypi.org/project/traceAI-vllm/) |

#### Agent Frameworks

| Package | Description | Version |
|---------|-------------|----------|
| [`traceAI-langchain`](https://pypi.org/project/traceAI-langchain/) | LangChain | [![PyPI](https://img.shields.io/pypi/v/traceAI-langchain)](https://pypi.org/project/traceAI-langchain/) |
| [`traceAI-llamaindex`](https://pypi.org/project/traceAI-llamaindex/) | LlamaIndex | [![PyPI](https://img.shields.io/pypi/v/traceAI-llamaindex)](https://pypi.org/project/traceAI-llamaindex/) |
| [`traceAI-crewai`](https://pypi.org/project/traceAI-crewai/) | CrewAI | [![PyPI](https://img.shields.io/pypi/v/traceAI-crewai)](https://pypi.org/project/traceAI-crewai/) |
| [`traceAI-openai-agents`](https://pypi.org/project/traceAI-openai-agents/) | OpenAI Agents | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai-agents)](https://pypi.org/project/traceAI-openai-agents/) |
| [`traceAI-smolagents`](https://pypi.org/project/traceAI-smolagents/) | SmolAgents | [![PyPI](https://img.shields.io/pypi/v/traceAI-smolagents)](https://pypi.org/project/traceAI-smolagents/) |
| [`traceAI-autogen`](https://pypi.org/project/traceAI-autogen/) | AutoGen | [![PyPI](https://img.shields.io/pypi/v/traceAI-autogen)](https://pypi.org/project/traceAI-autogen/) |
| [`traceAI-google-adk`](https://pypi.org/project/traceAI-google-adk/) | Google ADK | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-adk)](https://pypi.org/project/traceAI-google-adk/) |
| [`traceAI-agno`](https://pypi.org/project/traceAI-agno/) | Agno | [![PyPI](https://img.shields.io/pypi/v/traceAI-agno)](https://pypi.org/project/traceAI-agno/) |
| [`traceAI-pydantic-ai`](https://pypi.org/project/traceAI-pydantic-ai/) | Pydantic AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-pydantic-ai)](https://pypi.org/project/traceAI-pydantic-ai/) |
| [`traceAI-claude-agent-sdk`](https://pypi.org/project/traceAI-claude-agent-sdk/) | Claude Agent SDK | [![PyPI](https://img.shields.io/pypi/v/traceAI-claude-agent-sdk)](https://pypi.org/project/traceAI-claude-agent-sdk/) |
| [`traceAI-strands`](https://pypi.org/project/traceAI-strands/) | AWS Strands Agents | [![PyPI](https://img.shields.io/pypi/v/traceAI-strands)](https://pypi.org/project/traceAI-strands/) |
| [`traceAI-beeai`](https://pypi.org/project/traceAI-beeai/) | IBM BeeAI | [![PyPI](https://img.shields.io/pypi/v/traceAI-beeai)](https://pypi.org/project/traceAI-beeai/) |

#### Tools and Libraries

| Package | Description | Version |
|---------|-------------|----------|
| [`traceAI-haystack`](https://pypi.org/project/traceAI-haystack/) | Haystack | [![PyPI](https://img.shields.io/pypi/v/traceAI-haystack)](https://pypi.org/project/traceAI-haystack/) |
| [`traceAI-dspy`](https://pypi.org/project/traceAI-dspy/) | DSPy | [![PyPI](https://img.shields.io/pypi/v/traceAI-dspy)](https://pypi.org/project/traceAI-dspy/) |
| [`traceAI-guardrails`](https://pypi.org/project/traceAI-guardrails/) | Guardrails AI | [![PyPI](https://img.shields.io/pypi/v/traceAI-guardrails)](https://pypi.org/project/traceAI-guardrails/) |
| [`traceAI-instructor`](https://pypi.org/project/traceAI-instructor/) | Instructor | [![PyPI](https://img.shields.io/pypi/v/traceAI-instructor)](https://pypi.org/project/traceAI-instructor/) |
| [`traceAI-portkey`](https://pypi.org/project/traceAI-portkey/) | Portkey | [![PyPI](https://img.shields.io/pypi/v/traceAI-portkey)](https://pypi.org/project/traceAI-portkey/) |
| [`traceAI-mcp`](https://pypi.org/project/traceAI-mcp/) | Model Context Protocol | [![PyPI](https://img.shields.io/pypi/v/traceAI-mcp)](https://pypi.org/project/traceAI-mcp/) |
| [`traceAI-pipecat`](https://pypi.org/project/traceAI-pipecat/) | Pipecat (Voice AI) | [![PyPI](https://img.shields.io/pypi/v/traceAI-pipecat)](https://pypi.org/project/traceAI-pipecat/) |
| [`traceAI-livekit`](https://pypi.org/project/traceAI-livekit/) | LiveKit (Real-time) | [![PyPI](https://img.shields.io/pypi/v/traceAI-livekit)](https://pypi.org/project/traceAI-livekit/) |

#### Vector Databases

| Package | Description | Version |
|---------|-------------|----------|
| [`traceAI-pinecone`](https://pypi.org/project/traceAI-pinecone/) | Pinecone | [![PyPI](https://img.shields.io/pypi/v/traceAI-pinecone)](https://pypi.org/project/traceAI-pinecone/) |
| [`traceAI-chromadb`](https://pypi.org/project/traceAI-chromadb/) | ChromaDB | [![PyPI](https://img.shields.io/pypi/v/traceAI-chromadb)](https://pypi.org/project/traceAI-chromadb/) |
| [`traceAI-qdrant`](https://pypi.org/project/traceAI-qdrant/) | Qdrant | [![PyPI](https://img.shields.io/pypi/v/traceAI-qdrant)](https://pypi.org/project/traceAI-qdrant/) |
| [`traceAI-weaviate`](https://pypi.org/project/traceAI-weaviate/) | Weaviate | [![PyPI](https://img.shields.io/pypi/v/traceAI-weaviate)](https://pypi.org/project/traceAI-weaviate/) |
| [`traceAI-milvus`](https://pypi.org/project/traceAI-milvus/) | Milvus | [![PyPI](https://img.shields.io/pypi/v/traceAI-milvus)](https://pypi.org/project/traceAI-milvus/) |
| [`traceAI-lancedb`](https://pypi.org/project/traceAI-lancedb/) | LanceDB | [![PyPI](https://img.shields.io/pypi/v/traceAI-lancedb)](https://pypi.org/project/traceAI-lancedb/) |
| [`traceAI-mongodb`](https://pypi.org/project/traceAI-mongodb/) | MongoDB Atlas Vector Search | [![PyPI](https://img.shields.io/pypi/v/traceAI-mongodb)](https://pypi.org/project/traceAI-mongodb/) |
| [`traceAI-pgvector`](https://pypi.org/project/traceAI-pgvector/) | pgvector (PostgreSQL) | [![PyPI](https://img.shields.io/pypi/v/traceAI-pgvector)](https://pypi.org/project/traceAI-pgvector/) |
| [`traceAI-redis`](https://pypi.org/project/traceAI-redis/) | Redis Vector Search | [![PyPI](https://img.shields.io/pypi/v/traceAI-redis)](https://pypi.org/project/traceAI-redis/) |

---

### TypeScript

| Package | Description | Version |
|---------|-------------|----------|
| [`@traceai/fi-core`](https://www.npmjs.com/package/@traceai/fi-core) | Core instrumentation library | [![npm](https://img.shields.io/npm/v/@traceai/fi-core)](https://www.npmjs.com/package/@traceai/fi-core) |
| [`@traceai/fi-semantic-conventions`](https://www.npmjs.com/package/@traceai/fi-semantic-conventions) | Semantic conventions | [![npm](https://img.shields.io/npm/v/@traceai/fi-semantic-conventions)](https://www.npmjs.com/package/@traceai/fi-semantic-conventions) |

#### LLM Providers

| Package | Description | Version |
|---------|-------------|----------|
| [`@traceai/openai`](https://www.npmjs.com/package/@traceai/openai) | OpenAI | [![npm](https://img.shields.io/npm/v/@traceai/openai)](https://www.npmjs.com/package/@traceai/openai) |
| [`@traceai/anthropic`](https://www.npmjs.com/package/@traceai/anthropic) | Anthropic | [![npm](https://img.shields.io/npm/v/@traceai/anthropic)](https://www.npmjs.com/package/@traceai/anthropic) |
| [`@traceai/google-genai`](https://www.npmjs.com/package/@traceai/google-genai) | Google Generative AI | [![npm](https://img.shields.io/npm/v/@traceai/google-genai)](https://www.npmjs.com/package/@traceai/google-genai) |
| [`@traceai/fi-instrumentation-vertexai`](https://www.npmjs.com/package/@traceai/fi-instrumentation-vertexai) | Google Vertex AI | [![npm](https://img.shields.io/npm/v/@traceai/fi-instrumentation-vertexai)](https://www.npmjs.com/package/@traceai/fi-instrumentation-vertexai) |
| [`@traceai/bedrock`](https://www.npmjs.com/package/@traceai/bedrock) | AWS Bedrock | [![npm](https://img.shields.io/npm/v/@traceai/bedrock)](https://www.npmjs.com/package/@traceai/bedrock) |
| [`@traceai/mistral`](https://www.npmjs.com/package/@traceai/mistral) | Mistral AI | [![npm](https://img.shields.io/npm/v/@traceai/mistral)](https://www.npmjs.com/package/@traceai/mistral) |
| [`@traceai/groq`](https://www.npmjs.com/package/@traceai/groq) | Groq | [![npm](https://img.shields.io/npm/v/@traceai/groq)](https://www.npmjs.com/package/@traceai/groq) |
| [`@traceai/cohere`](https://www.npmjs.com/package/@traceai/cohere) | Cohere | [![npm](https://img.shields.io/npm/v/@traceai/cohere)](https://www.npmjs.com/package/@traceai/cohere) |
| [`@traceai/ollama`](https://www.npmjs.com/package/@traceai/ollama) | Ollama | [![npm](https://img.shields.io/npm/v/@traceai/ollama)](https://www.npmjs.com/package/@traceai/ollama) |
| [`@traceai/together`](https://www.npmjs.com/package/@traceai/together) | Together AI | [![npm](https://img.shields.io/npm/v/@traceai/together)](https://www.npmjs.com/package/@traceai/together) |
| [`@traceai/deepseek`](https://www.npmjs.com/package/@traceai/deepseek) | DeepSeek | [![npm](https://img.shields.io/npm/v/@traceai/deepseek)](https://www.npmjs.com/package/@traceai/deepseek) |
| [`@traceai/fireworks`](https://www.npmjs.com/package/@traceai/fireworks) | Fireworks AI | [![npm](https://img.shields.io/npm/v/@traceai/fireworks)](https://www.npmjs.com/package/@traceai/fireworks) |
| [`@traceai/cerebras`](https://www.npmjs.com/package/@traceai/cerebras) | Cerebras | [![npm](https://img.shields.io/npm/v/@traceai/cerebras)](https://www.npmjs.com/package/@traceai/cerebras) |
| [`@traceai/huggingface`](https://www.npmjs.com/package/@traceai/huggingface) | HuggingFace | [![npm](https://img.shields.io/npm/v/@traceai/huggingface)](https://www.npmjs.com/package/@traceai/huggingface) |
| [`@traceai/xai`](https://www.npmjs.com/package/@traceai/xai) | xAI (Grok) | [![npm](https://img.shields.io/npm/v/@traceai/xai)](https://www.npmjs.com/package/@traceai/xai) |
| [`@traceai/vllm`](https://www.npmjs.com/package/@traceai/vllm) | vLLM | [![npm](https://img.shields.io/npm/v/@traceai/vllm)](https://www.npmjs.com/package/@traceai/vllm) |

#### Agent Frameworks

| Package | Description | Version |
|---------|-------------|----------|
| [`@traceai/langchain`](https://www.npmjs.com/package/@traceai/langchain) | LangChain.js | [![npm](https://img.shields.io/npm/v/@traceai/langchain)](https://www.npmjs.com/package/@traceai/langchain) |
| [`@traceai/llamaindex`](https://www.npmjs.com/package/@traceai/llamaindex) | LlamaIndex | [![npm](https://img.shields.io/npm/v/@traceai/llamaindex)](https://www.npmjs.com/package/@traceai/llamaindex) |
| [`@traceai/openai-agents`](https://www.npmjs.com/package/@traceai/openai-agents) | OpenAI Agents | [![npm](https://img.shields.io/npm/v/@traceai/openai-agents)](https://www.npmjs.com/package/@traceai/openai-agents) |
| [`@traceai/fi-instrumentation-google-adk`](https://www.npmjs.com/package/@traceai/fi-instrumentation-google-adk) | Google ADK | [![npm](https://img.shields.io/npm/v/@traceai/fi-instrumentation-google-adk)](https://www.npmjs.com/package/@traceai/fi-instrumentation-google-adk) |
| [`@traceai/mastra`](https://www.npmjs.com/package/@traceai/mastra) | Mastra | [![npm](https://img.shields.io/npm/v/@traceai/mastra)](https://www.npmjs.com/package/@traceai/mastra) |
| [`@traceai/beeai`](https://www.npmjs.com/package/@traceai/beeai) | IBM BeeAI | [![npm](https://img.shields.io/npm/v/@traceai/beeai)](https://www.npmjs.com/package/@traceai/beeai) |
| [`@traceai/strands`](https://www.npmjs.com/package/@traceai/strands) | AWS Strands Agents | [![npm](https://img.shields.io/npm/v/@traceai/strands)](https://www.npmjs.com/package/@traceai/strands) |

#### Tools and Libraries

| Package | Description | Version |
|---------|-------------|----------|
| [`@traceai/vercel`](https://www.npmjs.com/package/@traceai/vercel) | Vercel AI SDK | [![npm](https://img.shields.io/npm/v/@traceai/vercel)](https://www.npmjs.com/package/@traceai/vercel) |
| [`@traceai/guardrails`](https://www.npmjs.com/package/@traceai/guardrails) | Guardrails AI | [![npm](https://img.shields.io/npm/v/@traceai/guardrails)](https://www.npmjs.com/package/@traceai/guardrails) |
| [`@traceai/instructor`](https://www.npmjs.com/package/@traceai/instructor) | Instructor | [![npm](https://img.shields.io/npm/v/@traceai/instructor)](https://www.npmjs.com/package/@traceai/instructor) |
| [`@traceai/portkey`](https://www.npmjs.com/package/@traceai/portkey) | Portkey | [![npm](https://img.shields.io/npm/v/@traceai/portkey)](https://www.npmjs.com/package/@traceai/portkey) |
| [`@traceai/mcp`](https://www.npmjs.com/package/@traceai/mcp) | Model Context Protocol | [![npm](https://img.shields.io/npm/v/@traceai/mcp)](https://www.npmjs.com/package/@traceai/mcp) |
| [`@traceai/fi-instrumentation-pipecat`](https://www.npmjs.com/package/@traceai/fi-instrumentation-pipecat) | Pipecat (Voice AI) | [![npm](https://img.shields.io/npm/v/@traceai/fi-instrumentation-pipecat)](https://www.npmjs.com/package/@traceai/fi-instrumentation-pipecat) |
| [`@traceai/fi-instrumentation-livekit`](https://www.npmjs.com/package/@traceai/fi-instrumentation-livekit) | LiveKit (Real-time) | [![npm](https://img.shields.io/npm/v/@traceai/fi-instrumentation-livekit)](https://www.npmjs.com/package/@traceai/fi-instrumentation-livekit) |

#### Vector Databases

| Package | Description | Version |
|---------|-------------|----------|
| [`@traceai/pinecone`](https://www.npmjs.com/package/@traceai/pinecone) | Pinecone | [![npm](https://img.shields.io/npm/v/@traceai/pinecone)](https://www.npmjs.com/package/@traceai/pinecone) |
| [`@traceai/chromadb`](https://www.npmjs.com/package/@traceai/chromadb) | ChromaDB | [![npm](https://img.shields.io/npm/v/@traceai/chromadb)](https://www.npmjs.com/package/@traceai/chromadb) |
| [`@traceai/qdrant`](https://www.npmjs.com/package/@traceai/qdrant) | Qdrant | [![npm](https://img.shields.io/npm/v/@traceai/qdrant)](https://www.npmjs.com/package/@traceai/qdrant) |
| [`@traceai/weaviate`](https://www.npmjs.com/package/@traceai/weaviate) | Weaviate | [![npm](https://img.shields.io/npm/v/@traceai/weaviate)](https://www.npmjs.com/package/@traceai/weaviate) |
| [`@traceai/milvus`](https://www.npmjs.com/package/@traceai/milvus) | Milvus | [![npm](https://img.shields.io/npm/v/@traceai/milvus)](https://www.npmjs.com/package/@traceai/milvus) |
| [`@traceai/lancedb`](https://www.npmjs.com/package/@traceai/lancedb) | LanceDB | [![npm](https://img.shields.io/npm/v/@traceai/lancedb)](https://www.npmjs.com/package/@traceai/lancedb) |
| [`@traceai/mongodb`](https://www.npmjs.com/package/@traceai/mongodb) | MongoDB Atlas Vector Search | [![npm](https://img.shields.io/npm/v/@traceai/mongodb)](https://www.npmjs.com/package/@traceai/mongodb) |
| [`@traceai/pgvector`](https://www.npmjs.com/package/@traceai/pgvector) | pgvector (PostgreSQL) | [![npm](https://img.shields.io/npm/v/@traceai/pgvector)](https://www.npmjs.com/package/@traceai/pgvector) |
| [`@traceai/redis`](https://www.npmjs.com/package/@traceai/redis) | Redis Vector Search | [![npm](https://img.shields.io/npm/v/@traceai/redis)](https://www.npmjs.com/package/@traceai/redis) |

---

### Java

Available via [JitPack](https://jitpack.io/#future-agi/traceAI). Add the JitPack repository:
```xml
<repositories>
    <repository>
        <id>jitpack.io</id>
        <url>https://jitpack.io</url>
    </repository>
</repositories>
```

| Package | Description |
|---------|-------------|
| `traceai-java-core` | Core instrumentation library |

#### LLM Providers

| Package | Description |
|---------|-------------|
| `traceai-java-openai` | OpenAI |
| `traceai-java-azure-openai` | Azure OpenAI |
| `traceai-java-anthropic` | Anthropic |
| `traceai-java-google-genai` | Google Generative AI |
| `traceai-java-cohere` | Cohere |
| `traceai-java-ollama` | Ollama |
| `traceai-java-bedrock` | AWS Bedrock |
| `traceai-java-vertexai` | Google Vertex AI |
| `traceai-java-watsonx` | IBM Watsonx |

#### Agent Frameworks

| Package | Description |
|---------|-------------|
| `traceai-langchain4j` | LangChain4j |
| `traceai-spring-ai` | Spring AI |
| `traceai-spring-boot-starter` | Spring Boot Auto-Configuration |
| `traceai-java-semantic-kernel` | Microsoft Semantic Kernel |

#### Vector Databases

| Package | Description |
|---------|-------------|
| `traceai-java-pinecone` | Pinecone |
| `traceai-java-qdrant` | Qdrant |
| `traceai-java-milvus` | Milvus |
| `traceai-java-weaviate` | Weaviate |
| `traceai-java-chromadb` | ChromaDB |
| `traceai-java-mongodb` | MongoDB Atlas Vector Search |
| `traceai-java-redis` | Redis Vector Search |
| `traceai-java-azure-search` | Azure AI Search |
| `traceai-java-pgvector` | pgvector (PostgreSQL) |
| `traceai-java-elasticsearch` | Elasticsearch |

---

### C#

Available on [NuGet](https://www.nuget.org/packages/fi-instrumentation-otel).

| Package | Description | Version |
|---------|-------------|----------|
| [`fi-instrumentation-otel`](https://www.nuget.org/packages/fi-instrumentation-otel) | Core instrumentation library | [![NuGet](https://img.shields.io/nuget/v/fi-instrumentation-otel)](https://www.nuget.org/packages/fi-instrumentation-otel) |

---

## Compatibility Matrix

| Category | Framework | Python | TypeScript | Java | C# |
|----------|-----------|--------|------------|------|-----|
| **LLM Providers** | OpenAI | ✅ | ✅ | ✅ | ✅ |
| | Anthropic | ✅ | ✅ | ✅ | |
| | AWS Bedrock | ✅ | ✅ | ✅ | |
| | Google Vertex AI | ✅ | ✅ | ✅ | |
| | Google Generative AI | ✅ | ✅ | ✅ | |
| | Mistral AI | ✅ | ✅ | | |
| | Groq | ✅ | ✅ | | |
| | Cohere | ✅ | ✅ | ✅ | |
| | Ollama | ✅ | ✅ | ✅ | |
| | LiteLLM | ✅ | | | |
| | Together AI | ✅ | ✅ | | |
| | DeepSeek | ✅ | ✅ | | |
| | Fireworks AI | ✅ | ✅ | | |
| | Cerebras | ✅ | ✅ | | |
| | HuggingFace | ✅ | ✅ | | |
| | xAI (Grok) | ✅ | ✅ | | |
| | vLLM | ✅ | ✅ | | |
| | Azure OpenAI | | | ✅ | |
| | IBM Watsonx | | | ✅ | |
| **Agent Frameworks** | LangChain | ✅ | ✅ | | |
| | LlamaIndex | ✅ | ✅ | | |
| | CrewAI | ✅ | | | |
| | AutoGen | ✅ | | | |
| | OpenAI Agents | ✅ | ✅ | | |
| | SmolAgents | ✅ | | | |
| | Google ADK | ✅ | ✅ | | |
| | Agno | ✅ | | | |
| | Pydantic AI | ✅ | | | |
| | Claude Agent SDK | ✅ | | | |
| | AWS Strands Agents | ✅ | ✅ | | |
| | IBM BeeAI | ✅ | ✅ | | |
| | Mastra | | ✅ | | |
| | LangChain4j | | | ✅ | |
| | Spring AI | | | ✅ | |
| | Semantic Kernel | | | ✅ | |
| **Tools & Libraries** | Haystack | ✅ | | | |
| | DSPy | ✅ | | | |
| | Guardrails AI | ✅ | ✅ | | |
| | Instructor | ✅ | ✅ | | |
| | Portkey | ✅ | ✅ | | |
| | Vercel AI SDK | | ✅ | | |
| | MCP | ✅ | ✅ | | |
| | Pipecat | ✅ | ✅ | | |
| | LiveKit | ✅ | ✅ | | |
| **Vector Databases** | Pinecone | ✅ | ✅ | ✅ | |
| | ChromaDB | ✅ | ✅ | ✅ | |
| | Qdrant | ✅ | ✅ | ✅ | |
| | Weaviate | ✅ | ✅ | ✅ | |
| | Milvus | ✅ | ✅ | ✅ | |
| | LanceDB | ✅ | ✅ | | |
| | MongoDB Atlas | ✅ | ✅ | ✅ | |
| | pgvector | ✅ | ✅ | ✅ | |
| | Redis | ✅ | ✅ | ✅ | |
| | Azure AI Search | | | ✅ | |
| | Elasticsearch | | | ✅ | |

> **Legend:** ✅ Supported | blank = not yet available

---

## Architecture

traceAI is built on top of OpenTelemetry and follows standard OTel instrumentation patterns:

**Full OpenTelemetry Compatibility**
- Works with any OTel-compatible backend
- Standard OTLP exporters (HTTP/gRPC)
- Compatible with existing OTel setups

**Bring Your Own Configuration**

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
otlp_exporter = OTLPSpanExporter(
    endpoint="https://api.futureagi.com/tracer/v1/traces",
    headers={
        "X-API-KEY": "your-api-key",
        "X-SECRET-KEY": "your-secret-key"
    }
)
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

const provider = new NodeTracerProvider({
  resource: new Resource({ "service.name": "my-ai-service" }),
});

const exporter = new OTLPTraceExporter({
  url: "https://api.futureagi.com/tracer/v1/traces",
  headers: {
    "X-API-KEY": process.env.FI_API_KEY!,
    "X-SECRET-KEY": process.env.FI_SECRET_KEY!,
  },
});

provider.addSpanProcessor(new BatchSpanProcessor(exporter));
provider.register();

registerInstrumentations({
  tracerProvider: provider,
  instrumentations: [new OpenAIInstrumentation()],
});
```
</details>

**What Gets Captured**

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

## Roadmap

- **Go language support**
- **Sampling strategies** for high-volume production environments
- **Continuous semantic convention updates** as the OTel GenAI spec evolves
- **Evaluation integration** connecting traces to quality measurement pipelines
- **Expanded agent framework coverage**

See our **[ROADMAP.md](ROADMAP.md)** for the full roadmap.

---

## Contributing

We welcome contributions! Read our **[Contributing Guide](CONTRIBUTING.md)** for details.

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

**Found a bug?** [Open an issue](https://github.com/future-agi/traceAI/issues/new) with a minimal reproduction.

---

## Contributors

<a href="https://github.com/future-agi/traceAI/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=future-agi/traceAI" />
</a>

---

## Resources

| Resource | Description |
|----------|-------------|
| [Website](https://www.futureagi.com/) | Learn more about Future AGI |
| [Documentation](https://docs.futureagi.com/) | Complete guides and API reference |
| [Cookbooks](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability) | Step-by-step implementation examples |
| [Roadmap](ROADMAP.md) | Planned features and integrations |
| [Changelog](CHANGELOG.md) | All release notes and updates |
| [Contributing Guide](CONTRIBUTING.md) | How to contribute to traceAI |
| [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) | Join our community |
| [Issues](https://github.com/future-agi/traceAI/issues) | Report bugs or request features |

---

## Connect With Us

<div align="center">

[![Website](https://img.shields.io/badge/Website-futureagi.com-blue?style=for-the-badge)](https://www.futureagi.com/)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Follow-0077B5?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/company/futureagi)
[![Twitter](https://img.shields.io/badge/Twitter-Follow-1DA1F2?style=for-the-badge&logo=x)](https://x.com/FutureAGI_)
[![Reddit](https://img.shields.io/badge/Reddit-Join-FF4500?style=for-the-badge&logo=reddit)](https://www.reddit.com/user/Future_AGI/submitted/)
[![Substack](https://img.shields.io/badge/Substack-Subscribe-FF6719?style=for-the-badge)](https://substack.com/@futureagi)

</div>

---

<div align="center">

**Built with care by the [Future AGI](https://www.futureagi.com/) team**

[Star us on GitHub](https://github.com/future-agi/traceAI) | [Report Bug](https://github.com/future-agi/traceAI/issues) | [Request Feature](https://github.com/future-agi/traceAI/issues)

</div>
