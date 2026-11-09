"""Independent credential-URL checks with real SDKs and synthetic transports."""

import asyncio
import copy
import json

import httpx
import openai
import pytest

from .test_responses_safety import (  # noqa: F401 - fixture dependencies
    openai_tracing,
    response_body,
    sdk_client,
    tracing,
)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("outcome", ["response", "error"])
@pytest.mark.parametrize("extra", [False, True])
def test_mcp_url_credentials_are_telemetry_only(openai_tracing, async_, outcome, extra):
    _, exporter = openai_tracing
    secrets = ["synthetic-url-user", "synthetic-url-password", "synthetic-query-token", "synthetic-fragment-token"]
    url = f"https://{secrets[0]}:{secrets[1]}@synthetic.invalid/mcp?access_token={secrets[2]}#{secrets[3]}"
    tool = {"type": "mcp", "server_label": "synthetic", "server_url": url,
            "authorization": "synthetic-mcp-authorization",
            "headers": {"X-Custom-Authentication": "synthetic-mcp-header"}}
    original = copy.deepcopy(tool)

    def handler(request):
        assert json.loads(request.content)["tools"] == [original]
        if outcome == "error":
            return httpx.Response(400, json={"error": {"message": "MCP rejected " + url}})
        body = response_body()
        body["tools"] = [original]
        return httpx.Response(200, json=body)

    params = {"model": "parallel", "input": "safe question"}
    params.update({"extra_body": {"tools": [tool]}} if extra else {"tools": [tool]})

    def check(response):
        assert response.tools[0].server_url == url
        assert response.tools[0].authorization == original["authorization"]

    async def run():
        async with sdk_client(handler, True) as client:
            if outcome == "error":
                with pytest.raises(openai.BadRequestError) as caught:
                    await client.responses.create(**params)
                assert url in caught.value.body["message"]
            else:
                check(await client.responses.create(**params))

    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            if outcome == "error":
                with pytest.raises(openai.BadRequestError) as caught:
                    client.responses.create(**params)
                assert url in caught.value.body["message"]
            else:
                check(client.responses.create(**params))
    assert tool == original
    span, = exporter.get_finished_spans()
    for secret in [*secrets, tool["authorization"], *tool["headers"].values()]:
        assert secret not in span.to_json()
