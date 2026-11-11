# Pipecat OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with Pipecat applications. It enables tracing and monitoring of voice applications built with Pipecat, with automatic attribute mapping to Future AGI conventions.

## Installation

1. **Install traceAI Pipecat**

```bash
pip install traceAI-pipecat
```

### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI and Pipecat.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
```

## Quickstart

This method automatically updates your existing span exporters to include Pipecat attribute mapping.

#### Register Tracer Provider
Set up the trace provider to establish the observability pipeline:

```python
from fi_instrumentation.otel import register, Transport, ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="Pipecat Voice App",
    set_global_tracer_provider=True,
)
```

#### Install Attribute Mapping
Install attribute mapping to convert Pipecat attributes to Future AGI conventions:

```python
from traceai_pipecat import enable_http_attribute_mapping

# For HTTP transport
success = enable_http_attribute_mapping()

# For gRPC transport
from traceai_pipecat import enable_grpc_attribute_mapping
success = enable_grpc_attribute_mapping()

# Or specify transport explicitly via enum
from traceai_pipecat import enable_fi_attribute_mapping
from fi_instrumentation.otel import Transport
success = enable_fi_attribute_mapping(transport=Transport.HTTP)  # or Transport.GRPC
```

## Features

### Automatic Attribute Mapping
The integration automatically maps Pipecat-specific attributes to Future AGI conventions:

- **LLM Operations**: Maps `gen_ai.system`, `gen_ai.request.model` to `llm.provider`, `llm.model_name`
- **Input/Output**: Maps `input`, `output`, `transcript` to structured Future AGI format
- **Token Usage**: Maps `gen_ai.usage.*` to `llm.token_count.*`
- **Tools**: Maps tool-related attributes to Future AGI tool conventions
- **Session Data**: Maps conversation and session information
- **Metadata**: Consolidates miscellaneous attributes into structured metadata

### Transport Support
- **HTTP**: Full support for HTTP transport with automatic endpoint detection
- **gRPC**: Support for gRPC transport (requires `fi-instrumentation[grpc]`)

### Span Kind Detection
Automatically determines the appropriate `fi.span.kind` based on span attributes:
- `LLM`: For LLM, STT, and TTS operations
- `TOOL`: For tool calls and results
- `AGENT`: For setup and configuration spans
- `CHAIN`: For turn and conversation spans

## Examples

### Complete HTTP Integration Example

```python
from fi_instrumentation.otel import register, Transport, ProjectType
from traceai_pipecat import enable_http_attribute_mapping

# Initialize tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="My Pipecat Voice App",
    transport=Transport.HTTP,
    set_global_tracer_provider=True,
)

# Install attribute mapping
try:
    enable_http_attribute_mapping()
    print("✅ Successfully installed HTTP attribute mapping")
except Exception as e:
    print(f"❌ Error: {e}")

# Your Pipecat application code here...
```

### Complete gRPC Integration Example

```python
from fi_instrumentation.otel import register, Transport, ProjectType
from traceai_pipecat import enable_grpc_attribute_mapping

# Initialize tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="My Pipecat Voice App",
    transport=Transport.GRPC,
    set_global_tracer_provider=True,
)

# Install attribute mapping
try:
    enable_grpc_attribute_mapping()
    print("✅ Successfully installed gRPC attribute mapping")
except Exception as e:
    print(f"❌ Error: {e}")

# Your Pipecat application code here...
```

## API Reference

### Integration Functions

#### `enable_fi_attribute_mapping(transport: Transport = Transport.HTTP) -> bool`
Install attribute mapping by replacing existing span exporters.

**Parameters:**
- `transport`: Transport protocol enum (`Transport.HTTP` or `Transport.GRPC`)

**Returns:**
- `bool`: True if at least one exporter was replaced

#### `enable_http_attribute_mapping() -> bool`
Convenience function for HTTP transport.

#### `enable_grpc_attribute_mapping() -> bool`
Convenience function for gRPC transport.

### Exporter Creation Functions

#### `create_mapped_http_exporter(endpoint: Optional[str] = None, headers: Optional[dict] = None)`
Create a new HTTP exporter with Pipecat attribute mapping.

#### `create_mapped_grpc_exporter(endpoint: Optional[str] = None, headers: Optional[dict] = None)`
Create a new gRPC exporter with Pipecat attribute mapping.

### Exporter Classes

#### `MappedHTTPSpanExporter`
HTTP span exporter that maps Pipecat attributes to Future AGI conventions.

#### `MappedGRPCSpanExporter`
gRPC span exporter that maps Pipecat attributes to Future AGI conventions.

#### `BaseMappedSpanExporter`
Base class for mapped span exporters.


## Troubleshooting

### Common Issues

1. **No exporters found to replace**
   - Ensure you've called `register()` before installing attribute mapping
   - Check that the transport type matches your tracer provider configuration

2. **Import errors for gRPC**
   - Install gRPC dependencies: `pip install "fi-instrumentation[grpc]"`

3. **Data not being sent to FutureAGI**
   - Ensure that your you have set the `FI_API_KEY` and `FI_SECRET_KEY` environment variables
   - Ensure that the `set_global_tracer_provider`  in the `register` function is set to `True`

## Call recordings as trace attachments (opt-in)

Attachments are off by default. When they are enabled in `traceai.init(...)`, the adapter can upload the audio your own `AudioBufferProcessor` already produces. It does not start, stop or reconfigure the recorder. Requires `pipecat-ai==1.15.0` and the attachment-enabled `fi-instrumentation-otel`.

```python
import traceai
from fi_instrumentation.attachments import AttachmentConfig
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from traceai_pipecat.attachments import AudioSessionContext

traceai.init(project_name="voice-agent", attachment_config=AttachmentConfig(enabled=True))

recorder = AudioBufferProcessor(num_channels=2, buffer_size=32 * 1024, auto_start_recording=True)
worker = PipelineWorker(pipeline, enable_tracing=True, enable_turn_tracking=True)
binding = traceai.bind_pipecat_audio_processor(recorder, AudioSessionContext.from_pipeline(worker))
# ... run the worker ...
await binding.finish()  # waits up to 30 s for the final segment and manifest
```

- The recording is attached to the `conversation` span that Pipecat's tracing starts. That span is marked as the conversation root (`fi.span.kind=CONVERSATION`) only when it has no parent.
- Layouts: mono gives one mixed track. Stereo gives user (left, input) and bot (right, output) tracks in one file. `layout="separate_tracks"` uploads each side separately, so hiding inputs drops only the user side. With mono or stereo, any hide flag blocks the whole recording.
- Audio is uploaded as WAV segments of up to 30 s, plus a manifest that records gaps. If uploads fall behind, the overflow becomes a `queue_full` gap rather than growing memory.
- `buffer_size` must be between 1 and 131072 bytes. `buffer_size=0` and `enable_turn_audio=True` are refused (`unbounded_source`).
- `binding.status` reports a short, content-free state such as `recording`, `published`, `partial`, `policy_denied` or `correlation_unavailable`.
- `traceai_pipecat.create_audio_capture(...)` builds a new, unstarted recorder only when both `AttachmentConfig(record_audio=True)` and the server's `record_audio` allow it.
