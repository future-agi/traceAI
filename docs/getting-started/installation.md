# Installation

This guide covers installing traceAI for Python and TypeScript applications.

## Prerequisites

- **Python**: 3.8 or higher
- **TypeScript/Node.js**: Node 18+ with TypeScript 5.0+
- **Future AGI Account**: Get API keys at [app.futureagi.com](https://app.futureagi.com)

## Python Installation

### Core Library

The core `fi-instrumentation` library is required for all Python integrations:

```bash
pip install fi-instrumentation
```

### Framework Packages

Install the package for your AI framework:

```bash
# LLM Providers
pip install traceai-openai          # OpenAI
pip install traceai-anthropic       # Anthropic
pip install traceai-mistralai       # Mistral AI
pip install traceai-groq            # Groq
pip install traceai-bedrock         # AWS Bedrock
pip install traceai-vertexai        # Google Vertex AI
pip install traceai-google-genai    # Google Generative AI
pip install traceai-litellm         # LiteLLM

# Agent Frameworks
pip install traceai-langchain       # LangChain
pip install traceai-llamaindex      # LlamaIndex
pip install traceai-crewai          # CrewAI
pip install traceai-autogen         # AutoGen
pip install traceai-openai-agents   # OpenAI Agents
pip install traceai-dspy            # DSPy

# Vector Databases
pip install traceai-pinecone        # Pinecone
pip install traceai-chromadb        # ChromaDB
pip install traceai-qdrant          # Qdrant
pip install traceai-weaviate        # Weaviate
```

### Optional Dependencies

For gRPC transport:
```bash
pip install fi-instrumentation[grpc]
```

## TypeScript Installation

### Core Library

```bash
npm install @traceai/fi-core
# or
pnpm add @traceai/fi-core
# or
yarn add @traceai/fi-core
```

### Framework Packages

```bash
# LLM Providers
npm install @traceai/openai         # OpenAI
npm install @traceai/anthropic      # Anthropic
npm install @traceai/bedrock        # AWS Bedrock

# Agent Frameworks
npm install @traceai/langchain      # LangChain
npm install @traceai/llamaindex     # LlamaIndex
npm install @traceai/vercel         # Vercel AI SDK
```

### Required Peer Dependencies

```bash
npm install @opentelemetry/api @opentelemetry/instrumentation
```

## Environment Setup

Set your API keys as environment variables:

```bash
# Required for Future AGI
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"

# Your LLM provider keys
export OPENAI_API_KEY="your-openai-key"
export ANTHROPIC_API_KEY="your-anthropic-key"
# etc.
```

Or in a `.env` file:

```env
FI_API_KEY=your-api-key
FI_SECRET_KEY=your-secret-key
OPENAI_API_KEY=your-openai-key
```

## Verify Installation

### Python

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

# Should not raise any errors
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="test"
)
print("Installation successful!")
```

### TypeScript

```typescript
import { register, ProjectType } from "@traceai/fi-core";

// Should not raise any errors
const tracerProvider = register({
    projectName: "test",
    projectType: ProjectType.OBSERVE,
});
console.log("Installation successful!");
```

## Next Steps

- [Python Quickstart](quickstart-python.md) - Start tracing Python applications
- [TypeScript Quickstart](quickstart-typescript.md) - Start tracing TypeScript applications
- [Configuration](../configuration/environment-variables.md) - Configure traceAI
