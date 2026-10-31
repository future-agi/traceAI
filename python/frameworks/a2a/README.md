# traceAI-a2a

Trace A2A requests across agents in Future AGI using the A2A SDK's native OpenTelemetry spans.

## Install

Use Python 3.10 or later. This example uses the A2A SDK 1.3 API.

```bash
pip install "traceAI-a2a[a2a]" "a2a-sdk==1.3.0"
```

The adapter supports the older `A2AClient` API in SDK 0.2.x and the transport-based clients in SDK 0.3.x and 1.x. Keep your existing `A2AInstrumentor().instrument(tracer_provider=provider)` setup; you do not need a second client instrumentor.

## Configure tracing

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment, then configure tracing before creating clients:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_a2a import A2AInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agent-workflows",
)
A2AInstrumentor().instrument(tracer_provider=provider)
```

The adapter routes A2A's native tracers to this provider without replacing the global OpenTelemetry provider. It enriches the native client operation rather than creating a second client span. Private transport-helper spans are suppressed within that operation.

## Send a message

Point `A2A_AGENT_URL` at your agent's base URL. The SDK discovers its agent card and selects a supported transport.

```python
import asyncio
import os
from uuid import uuid4

import httpx
from a2a.client import ClientConfig, create_client
from a2a.types import SendMessageRequest


async def main():
    async with httpx.AsyncClient() as http:
        client = await create_client(
            os.environ["A2A_AGENT_URL"],
            client_config=ClientConfig(httpx_client=http, streaming=False),
        )
        try:
            request = SendMessageRequest(
                message={
                    "message_id": str(uuid4()),
                    "role": 1,  # ROLE_USER
                    "parts": [{"text": "Summarize the latest release notes."}],
                }
            )
            async for event in client.send_message(request):
                print(event)
        finally:
            await client.close()


try:
    asyncio.run(main())
finally:
    provider.force_flush()
```

In Future AGI, open **Observe**, select `agent-workflows`, and open the latest trace. The native `a2a-python-sdk` client span includes the operation, message role and part count, endpoint origin, streaming flag, and task ID/state when the response contains them. A2A does not itself perform a model call, so these spans do not include token usage or model costs.

## Receive requests

The receiving service must extract incoming trace context. If your ASGI application does not already have OpenTelemetry HTTP server middleware, wrap the A2A application:

```python
from traceai_a2a import A2ATracingMiddleware

# a2a_app is your existing A2A ASGI application.
app = A2ATracingMiddleware(a2a_app, tracer_provider=provider)
```

An existing outer OpenTelemetry `SERVER` span is reused. Configure your model/framework instrumentation separately to see the receiving agent's internal work beneath it. For gRPC servers, use an OpenTelemetry gRPC server interceptor to extract the propagated metadata; the ASGI middleware only handles HTTP.

## Close streams when stopping early

Use `ClientConfig(streaming=True)` for an agent that supports streaming. A client span stays open until the stream is exhausted or explicitly closed. Use `aclosing` if you may stop early:

```python
from contextlib import aclosing

async with aclosing(client.send_message(request)) as events:
    async for event in events:
        print(event)
        break
```

The adapter also supports direct transport `send_message_streaming()` calls. Span context is active while the SDK advances the stream, not while your application handles a yielded event.

## Privacy and troubleshooting

- Message bodies, artifacts, authentication headers, URL credentials/paths/queries, and exception messages/stack traces are not captured by this adapter. Task IDs and endpoint origins are recorded. Other instrumentors and application spans have their own privacy settings.
- Preserve `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=true` when using the adapter. If native tracing is disabled, installation raises an error rather than reporting success without instrumenting clients.
- Configure tracing once at startup. To restore the original SDK methods, call `A2AInstrumentor().uninstrument()` after active requests and streams finish.
- A bare `break` does not close an async iterator. Use `aclosing` to finish the span deterministically.

See the [A2A Python SDK](https://github.com/a2aproject/a2a-python) and the [complete example](examples/multi_agent_demo.py).
