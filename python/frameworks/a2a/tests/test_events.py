import json

import httpx
import pytest

from .test_framework_a2a import CURRENT, SECRET, client, request
from .test_framework_a2a import tracing as tracing  # noqa: PLC0414


@pytest.mark.asyncio
async def test_stream_events_keep_task_id_and_artifact_type_without_content(tracing):
    _, exporter = tracing

    async def handle(req):
        status = {
            "taskId": "event-task",
            "contextId": "context-id",
            "status": {"state": "TASK_STATE_COMPLETED" if CURRENT else "completed"},
        }
        artifact = {
            "taskId": "event-task",
            "contextId": "context-id",
            "artifact": {"artifactId": "artifact-id", "parts": [{"text": SECRET}]},
        }
        if CURRENT:
            results = [{"artifactUpdate": artifact}, {"statusUpdate": status}]
        else:
            status.update(kind="status-update", final=True)
            artifact["kind"] = "artifact-update"
            artifact["artifact"]["parts"][0]["kind"] = "text"
            results = [artifact, status]
        events = [
            {"jsonrpc": "2.0", "id": json.loads(req.content)["id"], "result": value}
            for value in results
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async for _ in client(http).send_message_streaming(request(True)):
            pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.a2a.task.id"] == "event-task"
    assert span.attributes["gen_ai.a2a.task.state"] == "completed"
    assert span.attributes["gen_ai.a2a.artifact.type"] == "text"
    assert SECRET not in span.to_json()


@pytest.mark.asyncio
async def test_async_generator_protocol_is_preserved(tracing):
    _, exporter = tracing
    from .test_framework_a2a import response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: response(req, streaming=True))
    ) as http:
        stream = client(http).send_message_streaming(request(True))
        await stream.asend(None)
        with pytest.raises(RuntimeError, match=SECRET):
            await stream.athrow(RuntimeError(SECRET))
        await stream.aclose()
    (span,) = exporter.get_finished_spans()
    assert span.attributes["error.type"] == "RuntimeError"
    assert SECRET not in span.to_json()
