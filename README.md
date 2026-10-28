<div align="center">

![traceAI Logo](Logo.png)

# traceAI

**Open-source observability for AI applications - trace every LLM call, prompt, token, retrieval step, and agent decision.**

Built on [OpenTelemetry](https://opentelemetry.io/), traceAI sends structured traces to any OTel-compatible backend (Datadog, Grafana, Jaeger, Future AGI, and more). No new vendor. No new dashboard.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![TypeScript](https://img.shields.io/badge/typescript-5.0%2B-blue)](https://www.typescriptlang.org/)
[![Java](https://img.shields.io/badge/java-17%2B-orange)](https://openjdk.org/)
[![C#](https://img.shields.io/badge/C%23-.NET%2010-purple)](https://dotnet.microsoft.com/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-1.0+-purple)](https://opentelemetry.io/)

[![PyPI Downloads](https://img.shields.io/pypi/dm/fi-instrumentation-otel?label=PyPI%20downloads)](https://pypi.org/project/fi-instrumentation-otel/)
[![npm Downloads](https://img.shields.io/npm/dm/@traceai/fi-core?label=npm%20downloads)](https://www.npmjs.com/package/@traceai/fi-core)
[![NuGet Downloads](https://img.shields.io/nuget/dt/fi-instrumentation-otel?label=NuGet%20downloads)](https://www.nuget.org/packages/fi-instrumentation-otel)

[Documentation](https://docs.futureagi.com/integrations/overview) • [Examples](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability) • [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) • [PyPI](https://pypi.org/project/fi-instrumentation-otel/) • [npm](https://www.npmjs.com/package/@traceai/fi-core) • [NuGet](https://www.nuget.org/packages/fi-instrumentation-otel)

</div>

---

<div align="center">
  <img src="trace-repo.gif" alt="traceAI Demo" width="70%" />
</div>

---

## What is traceAI?

Your agent calls an LLM, retrieves context, invokes a tool, and returns an answer. When that answer is wrong, you need to know exactly where it broke - which retrieval missed, which tool returned stale data, which prompt drifted.

**traceAI** captures every LLM call, prompt, token count, retrieval step, and agent decision as structured OpenTelemetry traces. Your traces live natively in Datadog, Grafana, Future AGI, Jaeger, or any OTel-compatible backend. No new vendor. No new dashboard.

- **Drop-in instrumentation** for 50+ AI frameworks across 5 languages
- **OpenTelemetry-native** - works with any OTel-compatible backend
- **Semantic conventions** for LLM calls, agents, tools, retrieval, and vector databases
- **Python, TypeScript, Java, Go, and C#** with consistent APIs

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
  - [Go](#go)
  - [C#](#c)
  - [Integration recipes](#integration-recipes)
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
| **Drop-in Setup** | Add 3 lines to your existing code - no refactoring needed |
| **Multi-Framework** | 50+ integrations across Python, TypeScript, Java, Go, and C# |
| **Vendor Agnostic** | Works with any OpenTelemetry-compatible backend |
| **Rich Context** | Captures prompts, completions, tokens, model params, tool calls, and more |
| **Production-grade** | Async support, streaming, error handling, and low-overhead tracing |

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

# Use OpenAI as normal - traces are captured automatically
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

// Use OpenAI as normal - traces are captured automatically
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
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.openai.TracedOpenAIClient;

// Initialize tracing
TraceAI.init(TraceConfig.builder()
    .apiKey("your-api-key")
    .secretKey("your-secret-key")
    .projectName("my_ai_app")
    .build());

// Wrap your OpenAI client
var tracedClient = new TracedOpenAIClient(openAIClient);

// Use as normal - traces are captured automatically
var response = tracedClient.createChatCompletion(request);
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
using FIInstrumentation.Types;

// Register tracer
var tracer = TraceAI.Register(opts =>
{
    opts.ProjectName = "my_ai_app";
    opts.ProjectType = ProjectType.Observe;
    opts.ApiKey = "your-api-key";
    opts.SecretKey = "your-secret-key";
});

// Use the tracer with your AI calls
```

---

## Supported Frameworks

Catalog links point to the integration source and setup guidance in this repository.

### Python

| Package | Description |
|---------|-------------|
| [`fi-instrumentation-otel`](python/) | Core instrumentation library |
| [`traceai`](python/traceai/) | One-line setup SDK |

#### LLM Providers

| Package | Integration |
|---------|-------------|
| [`traceAI-anthropic`](python/frameworks/anthropic/) | Anthropic |
| [`traceAI-bedrock`](python/frameworks/bedrock/) | AWS Bedrock |
| [`traceAI-cerebras`](python/frameworks/cerebras/) | Cerebras |
| [`traceAI-cohere`](python/frameworks/cohere/) | Cohere |
| [`traceAI-deepseek`](python/frameworks/deepseek/) | DeepSeek |
| [`traceAI-fireworks`](python/frameworks/fireworks/) | Fireworks AI |
| [`traceAI-google-genai`](python/frameworks/google-genai/) | Google Gen AI |
| [`traceAI-vertexai`](python/frameworks/vertexai/) | Google Vertex AI |
| [`traceAI-groq`](python/frameworks/groq/) | Groq |
| [`traceAI-huggingface`](python/frameworks/huggingface/) | Hugging Face |
| [`traceAI-litellm`](python/frameworks/litellm/) | LiteLLM |
| [`traceAI-minimax`](python/frameworks/minimax/) | MiniMax |
| [`traceAI-mistralai`](python/frameworks/mistralai/) | Mistral AI |
| [`traceAI-ollama`](python/frameworks/ollama/) | Ollama |
| [`traceAI-openai`](python/frameworks/openai/) | OpenAI |
| [`traceAI-openai-realtime`](python/frameworks/openai-realtime/) | OpenAI Realtime |
| [`traceAI-replicate`](python/frameworks/replicate/) | Replicate |
| [`traceAI-together`](python/frameworks/together/) | Together AI |
| [`traceAI-vllm`](python/frameworks/vllm/) | vLLM |
| [`traceAI-voyage`](python/frameworks/voyage/) | Voyage AI |
| [`traceAI-xai`](python/frameworks/xai/) | xAI (Grok) |

#### Agent Frameworks

| Package | Integration |
|---------|-------------|
| [`traceAI-ag2`](python/frameworks/ag2/) | AG2 |
| [`traceAI-ag2-classic`](python/frameworks/ag2-classic/) | AG2 Classic |
| [`traceAI-a2a`](python/frameworks/a2a/) | Agent-to-Agent (A2A) |
| [`traceai-agno`](python/frameworks/agno/) | Agno |
| [`traceAI-autogen`](python/frameworks/autogen/) | AutoGen |
| [`traceai-strands`](python/frameworks/strands/) | AWS Strands Agents |
| [`traceai-claude-agent-sdk`](python/frameworks/claude-agent-sdk/) | Claude Agent SDK |
| [`traceAI-crewai`](python/frameworks/crewai/) | CrewAI |
| [`traceAI-elevenlabs-agents`](python/frameworks/elevenlabs-agents/) | ElevenLabs Agents |
| [`traceAI-google-adk`](python/frameworks/google-adk/) | Google ADK |
| [`traceAI-hermes-agent`](python/frameworks/hermes-agent/) | Hermes Agent |
| [`traceai-beeai`](python/frameworks/beeai/) | IBM BeeAI |
| [`traceAI-langchain`](python/frameworks/langchain/) | LangChain |
| [`traceAI-llamaindex`](python/frameworks/llama_index/) | LlamaIndex |
| [`traceAI-mcp-use`](python/frameworks/mcp-use/) | mcp-use |
| [`traceAI-agent-framework`](python/frameworks/agent-framework/) | Microsoft Agent Framework |
| [`traceAI-semantic-kernel`](python/frameworks/semantic-kernel/) | Microsoft Semantic Kernel |
| [`traceAI-openai-agents`](python/frameworks/openai-agents/) | OpenAI Agents |
| [`traceai-pydantic-ai`](python/frameworks/pydantic-ai/) | Pydantic AI |
| [`traceAI-smolagents`](python/frameworks/smolagents/) | SmolAgents |

#### Tools and Libraries

| Package | Integration |
|---------|-------------|
| [`traceAI-DSPy`](python/frameworks/dspy/) | DSPy |
| [`traceAI-exa`](python/frameworks/exa/) | Exa |
| [`traceAI-firecrawl`](python/frameworks/firecrawl/) | Firecrawl |
| [`traceAI-discoveryengine`](python/frameworks/discoveryengine/) | Google Discovery Engine (Vertex AI Search) |
| [`traceAI-guardrails`](python/frameworks/guardrails/) | Guardrails AI |
| [`traceAI-haystack`](python/frameworks/haystack/) | Haystack |
| [`traceAI-instructor`](python/frameworks/instructor/) | Instructor |
| [`traceAI-livekit`](python/frameworks/livekit/) | LiveKit |
| [`traceAI-mcp`](python/frameworks/mcp/) | Model Context Protocol (MCP) |
| [`traceAI-parallel`](python/frameworks/parallel/) | Parallel |
| [`traceAI-pipecat`](python/frameworks/pipecat/) | Pipecat |
| [`traceAI-portkey`](python/frameworks/portkey/) | Portkey |
| [`traceAI-tavily`](python/frameworks/tavily/) | Tavily |
| [`traceAI-typesafe`](python/frameworks/typesafe/) | TypeSafe |

#### Vector Databases

| Package | Integration |
|---------|-------------|
| [`traceAI-chromadb`](python/frameworks/chromadb/) | ChromaDB |
| [`traceAI-lancedb`](python/frameworks/lancedb/) | LanceDB |
| [`traceAI-milvus`](python/frameworks/milvus/) | Milvus |
| [`traceAI-mongodb`](python/frameworks/mongodb-vector/) | MongoDB Atlas Vector Search |
| [`traceAI-pgvector`](python/frameworks/pgvector/) | pgvector |
| [`traceAI-pinecone`](python/frameworks/pinecone/) | Pinecone |
| [`traceAI-qdrant`](python/frameworks/qdrant/) | Qdrant |
| [`traceAI-redis`](python/frameworks/redis-vector/) | Redis Vector Search |
| [`traceAI-weaviate`](python/frameworks/weaviate/) | Weaviate |

---

### TypeScript

| Package | Description |
|---------|-------------|
| [`@traceai/fi-core`](typescript/packages/fi-core/) | Core instrumentation library |
| [`@traceai/fi-semantic-conventions`](typescript/packages/fi-semantic-conventions/) | Semantic conventions |

#### LLM Providers

| Package | Integration |
|---------|-------------|
| [`@traceai/anthropic`](typescript/packages/traceai_anthropic/) | Anthropic |
| [`@traceai/bedrock`](typescript/packages/traceai_bedrock/) | AWS Bedrock |
| [`@traceai/cerebras`](typescript/packages/traceai_cerebras/) | Cerebras |
| [`@traceai/cohere`](typescript/packages/traceai_cohere/) | Cohere |
| [`@traceai/deepseek`](typescript/packages/traceai_deepseek/) | DeepSeek |
| [`@traceai/fireworks`](typescript/packages/traceai_fireworks/) | Fireworks AI |
| [`@traceai/google-genai`](typescript/packages/traceai_google_genai/) | Google Gen AI |
| [`@traceai/fi-instrumentation-vertexai`](typescript/packages/traceai_vertexai/) | Google Vertex AI |
| [`@traceai/groq`](typescript/packages/traceai_groq/) | Groq |
| [`@traceai/huggingface`](typescript/packages/traceai_huggingface/) | Hugging Face |
| [`@traceai/mistral`](typescript/packages/traceai_mistral/) | Mistral AI |
| [`@traceai/ollama`](typescript/packages/traceai_ollama/) | Ollama |
| [`@traceai/openai`](typescript/packages/traceai_openai/) | OpenAI |
| [`@traceai/together`](typescript/packages/traceai_together/) | Together AI |
| [`@traceai/vllm`](typescript/packages/traceai_vllm/) | vLLM |
| [`@traceai/xai`](typescript/packages/traceai_xai/) | xAI (Grok) |

#### Agent Frameworks

| Package | Integration |
|---------|-------------|
| [`@traceai/strands`](typescript/packages/traceai_strands/) | AWS Strands Agents |
| [`@traceai/claude-agent-sdk`](typescript/packages/traceai_claude_agent_sdk/) | Claude Agent SDK |
| [`@traceai/codex-sdk`](typescript/packages/traceai_codex_sdk/) | Codex SDK |
| [`@traceai/genkit`](typescript/packages/traceai_genkit/) | Firebase Genkit |
| [`@traceai/fi-instrumentation-google-adk`](typescript/packages/traceai_google_adk/) | Google ADK |
| [`@traceai/beeai`](typescript/packages/traceai_beeai/) | IBM BeeAI |
| [`@traceai/langchain`](typescript/packages/traceai_langchain/) | LangChain |
| [`@traceai/llamaindex`](typescript/packages/traceai_llamaindex/) | LlamaIndex |
| [`@traceai/mastra`](typescript/packages/traceai_mastra/) | Mastra |
| [`@traceai/openai-agents`](typescript/packages/traceai_openai_agents/) | OpenAI Agents |
| [`@traceai/pi-coding-agent`](typescript/packages/traceai_pi_coding_agent/) | Pi Coding Agent |
| [`@traceai/voltagent`](typescript/packages/traceai_voltagent/) | VoltAgent |

#### Tools and Libraries

| Package | Integration |
|---------|-------------|
| [`@traceai/guardrails`](typescript/packages/traceai_guardrails/) | Guardrails AI |
| [`@traceai/instructor`](typescript/packages/traceai_instructor/) | Instructor |
| [`@traceai/fi-instrumentation-livekit`](typescript/packages/traceai_livekit/) | LiveKit |
| [`@traceai/mcp`](typescript/packages/traceai_mcp/) | Model Context Protocol (MCP) |
| [`@traceai/fi-instrumentation-pipecat`](typescript/packages/traceai_pipecat/) | Pipecat |
| [`@traceai/portkey`](typescript/packages/traceai_portkey/) | Portkey |
| [`@traceai/vercel`](typescript/packages/traceai_vercel/) | Vercel AI SDK |

#### Vector Databases

| Package | Integration |
|---------|-------------|
| [`@traceai/chromadb`](typescript/packages/traceai_chromadb/) | ChromaDB |
| [`@traceai/lancedb`](typescript/packages/traceai_lancedb/) | LanceDB |
| [`@traceai/milvus`](typescript/packages/traceai_milvus/) | Milvus |
| [`@traceai/mongodb`](typescript/packages/traceai_mongodb/) | MongoDB Atlas Vector Search |
| [`@traceai/pgvector`](typescript/packages/traceai_pgvector/) | pgvector |
| [`@traceai/pinecone`](typescript/packages/traceai_pinecone/) | Pinecone |
| [`@traceai/qdrant`](typescript/packages/traceai_qdrant/) | Qdrant |
| [`@traceai/redis`](typescript/packages/traceai_redis/) | Redis Vector Search |
| [`@traceai/weaviate`](typescript/packages/traceai_weaviate/) | Weaviate |

---

### Java

See the [Java SDK](java/) for Maven setup and package configuration.

| Package | Description |
|---------|-------------|
| [`traceai-java-core`](java/traceai-java-core/) | Core instrumentation library |

#### LLM Providers

| Package | Integration |
|---------|-------------|
| [`traceai-java-anthropic`](java/traceai-java-anthropic/) | Anthropic |
| [`traceai-java-bedrock`](java/traceai-java-bedrock/) | AWS Bedrock |
| [`traceai-java-azure-openai`](java/traceai-java-azure-openai/) | Azure OpenAI |
| [`traceai-java-cohere`](java/traceai-java-cohere/) | Cohere |
| [`traceai-java-google-genai`](java/traceai-java-google-genai/) | Google Gen AI |
| [`traceai-java-vertexai`](java/traceai-java-vertexai/) | Google Vertex AI |
| [`traceai-java-watsonx`](java/traceai-java-watsonx/) | IBM watsonx |
| [`traceai-java-ollama`](java/traceai-java-ollama/) | Ollama |
| [`traceai-java-openai`](java/traceai-java-openai/) | OpenAI |

#### Agent Frameworks

| Package | Integration |
|---------|-------------|
| [`traceai-langchain4j`](java/traceai-langchain4j/) | LangChain4j |
| [`traceai-java-semantic-kernel`](java/traceai-java-semantic-kernel/) | Microsoft Semantic Kernel |
| [`traceai-spring-ai`](java/traceai-spring-ai/) | Spring AI |
| [`traceai-spring-boot-starter`](java/traceai-spring-boot-starter/) | Spring Boot Auto-Configuration |

#### Vector Databases

| Package | Integration |
|---------|-------------|
| [`traceai-java-azure-search`](java/traceai-java-azure-search/) | Azure AI Search |
| [`traceai-java-chromadb`](java/traceai-java-chromadb/) | ChromaDB |
| [`traceai-java-elasticsearch`](java/traceai-java-elasticsearch/) | Elasticsearch |
| [`traceai-java-milvus`](java/traceai-java-milvus/) | Milvus |
| [`traceai-java-mongodb`](java/traceai-java-mongodb/) | MongoDB Atlas Vector Search |
| [`traceai-java-pgvector`](java/traceai-java-pgvector/) | pgvector |
| [`traceai-java-pinecone`](java/traceai-java-pinecone/) | Pinecone |
| [`traceai-java-qdrant`](java/traceai-java-qdrant/) | Qdrant |
| [`traceai-java-redis`](java/traceai-java-redis/) | Redis Vector Search |
| [`traceai-java-weaviate`](java/traceai-java-weaviate/) | Weaviate |

---

### Go

Go integrations use separate modules. Follow each module's README for SDK versions and setup.

| Package | Integration |
|---------|-------------|
| [`github.com/future-agi/traceAI/go/a2a`](go/a2a/) | Agent-to-Agent (A2A) |
| [`github.com/future-agi/traceAI/go/anthropic`](go/anthropic/) | Anthropic |
| [`github.com/future-agi/traceAI/go/eino`](go/eino/) | Eino |
| [`github.com/future-agi/traceAI/go/openai`](go/openai/) | OpenAI |

---

### C#

| Package | Description |
|---------|-------------|
| [`fi-instrumentation-otel`](csharp/) | .NET instrumentation library |

---

### Integration recipes

These recipes reuse SDK instrumentation or configure a native tracing/export path; they are not separate provider packages.

| Recipe | Guide |
|--------|-------|
| Anannas AI | [Setup](python/examples/anannas-ai/) |
| Azure AI Foundry | [Setup](python/examples/azure-ai-foundry/) |
| Cognee | [Setup](python/examples/cognee/) |
| Databricks | [Setup](python/examples/databricks/) |
| Doubleword | [Setup](python/examples/doubleword/) |
| EverOS | [Setup](python/examples/everos/) |
| Gradio | [Setup](python/examples/gradio/) |
| LangSmith trace forwarding | [Setup](python/examples/langsmith/) |
| NVIDIA DGX Cloud Lepton | [Setup](python/examples/lepton/) |
| MLflow | [Setup](python/examples/mlflow/) |
| OpenInference OTLP forwarding | [Setup](python/examples/openinference/) |
| OpenInference instrumented application | [Setup](python/examples/openinference-live/) |
| OpenLIT | [Setup](python/examples/openlit/) |
| OpenLLMetry (Traceloop SDK) | [Setup](python/examples/openllmetry/) |
| OrcaRouter | [Setup](python/examples/orcarouter/) |
| PrismML Bonsai | [Setup](python/examples/prismml/) |
| Deep Agents (via LangChain) | [Setup](python/frameworks/langchain/README.md#deep-agents) |
| TanStack AI (TypeScript) | [Setup](typescript/examples/tanstack-ai/) |
| Arconia (Java) | [Setup](java/examples/arconia-example/) |
| Quarkus LangChain4j (Java) | [Setup](java/examples/quarkus-langchain4j-example/) |

| Application integration | Guide |
|-------------------------|-------|
| openwebui | [Setup](integrations/openwebui/) |

---

## Compatibility Matrix

This matrix describes the framework-specific packages in the catalog. The [C# SDK](csharp/) provides generic tracing APIs. Recipes are listed separately above.

| Category | Integration | Python | TypeScript | Java | Go |
|----------|-------------|--------|------------|------|----|
| **LLM Providers** | Anthropic | Yes | Yes | Yes | Yes |
|  | AWS Bedrock | Yes | Yes | Yes |  |
|  | Azure OpenAI |  |  | Yes |  |
|  | Cerebras | Yes | Yes |  |  |
|  | Cohere | Yes | Yes | Yes |  |
|  | DeepSeek | Yes | Yes |  |  |
|  | Fireworks AI | Yes | Yes |  |  |
|  | Google Gen AI | Yes | Yes | Yes |  |
|  | Google Vertex AI | Yes | Yes | Yes |  |
|  | Groq | Yes | Yes |  |  |
|  | Hugging Face | Yes | Yes |  |  |
|  | IBM watsonx |  |  | Yes |  |
|  | LiteLLM | Yes |  |  |  |
|  | MiniMax | Yes |  |  |  |
|  | Mistral AI | Yes | Yes |  |  |
|  | Ollama | Yes | Yes | Yes |  |
|  | OpenAI | Yes | Yes | Yes | Yes |
|  | OpenAI Realtime | Yes |  |  |  |
|  | Replicate | Yes |  |  |  |
|  | Together AI | Yes | Yes |  |  |
|  | vLLM | Yes | Yes |  |  |
|  | Voyage AI | Yes |  |  |  |
|  | xAI (Grok) | Yes | Yes |  |  |
| **Agent Frameworks** | AG2 | Yes |  |  |  |
|  | AG2 Classic | Yes |  |  |  |
|  | Agent-to-Agent (A2A) | Yes |  |  | Yes |
|  | Agno | Yes |  |  |  |
|  | AutoGen | Yes |  |  |  |
|  | AWS Strands Agents | Yes | Yes |  |  |
|  | Claude Agent SDK | Yes | Yes |  |  |
|  | Codex SDK |  | Yes |  |  |
|  | CrewAI | Yes |  |  |  |
|  | Eino |  |  |  | Yes |
|  | ElevenLabs Agents | Yes |  |  |  |
|  | Firebase Genkit |  | Yes |  |  |
|  | Google ADK | Yes | Yes |  |  |
|  | Hermes Agent | Yes |  |  |  |
|  | IBM BeeAI | Yes | Yes |  |  |
|  | LangChain | Yes | Yes |  |  |
|  | LangChain4j |  |  | Yes |  |
|  | LlamaIndex | Yes | Yes |  |  |
|  | Mastra |  | Yes |  |  |
|  | mcp-use | Yes |  |  |  |
|  | Microsoft Agent Framework | Yes |  |  |  |
|  | Microsoft Semantic Kernel | Yes |  | Yes |  |
|  | OpenAI Agents | Yes | Yes |  |  |
|  | Pi Coding Agent |  | Yes |  |  |
|  | Pydantic AI | Yes |  |  |  |
|  | SmolAgents | Yes |  |  |  |
|  | Spring AI |  |  | Yes |  |
|  | Spring Boot Auto-Configuration |  |  | Yes |  |
|  | VoltAgent |  | Yes |  |  |
| **Tools and Libraries** | DSPy | Yes |  |  |  |
|  | Exa | Yes |  |  |  |
|  | Firecrawl | Yes |  |  |  |
|  | Google Discovery Engine (Vertex AI Search) | Yes |  |  |  |
|  | Guardrails AI | Yes | Yes |  |  |
|  | Haystack | Yes |  |  |  |
|  | Instructor | Yes | Yes |  |  |
|  | LiveKit | Yes | Yes |  |  |
|  | Model Context Protocol (MCP) | Yes | Yes |  |  |
|  | Parallel | Yes |  |  |  |
|  | Pipecat | Yes | Yes |  |  |
|  | Portkey | Yes | Yes |  |  |
|  | Tavily | Yes |  |  |  |
|  | TypeSafe | Yes |  |  |  |
|  | Vercel AI SDK |  | Yes |  |  |
| **Vector Databases** | Azure AI Search |  |  | Yes |  |
|  | ChromaDB | Yes | Yes | Yes |  |
|  | Elasticsearch |  |  | Yes |  |
|  | LanceDB | Yes | Yes |  |  |
|  | Milvus | Yes | Yes | Yes |  |
|  | MongoDB Atlas Vector Search | Yes | Yes | Yes |  |
|  | pgvector | Yes | Yes | Yes |  |
|  | Pinecone | Yes | Yes | Yes |  |
|  | Qdrant | Yes | Yes | Yes |  |
|  | Redis Vector Search | Yes | Yes | Yes |  |
|  | Weaviate | Yes | Yes | Yes |  |

Blank cells indicate no framework-specific package in that language.

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

- **Sampling strategies** for high-volume production environments
- **Continuous semantic convention updates** as the OTel GenAI spec evolves
- **Evaluation integration** connecting traces to quality measurement pipelines
- **Expanded agent framework coverage**

Request features or report bugs on [GitHub Issues](https://github.com/future-agi/traceAI/issues).

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
| [Feature Requests & Bugs](https://github.com/future-agi/traceAI/issues) | Request features or report issues |
| [Changelog](CHANGELOG.md) | All release notes and updates |
| [Contributing Guide](CONTRIBUTING.md) | How to contribute to traceAI |
| [Slack](https://join.slack.com/t/future-agi/shared_invite/zt-3gqwrdf2p-4oj1LVPqkQIoiS_OSrFL2A) | Join our community |
| [Issues](https://github.com/future-agi/traceAI/issues) | Report bugs or request features |
| [TraceAI Workshop](https://colab.research.google.com/drive/1Z6QFUhxZir5lFemmfjF3NqOBIP6CYUWb#scrollTo=LZbiyQ2KwD-B) | Workshop on tracing and evaluation |

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

**Built with ❤️ by the [Future AGI](https://www.futureagi.com/) team and [contributors](https://github.com/future-agi/traceAI/graphs/contributors).**

If traceAI helps you debug faster, a ⭐ helps more teams find us.

[🌐 futureagi.com](https://futureagi.com) · [📖 docs.futureagi.com](https://docs.futureagi.com) · [☁️ app.futureagi.com](https://app.futureagi.com)

</div>
