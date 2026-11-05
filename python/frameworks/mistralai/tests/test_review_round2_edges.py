"""Privacy and decoding boundaries with real SDKs and synthetic transports."""

import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode

from test_review_regressions import CHAT, Mistral, NATIVE, httpx, tracing
from test_sdk import SSE, chat_response


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("resource", ["chat", "agents"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize(
    "flag",
    [
        "hide_input_text",
        "hide_input_messages",
        "hide_output_text",
        "hide_output_messages",
        "visible",
    ],
)
def test_chat_privacy_all_paths(
    asynchronous, resource, streaming, environment, flag, monkeypatch
):
    flags = {}
    if flag != "visible":
        if environment:
            monkeypatch.setenv("FI_" + flag.upper(), "true")
        else:
            flags[flag] = True
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        if streaming:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=SSE()
            )
        return httpx.Response(200, json=chat_response())

    kwargs = {"messages": [{"role": "user", "content": "private-input-marker"}]}
    kwargs.update(
        {"model": "mistral-test"}
        if resource == "chat"
        else {"agent_id": "synthetic-agent"}
    )
    method = "stream" if streaming else "complete"
    with tracing(**flags) as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic", async_client=transport)
                    result = await getattr(
                        getattr(client, resource), method + "_async"
                    )(**kwargs)
                    if streaming:
                        chunks = [chunk async for chunk in result]
                        await result.close()
                        assert (
                            chunks[0].data.choices[0].delta.content
                            == "private-output-marker"
                        )
                    else:
                        assert (
                            result.choices[0].message.content == "private-output-marker"
                        )

            asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic", client=transport)
                result = getattr(getattr(client, resource), method)(**kwargs)
                if streaming:
                    chunks = list(result)
                    result.close()
                    assert (
                        chunks[0].data.choices[0].delta.content
                        == "private-output-marker"
                    )
                else:
                    assert result.choices[0].message.content == "private-output-marker"
        assert observed[0]["messages"] == kwargs["messages"]
        (span,) = exporter.get_finished_spans()
        assert len(starts.spans) == 1
        assert span.status.status_code == StatusCode.OK
        assert span.attributes["gen_ai.usage.input_tokens"] == 2
        rendered = span.to_json()
        assert ("private-input-marker" in rendered) == ("input" not in flag)
        assert ("private-output-marker" in rendered) == ("output" not in flag)


@pytest.mark.skipif(not NATIVE, reason="Native SDK response observers")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "operation", ["chat", "agents", "embedding", "fim", "start", "append", "restart"]
)
@pytest.mark.parametrize("body", ["schema", "json", "content-type"])
@pytest.mark.parametrize("hide", [False, True])
def test_native_decoding_boundaries(asynchronous, operation, body, hide):
    def respond(request):
        if body == "schema":
            return httpx.Response(
                200, json={"error": {"message": "synthetic-private-error"}}
            )
        return httpx.Response(
            200,
            content=b"synthetic-private-error",
            headers={
                "content-type": "application/json" if body == "json" else "text/html",
            },
        )

    def invoke(client):
        kwargs = {"model": "mistral-test"}
        method = "complete"
        if operation in ("chat", "agents"):
            resource = getattr(client, operation)
            kwargs["messages"] = [{"role": "user", "content": "hello"}]
            if operation == "agents":
                kwargs.pop("model")
                kwargs["agent_id"] = "synthetic-agent"
        elif operation == "embedding":
            resource, method = client.embeddings, "create"
            kwargs["inputs"] = ["hello"]
        elif operation == "fim":
            resource = client.fim
            kwargs["prompt"] = "hello"
        else:
            resource, method = client.beta.conversations, operation
            kwargs["inputs"] = "hello"
            if operation != "start":
                kwargs.pop("model")
                kwargs["conversation_id"] = "conv-synthetic"
            if operation == "restart":
                kwargs["from_entry_id"] = "entry-synthetic"
        return getattr(resource, method + ("_async" if asynchronous else ""))(**kwargs)

    with tracing(hide_error_details=hide) as (exporter, starts):
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    with pytest.raises(Exception) as error:
                        await invoke(
                            Mistral(api_key="synthetic", async_client=transport)
                        )
                    return error.value

            error = asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                with pytest.raises(Exception) as caught:
                    invoke(Mistral(api_key="synthetic", client=transport))
                error = caught.value
        assert "synthetic-private-error" in error.body
        (span,) = exporter.get_finished_spans()
        assert len(starts.spans) == 1
        assert not starts.spans[0].is_recording()
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) == 1
        assert (
            span.events[0].attributes["exception.type"].endswith(type(error).__name__)
        )
        if hide:
            assert "synthetic-private-error" not in span.to_json()
        else:
            assert span.events[0].attributes["exception.message"] == str(error)


