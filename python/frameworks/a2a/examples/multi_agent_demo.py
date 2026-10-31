"""Call an existing A2A agent using SDK 1.3 and native tracing.

Set FI_API_KEY, FI_SECRET_KEY and A2A_AGENT_URL before running this example.
The receiving agent must also extract W3C trace context to join the trace.
"""

import asyncio
import os
from uuid import uuid4

import httpx
from a2a.client import ClientConfig, create_client
from a2a.types import SendMessageRequest
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_a2a import A2AInstrumentor


async def main() -> None:
    provider = register(
        project_type=ProjectType.OBSERVE, project_name="agent-workflows"
    )
    instrumentor = A2AInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        async with httpx.AsyncClient() as http:
            client = await create_client(
                os.environ["A2A_AGENT_URL"],
                client_config=ClientConfig(httpx_client=http, streaming=False),
            )
            try:
                request = SendMessageRequest(
                    message={
                        "message_id": str(uuid4()),
                        "role": 1,
                        "parts": [{"text": "Summarize the latest release notes."}],
                    }
                )
                async for event in client.send_message(request):
                    print(event)
            finally:
                await client.close()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
