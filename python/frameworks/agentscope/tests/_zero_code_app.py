"""Real app for the process-start privacy regression; no instrumentation setup."""

import argparse
import asyncio
import json
from collections import UserDict
from importlib.metadata import version


def text(response):
    return "\n".join(
        block.get("text", "") if isinstance(block, dict) else block.text
        for block in response.content
    )


async def main(args):
    from agentscope import model

    v2 = version("agentscope").startswith("2.")
    if v2:
        from agentscope.credential import OpenAICredential
        from agentscope.message import Msg, TextBlock

        instance = model.OpenAIChatModel(
            credential=OpenAICredential(
                api_key="unused-static-credential-20261030", base_url=args.endpoint
            ),
            model="local-test",
            stream=args.stream,
            max_retries=0,
            client_kwargs={"max_retries": 0},
        )
        inputs = [Msg(name="user", role="user", content=[TextBlock(text="question")])]
    else:
        instance = model.OpenAIChatModel(
            model_name="local-test",
            api_key="unused-static-credential-20261030",
            stream=args.stream,
            client_kwargs={"base_url": args.endpoint, "max_retries": 0},
        )
        inputs = [{"role": "user", "content": "question"}]

    calls = 0
    kwargs = {}
    if args.source == "dynamic":
        async def key_provider():
            nonlocal calls
            calls += 1
            return args.credential

        instance.client = instance.client.with_options(api_key=key_provider)
    else:
        kwargs["extra_headers"] = UserDict(
            {"Authorization": "Bearer " + args.credential}
        )

    result = await instance(inputs, **kwargs)
    outputs = [text(chunk) async for chunk in result] if args.stream else [text(result)]
    # A second, unrelated native application trace must be scrubbed too, without
    # inheriting any framework session. This exercises the real OTLP export route.
    from opentelemetry import trace

    with trace.get_tracer("unrelated-application").start_as_current_span("later-echo") as span:
        span.set_attribute("output.value", "later: " + args.credential)
        span.add_event("echo", {"echo.value": args.credential})

    print("TRACEAI_RESULT " + json.dumps({"outputs": outputs, "provider_calls": calls}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credential", required=True)
    parser.add_argument("--source", choices=["dynamic", "userdict"], required=True)
    parser.add_argument("--stream", action="store_true")
    asyncio.run(main(parser.parse_args()))
