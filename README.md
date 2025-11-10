# traceAI

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?logo=javascript&logoColor=black)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

[![Docs](https://img.shields.io/badge/Docs-Documentation-brightgreen)](https://docs.futureagi.com/future-agi/products/observability/concept/traceai)
![GitHub stars](https://img.shields.io/github/stars/future-agi/traceAI?style=social)

**Open-source (OSS)** observability toolkit for AI workflows - production-grade, standardized, and framework-agnostic.

## Table of Contents
- [Introduction](#introduction)
- [Features](#features)
- [Supported Frameworks](#supported-frameworks)
- [Quickstart](#quickstart)
- [Contributing](#contributing)
- [Resources](#resources)
- [Connect With Us](#connect-with-us)
- [License](#license)

## Introduction

`traceAI` is an **open-source (OSS)** project for **standardized tracing of AI applications and frameworks**. 

It provides a set of **conventions and plugins** that complement [**OpenTelemetry**](https://github.com/open-telemetry/opentelemetry-python), enabling instrumentation and monitoring of code executions across models, frameworks, and vendors. All traces and spans are mapped to a consistent set of standardized attributes, making observability simple and uniform.

The project currently consists of multiple Python packages (e.g., `traceAI-openai`, `traceAI-langchain`, `traceAI-anthropic`) that implement these conventions for popular AI SDKs and frameworks. Each package integrates seamlessly with OpenTelemetry, establishing a consistent observability pipeline across projects.

While `traceAI` is natively supported by [**Future AGI**](https://github.com/future-agi), its packages can be used with any OpenTelemetry-compatible backend. These packages help developers **instrument AI workflows, monitor performance, and maintain observability** across different frameworks and vendors with minimal effort.

## Features

| Feature | Description |
|:--|:--|
| **OpenTelemetry Integration** | Native [OpenTelemetry](https://github.com/open-telemetry/opentelemetry-python) support enables standardized, end-to-end tracing |
| **Unified Trace Format** | Consistent schema for evaluations and executions, independent of framework or model type |
| **Framework-Agnostic** | Works out of the box with LangChain, LlamaIndex, DSPy, Haystack, and custom pipelines |
| **Standardized Metrics** | Captures latency, accuracy, reliability, and cost metrics for structured benchmarking |
| **Multi-Modal Tracing** | Supports text, audio, and image models with shared context identifiers |
| **Interoperable Design** | Connects with observability platforms and visualization dashboards |
| **Lightweight Implementation** | Minimal dependencies for easy embedding in existing stacks |
| **Extensible Schema** | Supports custom span attributes and metadata for organization-specific workflows |
| **Extensible Plugins** | Easily add custom instrumentation for unsupported frameworks and adapters |
| **Open & Developer-First** | Fully open-source and community-driven, maintained with native support from [**Future AGI**](https://github.com/future-agi) |

## Supported Frameworks

`traceAI` provides drop-in instrumentation for popular AI frameworks - enabling **standardized tracing** across models, frameworks, and vendors.

### Major LLM Vendors
Standardized instrumentation for major large language model vendors - enabling consistent tracing across models.

| Framework | Instrumentation Package | Python (PyPI) |
|-----------|-----------------------|---------|
| [OpenAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai) | `traceAI-openai` | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai)](https://pypi.org/project/traceAI-openai) |
| [Anthropic](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/anthropic) | `traceAI-anthropic` | [![PyPI](https://img.shields.io/pypi/v/traceAI-anthropic)](https://pypi.org/project/traceAI-anthropic) |
| [MistralAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/mistralai) | `traceAI-mistralai` | [![PyPI](https://img.shields.io/pypi/v/traceAI-mistralai)](https://pypi.org/project/traceAI-mistralai) |
| [Groq](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/groq) | `traceAI-groq` | [![PyPI](https://img.shields.io/pypi/v/traceAI-groq)](https://pypi.org/project/traceAI-groq) |
| [AWS Bedrock](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/bedrock) | `traceAI-bedrock` | [![PyPI](https://img.shields.io/pypi/v/traceAI-bedrock)](https://pypi.org/project/traceAI-bedrock) |

### Google AI Services
Instrumentation for Google AI services, providing unified trace observability.

| Framework | Instrumentation Package | Python (PyPI) |
|-----------|-----------------------|---------|
| [VertexAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/vertexai) | `traceAI-vertexai` | [![PyPI](https://img.shields.io/pypi/v/traceAI-vertexai)](https://pypi.org/project/traceAI-vertexai) |
| [Google GenAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/google_genai) | `traceAI-google-genai` | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-genai)](https://pypi.org/project/traceAI-google-genai) |
| [Google ADK](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/google_adk) | `traceAI-google-adk` | [![PyPI](https://img.shields.io/pypi/v/traceAI-google-adk)](https://pypi.org/project/traceAI-google-adk) |

### Agent & RAG Frameworks
Trace instrumentation for agents and retrieval-augmented generation frameworks - supporting multi-modal pipelines.

| Framework | Instrumentation Package | Python (PyPI) |
|-----------|-----------------------|---------|
| [LlamaIndex](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/llamaindex) | `traceAI-llamaindex` | [![PyPI](https://img.shields.io/pypi/v/traceAI-llamaindex)](https://pypi.org/project/traceAI-llamaindex) |
| [LangChain](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/langchain) | `traceAI-langchain` | [![PyPI](https://img.shields.io/pypi/v/traceAI-langchain)](https://pypi.org/project/traceAI-langchain) |
| [Autogen](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/autogen) | `traceAI-autogen` | [![PyPI](https://img.shields.io/pypi/v/traceAI-autogen)](https://pypi.org/project/traceAI-autogen) |
| [CrewAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/crewai) | `traceAI-crewai` | [![PyPI](https://img.shields.io/pypi/v/traceAI-crewai)](https://pypi.org/project/traceAI-crewai) |
| [Haystack](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/haystack) | `traceAI-haystack` | [![PyPI](https://img.shields.io/pypi/v/traceAI-haystack)](https://pypi.org/project/traceAI-haystack) |
| [DSPy](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/dspy) | `traceAI-dspy` | [![PyPI](https://img.shields.io/pypi/v/traceAI-dspy)](https://pypi.org/project/traceAI-dspy) |
| [Guardrails](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/guardrails) | `traceAI-guardrails` | [![PyPI](https://img.shields.io/pypi/v/traceAI-guardrails)](https://pypi.org/project/traceAI-guardrails) |
| [OpenAI Agents](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents) | `traceAI-openai-agents` | [![PyPI](https://img.shields.io/pypi/v/traceAI-openai-agents)](https://pypi.org/project/traceAI-openai-agents) |
| [SmolAgents](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/smol_agents) | `traceAI-smolagents` | [![PyPI](https://img.shields.io/pypi/v/traceAI-smolagents)](https://pypi.org/project/traceAI-smolagents) |

### Utilities & Plugins
Supplementary instrumentation and utility packages for workflow integration and plugin support.

| Framework | Instrumentation Package | Python (PyPI) |
|-----------|-----------------------|---------|
| [LiteLLM](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/litellm) | `traceAI-litellm` | [![PyPI](https://img.shields.io/pypi/v/traceAI-litellm)](https://pypi.org/project/traceAI-litellm) | 
| [Instructor](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/instructor) | `traceAI-instructor` | [![PyPI](https://img.shields.io/pypi/v/traceAI-instructor)](https://pypi.org/project/traceAI-instructor) |
| [PortKey](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/portkey) | `traceAI-portkey` | [![PyPI](https://img.shields.io/pypi/v/traceAI-portkey)](https://pypi.org/project/traceAI-portkey) |
| [MCP](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/mcp) | `traceAI-mcp` | [![PyPI](https://img.shields.io/pypi/v/traceAI-mcp)](https://pypi.org/project/traceAI-mcp) |

### 🟦 JavaScript / TypeScript Packages (npm)

Standardized instrumentation for AI frameworks - built for Node.js and TypeScript developers.

> ⚙️ More traceAI integrations for JS/TS frameworks are actively being developed - stay tuned.

| Framework | Instrumentation Package | npm |
|-----------|------------------------|:---:|
| [OpenAI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai) | `@traceai/openai` | [![npm](https://img.shields.io/npm/v/@traceai/openai.svg)](https://www.npmjs.com/package/@traceai/openai) |
| [Anthropic](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/anthropic) | `@traceai/anthropic` | [![npm](https://img.shields.io/npm/v/@traceai/anthropic.svg)](https://www.npmjs.com/package/@traceai/anthropic) |
| [AWS Bedrock](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/bedrock) | `@traceai/bedrock` | [![npm](https://img.shields.io/npm/v/@traceai/bedrock.svg)](https://www.npmjs.com/package/@traceai/bedrock) |
| [LangChain](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/langchain) | `@traceai/langchain` | [![npm](https://img.shields.io/npm/v/@traceai/langchain.svg)](https://www.npmjs.com/package/@traceai/langchain) |
| [LlamaIndex](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/llamaindex) | `@traceai/llamaindex` | [![npm](https://img.shields.io/npm/v/@traceai/llamaindex.svg)](https://www.npmjs.com/package/@traceai/llamaindex) |
| [MCP](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/mcp) | `@traceai/mcp` | [![npm](https://img.shields.io/npm/v/@traceai/mcp.svg)](https://www.npmjs.com/package/@traceai/mcp) |




## Quickstart

### OpenAI Implementation

#### 1. Install traceAI OpenAI

```bash
pip install traceAI-openai
```

#### 2. Set Environment Variables
Set up your environment variables to authenticate with Future AGI and OpenAI. This must be done before running any instrumented code.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

#### 3. Register Tracer Provider
Register a trace provider to establish the observability pipeline for your application. All instrumented calls will report traces via this provider.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_app"
)
```

#### 4. Configure OpenAI Instrumentation
Enable traceAI instrumentation for OpenAI models (text, image, audio). This wraps the OpenAI client so that all API calls are automatically traced.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

#### 5. Create OpenAI Components
Run your OpenAI client as usual. This example demonstrates that instrumentation is active and traces will be generated for the request.

```python
import openai

openai.api_key = os.environ["OPENAI_API_KEY"]

response = openai.ChatCompletion.create(
    model="gpt-4o",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Can you tell me a joke?"}
    ]
)

print(response.choices[0].message['content'].strip())
```

## Contributing

We welcome contributions from the community! Follow these steps to contribute:

#### 1. **Fork** the repository
Create your own copy of the repository on GitHub by clicking the **Fork** button.

#### 2. Sync with Upstream (Optional but Recommended)
Before starting work, ensure your local copy is up-to-date with the main repository:

```bash
git fetch upstream
git checkout main
git merge upstream/main
```

#### 3. **Create a branch**
Create a new branch for your feature or bug fix:

```bash
git checkout -b feature/your-feature-name
```
    
#### 4. **Make Changes and Commit**
Make your changes locally. Stage and commit them with a descriptive message:

```bash
git add .
git commit -m "Add feature/bug description"
```

#### 5. **Push Your Branch**
Push your branch to your fork on GitHub:

```bash
git push origin feature/your-feature-name
```

#### 6. **Open a Pull Request**
Go to your fork on GitHub and open a Pull Request against the upstream repository’s main branch.
Include a clear description of your changes, reference any related issues, and follow the project’s contribution guidelines.

## Resources

- **Official Website:** [https://www.futureagi.com](https://www.futureagi.com)  
- **Documentation:** [https://docs.futureagi.com](https://docs.futureagi.com)
- **Integrations:** [https://docs.futureagi.com/future-agi/integrations/overview](https://docs.futureagi.com/future-agi/integrations/overview)
- **Cookbooks:** [How-To Implement Observability](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability)  

> ⚡ **Tip:** Explore the Integrations for ready-to-use recipes for instrumenting AI pipelines across frameworks and modalities.

## Connect With Us

Stay updated and connect with the [**Future AGI**](https://github.com/future-agi) community:

- **LinkedIn:** [https://www.linkedin.com/company/futureagi](https://www.linkedin.com/company/futureagi)  
- **Twitter/X:** [https://x.com/FutureAGI_](https://x.com/FutureAGI_)  
- **Reddit:** [https://www.reddit.com/user/Future_AGI/submitted/](https://www.reddit.com/user/Future_AGI/submitted/)  
- **Substack:** [https://substack.com/@futureagi](https://substack.com/@futureagi)

Developed and maintained by [**Future AGI**](https://github.com/future-agi).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

---
⭐ If you find `traceAI` useful, please consider giving it a star on [GitHub](https://github.com/future-agi/traceAI)!
