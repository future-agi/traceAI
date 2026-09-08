# TraceAI Instrumentation for Microsoft Semantic Kernel

This package provides OpenTelemetry instrumentation for the [Semantic Kernel](https://github.com/microsoft/semantic-kernel) framework in Python.

## Installation

```bash
pip install traceai-semantic-kernel
```

## Usage

```python
import asyncio
from semantic_kernel import Kernel
from traceai_semantic_kernel import SemanticKernelInstrumentor

# Instrument Semantic Kernel
SemanticKernelInstrumentor().instrument()

async def main():
    kernel = Kernel()
    # Add plugins and services to the kernel...
    # result = await kernel.invoke(function, inputs...)

if __name__ == "__main__":
    asyncio.run(main())
```