@pytest.mark.skipif(not NATIVE, reason="Native SDK response observers")
@pytest.mark.parametrize("asynchronous", [False, True])
def test_concurrent_decoding_scopes(asynchronous):
    def respond(request):
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json=CHAT if model == "valid" else {"invalid": True})

    with tracing() as (exporter, starts):
        if asynchronous:

            async def run():
                # Yield during transport to interleave requests in one client.
                async def async_respond(request):
                    await asyncio.sleep(0)
                    return respond(request)

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(async_respond)
                ) as transport:
                    client = Mistral(api_key="synthetic", async_client=transport)
                    return await asyncio.gather(
                        *[
                            client.chat.complete_async(
                                model=model,
                                messages=[{"role": "user", "content": "hi"}],
                            )
                            for model in ["valid", "invalid"] * 4
                        ],
                        return_exceptions=True,
                    )

            results = asyncio.run(run())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic", client=transport)

                def call(model):
                    try:
                        return client.chat.complete(
                            model=model, messages=[{"role": "user", "content": "hi"}]
                        )
                    except Exception as error:
                        return error

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(call, ["valid", "invalid"] * 4))
        assert sum(isinstance(result, Exception) for result in results) == 4
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 8
        assert sum(span.status.status_code == StatusCode.ERROR for span in spans) == 4
        assert sum(span.status.status_code == StatusCode.OK for span in spans) == 4
        assert all(not span.is_recording() for span in starts.spans)


@pytest.mark.skipif(not NATIVE, reason="Native SDK response observers")
def test_observed_methods_restored():
    methods = []
    for module, cls_name, names in (
        ("chat", "Chat", ("complete", "stream")),
        ("agents", "Agents", ("complete", "stream")),
        ("embeddings", "Embeddings", ("create",)),
        ("fim", "Fim", ("complete", "stream")),
        (
            "conversations",
            "Conversations",
            (
                "start",
                "append",
                "restart",
                "start_stream",
                "append_stream",
                "restart_stream",
            ),
        ),
    ):
        cls = getattr(importlib.import_module("mistralai.client." + module), cls_name)
        for name in names:
            for suffix in ("", "_async"):
                name_with_suffix = name + suffix
                methods.append((cls, name_with_suffix, getattr(cls, name_with_suffix)))
    for _ in range(2):
        with tracing():
            for cls, name, original in methods:
                assert getattr(cls, name).__wrapped__ is original
        for cls, name, original in methods:
            assert getattr(cls, name) is original


@pytest.mark.parametrize(
    "key",
    [
        "choice.count",
        "encoding_formats",
        "frequency_penalty",
        "max_tokens",
        "presence_penalty",
        "seed",
        "stop_sequences",
        "temperature",
        "top_p",
        "top_k",
        "future_parameter",
    ],
)
def test_native_parameter_policy_drops_lazy_values(key):
    from traceai_mistralai._privacy import _NativeTraceConfig

    config = _NativeTraceConfig(TraceConfig(hide_llm_invocation_parameters=True))

    def sensitive():
        pytest.fail("Hidden parameter value should not be evaluated")

    assert config.mask("gen_ai.request." + key, sensitive) is None
    assert config.mask("gen_ai.request.model", "mistral-test") == "mistral-test"
    assert config.mask("gen_ai.usage.input_tokens", 2) == 2


@pytest.mark.skipif(not NATIVE, reason="Native SDK response observers")
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide", [False, True])
def test_decoder_exception_identity_and_scope_reset(asynchronous, hide, monkeypatch):
    module = importlib.import_module("mistralai.client.chat")
    original = module.unmarshal_json_response
    failure = (
        asyncio.CancelledError("synthetic-decoder-cancel")
        if asynchronous
        else RuntimeError("synthetic-decoder-error")
    )
    calls = []

    def decode(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "unmarshal_json_response", decode)
    with tracing(hide_error_details=hide) as (exporter, starts):
        kwargs = {
            "model": "mistral-test",
            "messages": [{"role": "user", "content": "hi"}],
        }
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda r: httpx.Response(200, json=CHAT)
                    )
                ) as transport:
                    client = Mistral(api_key="synthetic", async_client=transport)
                    with pytest.raises(asyncio.CancelledError) as error:
                        await client.chat.complete_async(**kwargs)
                    assert error.value is failure
                    assert (await client.chat.complete_async(**kwargs)).choices[
                        0
                    ].message.content == "OK"

            asyncio.run(run())
        else:
            with httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CHAT))
            ) as transport:
                client = Mistral(api_key="synthetic", client=transport)
                with pytest.raises(RuntimeError) as error:
                    client.chat.complete(**kwargs)
                assert error.value is failure
                assert client.chat.complete(**kwargs).choices[0].message.content == "OK"
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 2
        assert spans[0].status.status_code == StatusCode.ERROR
        assert spans[1].status.status_code == StatusCode.OK
        assert len(spans[0].events) == 1
        assert all(not span.is_recording() for span in starts.spans)
        assert ("synthetic-decoder-" in spans[0].to_json()) == (not hide)
