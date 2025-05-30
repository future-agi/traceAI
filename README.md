# traceAI

traceAI is  OSS package to enable standardized tracing of AI applications and frameworks 

traceAI is a set of conventions and plugins that integrates seamlessly with OpenTelemetry to instrument and monitor code executions across models, frameworks, and vendors, mapping them to standardized trace attributes. 

traceAI is natively supported by Future AGI, but can be used with any OpenTelemetry-compatible backend as well. traceAI provides a set of instrumentations for popular machine learning SDKs and frameworks in a variety of languages like Langchain, OpenAI, Anthropic, and more.


## Table of Contents
- [Introduction](#introduction)
- [Features](#features)
- [Supported Frameworks](#supported-frameworks)
- [Quickstart](#quickstart)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Standardized Tracing**: Maps AI workflows to a consistent set of trace attributes and spans.
- **Framework Agnostic**: Works with popular AI frameworks like OpenAI, LangChain, Anthropic, and more.
- **OpenTelemetry Integration**: Seamlessly integrates with OpenTelemetry-compatible backends.
- **Extensible Plugins**: Easily add custom instrumentation for unsupported frameworks.
- **Future AGI Native Support**: Optimized for Future AGI's observability platform.


## Supported Frameworks



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



## Quickstart

### Install traceAI OpenAI

```bash
pip install traceAI-openai
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_app"
)
```

### Configure OpenAI Instrumentation
Set up your OpenAI client with built-in observability. This includes support for text, image, and audio models.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Components
Set up your OpenAI client with built-in observability.  

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

We welcome contributions from the community! To contribute:
1. Fork the repository.
2. Create a new branch (`git checkout -b feature/your-feature-name`).
3. Commit your changes (`git commit -m 'Add your feature'`).
4. Push to the branch (`git push origin feature/your-feature-name`).
5. Open a pull request.

## Resources

- Website: https://www.futureagi.com/
- Documentation: https://docs.futureagi.com/
- Cookbooks: [How-To-Implement-Observability](https://docs.futureagi.com/cookbook/cookbook8/How-To-Implement-Observability)

## Connect With Us

- LinkedIn: https://www.linkedin.com/company/futureagi
- Twitter: https://x.com/FutureAGI_
- Reddit: https://www.reddit.com/user/Future_AGI/submitted/
- Substack: https://substack.com/@futureagi
