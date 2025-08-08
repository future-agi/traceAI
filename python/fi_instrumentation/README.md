# fi-instrumentation-otel

Core OpenTelemetry instrumentation library for Python applications with advanced evaluation capabilities for AI systems.

## Overview

`fi-instrumentation-otel` provides a comprehensive tracing solution built on OpenTelemetry that's specifically designed for AI applications. It offers custom span exporters, evaluation tags, and seamless integration with the TraceAI platform for observability and performance monitoring.

## Features

- **OpenTelemetry Integration**: Built on top of OpenTelemetry APIs with custom implementations.
- **Custom Span Exporter**: HTTP-based span exporter with configurable endpoints.
- **AI Evaluation Tags**: Comprehensive evaluation system for AI applications with 50+ built-in evaluators.
- **Project Management**: Support for project versioning, sessions, and metadata.
- **Flexible Configuration**: Environment variable and programmatic configuration support.
- **Python Support**: Full Python support with comprehensive type definitions.


## Quick Start

### Installation

```bash
pip install fi-instrumentation-otel
```

### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
```

### Basic Setup

```python
from fi_instrumentation import register, ProjectType

# Initialize trace provider
tracer_provider = register(
  project_name='my-ai-project',
  project_type=ProjectType.OBSERVE,
)
```

### Create spans for your application
Refer to the [OpenTelemetry Python documentation](https://opentelemetry.io/docs/languages/python/) for more information on how to create spans.

```python
from fi_instrumentation import FITracer

tracer = FITracer(trace_provider.get_tracer(__name__))

with tracer.start_as_current_span("my-llm-span"):
    # Your code here
    span.set_attribute("my-attribute", "my-value")
```

Refer to the [FI Semantic Conventions](https://docs.futureagi.com/future-agi/products/observability/tracing-manual/semantic-conventions) on how to set attributes on your spans.


## Contributing

This package is part of the TraceAI project. Please refer to the main repository for contribution guidelines.

## Links

- [GitHub Repository](https://github.com/future-agi/traceAI)
- [Documentation](https://docs.futureagi.com)
