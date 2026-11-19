"""SageMaker Runtime instrumentation: invoke_endpoint, async and streaming, offline."""
import io
import json

import pytest
from botocore.response import StreamingBody
from botocore.exceptions import ClientError, ParamValidationError
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode

from conftest import ENDPOINT, FakeEventStream, attrs, blob, make_client, ok, part, respond
from fi_instrumentation import TraceConfig

PROVIDER = "gen_ai.provider.name"
MODEL = "gen_ai.request.model"
KIND = "gen_ai.span.kind"
IN_TOK = "gen_ai.usage.input_tokens"
OUT_TOK = "gen_ai.usage.output_tokens"
TOTAL_TOK = "gen_ai.usage.total_tokens"


def one(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0]


def invoke(client, payload, content_type="application/json", **kwargs):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return client.invoke_endpoint(EndpointName=ENDPOINT, Body=body, ContentType=content_type, **kwargs)


# --- invoke_endpoint ---------------------------------------------------------------------------

def test_generic_json_round_trip_records_llm_span(spans):
    client = make_client()
    request = {"inputs": "hello there"}
    response_body = {"answer": "general kenobi"}
    seen = respond(client, "InvokeEndpoint", ok(response_body, InvokedProductionVariant="variant-1"))

    response = invoke(client, request, Accept="application/json")

    # The caller still gets an unread, complete body and the SDK got the unchanged request.
    assert json.loads(response["Body"].read()) == response_body
    assert seen[0]["body"] == json.dumps(request).encode()
    span = one(spans)
    a = attrs(span)
    assert span.name == "sagemaker.invoke_endpoint"
    assert a[KIND] == "LLM"
    assert a[PROVIDER] == "aws.sagemaker"
    assert a[MODEL] == ENDPOINT
    assert a["aws.sagemaker.endpoint_name"] == ENDPOINT
    assert a["aws.sagemaker.invoked_variant"] == "variant-1"
    assert json.loads(a["input.value"]) == request
    assert a["input.mime_type"] == "application/json"
    assert json.loads(a["output.value"]) == response_body
    assert a["output.mime_type"] == "application/json"
    assert span.status.status_code != StatusCode.ERROR


def test_response_body_is_fully_readable_in_every_style(spans):
    client = make_client()
    payload = {"generated_text": "x" * 50}
    respond(client, "InvokeEndpoint", ok(payload))
    response = invoke(client, {"inputs": "a"})
    assert json.load(response["Body"]) == payload


def test_tgi_output_with_token_details(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpoint",
        ok([{"generated_text": "Paris", "details": {"generated_tokens": 3, "prefill": [{}, {}, {}, {}]}}]),
    )
    invoke(client, {"inputs": "Capital of France?", "parameters": {"max_new_tokens": 8, "temperature": 0.2}})
    a = attrs(one(spans))
    assert a["output.value"] == "Paris"
    assert a["output.mime_type"] == "text/plain"
    assert a[OUT_TOK] == 3
    assert a[IN_TOK] == 4
    assert a[TOTAL_TOK] == 7
    assert json.loads(a["gen_ai.request.parameters"]) == {"max_new_tokens": 8, "temperature": 0.2}


def test_openai_compatible_output(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpoint",
        ok(
            {
                "model": "served-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Tokyo"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        ),
    )
    invoke(client, {"messages": [{"role": "user", "content": "Capital of Japan?"}], "max_tokens": 16, "temperature": 0.5})
    a = attrs(one(spans))
    assert a["output.value"] == "Tokyo"
    assert (a[IN_TOK], a[OUT_TOK], a[TOTAL_TOK]) == (11, 7, 18)
    assert a["gen_ai.response.model"] == "served-model"
    assert json.loads(a["gen_ai.request.parameters"]) == {"max_tokens": 16, "temperature": 0.5}
    assert "Capital of Japan?" in a["input.value"]


def test_openai_compatible_text_completion_choice(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"choices": [{"text": "completed"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}))
    invoke(client, {"prompt": "p"})
    a = attrs(one(spans))
    assert a["output.value"] == "completed"
    assert (a[IN_TOK], a[OUT_TOK], a[TOTAL_TOK]) == (1, 2, 3)


def test_bedrock_like_output(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpoint",
        ok({"generation": "hi back", "prompt_token_count": 5, "generation_token_count": 9, "stop_reason": "stop"}),
    )
    invoke(client, {"prompt": "hi"})
    a = attrs(one(spans))
    assert a["output.value"] == "hi back"
    assert (a[IN_TOK], a[OUT_TOK], a[TOTAL_TOK]) == (5, 9, 14)


def test_anthropic_messages_like_output(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpoint",
        ok({"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 4, "output_tokens": 6}}),
    )
    invoke(client, {"messages": []})
    a = attrs(one(spans))
    assert a["output.value"] == "hello"
    assert (a[IN_TOK], a[OUT_TOK]) == (4, 6)


def test_inference_component_and_target_variant(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "ok"}))
    invoke(client, {"inputs": "x"}, InferenceComponentName="ic-llama", TargetVariant="blue")
    a = attrs(one(spans))
    assert a[MODEL] == ENDPOINT
    assert a["aws.sagemaker.inference_component"] == "ic-llama"
    assert a["aws.sagemaker.target_variant"] == "blue"


def test_text_content_type_is_captured_as_text(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok(b"label,score\nA,1\n", content_type="text/csv; charset=utf-8"))
    invoke(client, "a,b\n1,2\n", content_type="text/csv")
    a = attrs(one(spans))
    assert a["input.value"] == "a,b\n1,2\n"
    assert a["input.mime_type"] == "text/plain"
    assert a["output.value"] == "label,score\nA,1\n"


def test_binary_content_is_noted_without_the_bytes(spans):
    client = make_client()
    secret_in = b"\x89PNG\r\n\x1a\nSECRETPIXELS"
    secret_out = b"\x00\x01OUTBYTES\xff" * 3
    respond(client, "InvokeEndpoint", ok(secret_out, content_type="application/x-npy"))
    response = client.invoke_endpoint(EndpointName=ENDPOINT, Body=secret_in, ContentType="image/png")
    assert response["Body"].read() == secret_out
    span = one(spans)
    a = attrs(span)
    text = blob(span)
    assert "SECRETPIXELS" not in text and "OUTBYTES" not in text
    assert "binary" in a["input.value"] and "image/png" in a["input.value"] and str(len(secret_in)) in a["input.value"]
    assert "binary" in a["output.value"] and "application/x-npy" in a["output.value"]


def test_missing_request_content_type_is_treated_as_binary(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "ok"}))
    client.invoke_endpoint(EndpointName=ENDPOINT, Body=b'{"inputs": "private"}')
    span = one(spans)
    assert "private" not in attrs(span)["input.value"]
    assert "binary" in attrs(span)["input.value"]


def test_file_like_request_body_is_not_consumed(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "ok"}))
    body = io.BytesIO(b'{"inputs": "from a file"}')
    client.invoke_endpoint(EndpointName=ENDPOINT, Body=body, ContentType="application/json")
    assert body.tell() == 0
    assert "from a file" not in blob(one(spans))


def test_invalid_json_with_json_content_type_never_fails_the_call(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok(b"{not json", content_type="application/json"))
    response = invoke(client, "{also not json")
    assert response["Body"].read() == b"{not json"
    a = attrs(one(spans))
    assert a["input.value"] == "{also not json"
    assert a["output.value"] == "{not json"
    assert IN_TOK not in a and OUT_TOK not in a


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [1, 2, 3],
        [None],
        {"usage": "many"},
        {"usage": {"prompt_tokens": "7", "completion_tokens": True}},
        {"choices": "nope"},
        {"choices": [None, 3]},
        {"choices": [{"message": None}]},
        {"details": {"generated_tokens": -4, "prefill": "x"}},
        "just a string",
        42,
        None,
    ],
)
def test_odd_response_shapes_never_fail_the_call(spans, payload):
    client = make_client()
    respond(client, "InvokeEndpoint", ok(payload))
    response = invoke(client, {"inputs": "x"})
    assert json.loads(response["Body"].read()) == payload
    span = one(spans)
    assert span.status.status_code != StatusCode.ERROR
    a = attrs(span)
    for token_attr in (IN_TOK, OUT_TOK, TOTAL_TOK):
        assert token_attr not in a or (isinstance(a[token_attr], int) and not isinstance(a[token_attr], bool) and a[token_attr] >= 0)


def test_client_error_is_reraised_unchanged_with_error_status(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpoint",
        {"Error": {"Code": "ModelError", "Message": "boom-secret-detail"}, "ResponseMetadata": {"HTTPStatusCode": 424}},
        status=424,
    )
    with pytest.raises(ClientError) as raised:
        invoke(client, {"inputs": "x"})
    assert raised.value.response["Error"]["Code"] == "ModelError"
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    # botocore raises a ClientError subclass named after the AWS error code.
    assert attrs(span)["error.type"] == "ModelError"
    assert "boom-secret-detail" not in blob(span)
    assert span.end_time is not None


def test_parameter_validation_error_passes_through(spans):
    client = make_client()
    with pytest.raises(ParamValidationError):
        client.invoke_endpoint(Body=b"x")  # EndpointName is required
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ParamValidationError"


@pytest.mark.parametrize("config, hidden, visible", [
    (TraceConfig(hide_inputs=True), "TOPSECRET-IN", "TOPSECRET-OUT"),
    (TraceConfig(hide_outputs=True), "TOPSECRET-OUT", "TOPSECRET-IN"),
    (TraceConfig(hide_inputs=True, hide_outputs=True), "TOPSECRET", None),
])
def test_hidden_content_is_not_exported(instrument, config, hidden, visible):
    spans = instrument(config=config)
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "TOPSECRET-OUT", "details": {"generated_tokens": 2}}))
    invoke(client, {"inputs": "TOPSECRET-IN"})
    span = one(spans)
    text = blob(span)
    assert hidden not in text
    if visible:
        assert visible in text
    # Metadata survives hiding.
    assert attrs(span)[MODEL] == ENDPOINT
    assert attrs(span)[PROVIDER] == "aws.sagemaker"


def test_hidden_output_text_keeps_input(instrument):
    spans = instrument(config=TraceConfig(hide_output_text=True))
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "TOPSECRET-OUT"}))
    invoke(client, {"inputs": "visible-in"})
    text = blob(one(spans))
    assert "TOPSECRET-OUT" not in text
    assert "visible-in" in text


def test_suppressed_instrumentation_creates_no_span(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generated_text": "ok"}))
    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        response = invoke(client, {"inputs": "x"})
    finally:
        context_api.detach(token)
    assert json.loads(response["Body"].read()) == {"generated_text": "ok"}
    assert spans.get_finished_spans() == ()


# --- invoke_endpoint_async ---------------------------------------------------------------------

def test_async_invoke_records_request_span_only(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpointAsync",
        {
            "InferenceId": "inf-123",
            "OutputLocation": "s3://bucket/out/inf-123.out",
            "FailureLocation": "s3://bucket/fail/inf-123.out",
            "ResponseMetadata": {"HTTPStatusCode": 202},
        },
        status=202,
    )
    response = client.invoke_endpoint_async(
        EndpointName=ENDPOINT,
        InputLocation="s3://bucket/in/payload-with-private-name.json",
        ContentType="application/json",
        InferenceId="inf-123",
    )
    assert response["InferenceId"] == "inf-123"
    span = one(spans)
    a = attrs(span)
    assert span.name == "sagemaker.invoke_endpoint_async"
    assert a[KIND] == "LLM"
    assert a[PROVIDER] == "aws.sagemaker"
    assert a[MODEL] == ENDPOINT
    assert a["aws.sagemaker.inference_id"] == "inf-123"
    assert "output.value" not in a
    assert "input.value" not in a
    assert "payload-with-private-name" not in blob(span)


def test_async_invoke_client_error(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpointAsync",
        {"Error": {"Code": "ValidationError", "Message": "no"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        status=400,
    )
    with pytest.raises(ClientError):
        client.invoke_endpoint_async(EndpointName=ENDPOINT, InputLocation="s3://b/k")
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ValidationError"


# --- invoke_endpoint_with_response_stream ------------------------------------------------------

def stream_client(events, error=None, content_type="application/jsonlines", **extra):
    client = make_client()
    stream = FakeEventStream(events, error=error)
    respond(
        client,
        "InvokeEndpointWithResponseStream",
        ok(stream, content_type=content_type, InvokedProductionVariant="v1", **extra),
    )
    return client, stream


def start_stream(client, **kwargs):
    return client.invoke_endpoint_with_response_stream(
        EndpointName=ENDPOINT, Body=json.dumps({"inputs": "stream me", "stream": True}), ContentType="application/json", **kwargs
    )


def test_tgi_stream_is_assembled_across_split_chunks(spans):
    lines = [
        {"token": {"id": 1, "text": "Hel"}, "generated_text": None, "details": None},
        {"token": {"id": 2, "text": "lo"}, "generated_text": None, "details": None},
        {"token": {"id": 3, "text": "!"}, "generated_text": "Hello!", "details": {"generated_tokens": 3, "finish_reason": "eos"}},
    ]
    wire = "".join(json.dumps(line) + "\n" for line in lines).encode()
    # Cut mid-JSON and mid-line so reassembly across PayloadParts is required.
    cuts = [0, 17, 60, 61, len(wire) - 5, len(wire)]
    events = [part(wire[a:b]) for a, b in zip(cuts, cuts[1:])]
    client, stream = stream_client(events)

    response = start_stream(client)
    assert spans.get_finished_spans() == ()  # not ended until the stream is consumed
    received = b"".join(event["PayloadPart"]["Bytes"] for event in response["Body"])

    assert received == wire
    span = one(spans)
    a = attrs(span)
    assert span.name == "sagemaker.invoke_endpoint_with_response_stream"
    assert a[KIND] == "LLM" and a[PROVIDER] == "aws.sagemaker" and a[MODEL] == ENDPOINT
    assert a["output.value"] == "Hello!"
    assert a[OUT_TOK] == 3
    assert "stream me" in a["input.value"]
    assert a["aws.sagemaker.invoked_variant"] == "v1"
    assert span.status.status_code != StatusCode.ERROR


def test_tgi_stream_without_final_generated_text_joins_tokens(spans):
    lines = [{"token": {"text": "a"}}, {"token": {"text": "b"}}, {"token": {"text": "c"}}]
    wire = "\n".join(json.dumps(line) for line in lines)
    client, _ = stream_client([part(wire)])
    list(start_stream(client)["Body"])
    assert attrs(one(spans))["output.value"] == "abc"


def test_openai_sse_stream_with_usage(spans):
    chunks = [
        {"model": "m1", "choices": [{"delta": {"role": "assistant", "content": "Tok"}}]},
        {"choices": [{"delta": {"content": "yo"}}]},
        {"choices": [], "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}},
    ]
    wire = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    client, _ = stream_client([part(wire[:40]), part(wire[40:])], content_type="text/event-stream")
    for _event in start_stream(client)["Body"]:
        pass
    a = attrs(one(spans))
    assert a["output.value"] == "Tokyo"
    assert (a[IN_TOK], a[OUT_TOK], a[TOTAL_TOK]) == (6, 2, 8)
    assert a["gen_ai.response.model"] == "m1"


def test_plain_text_stream(spans):
    client, _ = stream_client([part("one "), part("two")], content_type="text/plain")
    for _event in start_stream(client)["Body"]:
        pass
    assert attrs(one(spans))["output.value"] == "one two"


def test_binary_stream_records_no_bytes(spans):
    client, _ = stream_client([part(b"\x00\x01SECRETBYTES"), part(b"\xff\xfe")], content_type="application/octet-stream")
    for _event in start_stream(client)["Body"]:
        pass
    span = one(spans)
    assert "SECRETBYTES" not in blob(span)
    assert "binary" in attrs(span)["output.value"]


def test_stream_close_ends_span_as_incomplete(spans):
    client, stream = stream_client([part('{"token": {"text": "a"}}\n'), part('{"token": {"text": "b"}}\n')])
    response = start_stream(client)
    next(iter(response["Body"]))
    assert spans.get_finished_spans() == ()
    response["Body"].close()
    assert stream.close_calls == 1
    span = one(spans)
    assert attrs(span)["aws.sagemaker.stream.incomplete"] is True
    response["Body"].close()  # idempotent
    assert len(spans.get_finished_spans()) == 1


def test_stream_context_manager_ends_span(spans):
    client, _ = stream_client([part('{"token": {"text": "a"}}\n')])
    response = start_stream(client)
    with response["Body"] as body:
        for _event in body:
            pass
    assert len(spans.get_finished_spans()) == 1


def test_stream_error_midway_marks_error_and_reraises(spans):
    boom = RuntimeError("upstream stream failure SECRETDETAIL")
    client, _ = stream_client([part('{"token": {"text": "a"}}\n')], error=boom)
    response = start_stream(client)
    with pytest.raises(RuntimeError) as raised:
        for _event in response["Body"]:
            pass
    assert raised.value is boom
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "RuntimeError"
    assert "SECRETDETAIL" not in blob(span)


def test_stream_open_client_error(spans):
    client = make_client()
    respond(
        client,
        "InvokeEndpointWithResponseStream",
        {"Error": {"Code": "ValidationError", "Message": "no"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        status=400,
    )
    with pytest.raises(ClientError):
        start_stream(client)
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["error.type"] == "ValidationError"


def test_stream_hidden_output(instrument):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    client, _ = stream_client([part('{"token": {"text": "TOPSECRET-OUT"}, "details": {"generated_tokens": 1}}\n')])
    for _event in start_stream(client)["Body"]:
        pass
    span = one(spans)
    assert "TOPSECRET-OUT" not in blob(span)
    assert attrs(span)[OUT_TOK] == 1


def test_stream_garbage_never_breaks_consumption(spans):
    events = [part(b"\xff\xfe{{{"), part("null\n"), part("[1,2]\n"), part('{"token": 5}\n'), {"unexpected": {}}]
    client, _ = stream_client(events)
    received = list(start_stream(client)["Body"])
    assert len(received) == len(events)
    assert one(spans).status.status_code != StatusCode.ERROR


def test_stream_output_is_bounded(spans):
    big = "x" * 100_000
    client, _ = stream_client([part(json.dumps({"token": {"text": big}}) + "\n")])
    for _event in start_stream(client)["Body"]:
        pass
    assert len(attrs(one(spans))["output.value"]) <= 32768


# --- lifecycle ---------------------------------------------------------------------------------

def test_only_sagemaker_runtime_clients_are_wrapped(spans):
    runtime = make_client("sagemaker-runtime")
    control = make_client("sagemaker")
    s3 = make_client("s3")
    other = [make_client("bedrock-runtime"), make_client("bedrock-agent-runtime")]
    assert getattr(runtime.invoke_endpoint, "__wrapped__", None) is not None or hasattr(runtime, "_unwrapped_invoke_endpoint")
    for client in (control, s3, *other):
        assert not hasattr(client, "_unwrapped_invoke_endpoint")
        assert not any(name.startswith("_unwrapped_") for name in vars(client))


def test_uninstrument_restores_client_creation_and_passes_existing_clients_through(provider):
    from traceai_sagemaker import SageMakerInstrumentor

    instrumentor = SageMakerInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    existing = make_client()
    respond(existing, "InvokeEndpoint", ok({"generated_text": "ok"}))
    instrumentor.uninstrument()

    fresh = make_client()
    assert not hasattr(fresh, "_unwrapped_invoke_endpoint")
    invoke(existing, {"inputs": "x"})
    assert provider.exporter.get_finished_spans() == ()

    # It can be instrumented again.
    instrumentor.instrument(tracer_provider=provider)
    again = make_client()
    respond(again, "InvokeEndpoint", ok({"generated_text": "ok"}))
    invoke(again, {"inputs": "x"})
    instrumentor.uninstrument()
    assert len(provider.exporter.get_finished_spans()) == 1


def test_package_metadata():
    import traceai_sagemaker

    assert traceai_sagemaker.SageMakerInstrumentor
    assert traceai_sagemaker.__version__


class _SslLikeError(Exception):
    """Stands in for urllib3 SSLError/DecodeError: an HTTPError, neither OSError nor BotoCoreError."""


class _FailingBody(StreamingBody):
    def __init__(self, error):
        super().__init__(io.BytesIO(b"{}"), 2)
        self.error = error

    def read(self, *args, **kwargs):
        raise self.error


@pytest.mark.parametrize("error", [_SslLikeError("tls dropped mid-read"), ValueError("bad decode")])
def test_non_io_body_read_error_surfaces_to_the_caller(spans, error):
    client = make_client()
    respond(client, "InvokeEndpoint", ok(_FailingBody(error)))
    with pytest.raises(type(error)) as raised:
        invoke(client, {"inputs": "x"})
    assert raised.value is error
    span = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert span.end_time is not None


def _error_with_context():
    try:
        raise OSError("connection reset")
    except OSError:
        try:
            raise _SslLikeError("tls dropped mid-read")
        except _SslLikeError as error:
            return error


def test_body_read_error_keeps_its_own_exception_context(spans):
    error = _error_with_context()
    prior = error.__context__
    client = make_client()
    respond(client, "InvokeEndpoint", ok(_FailingBody(error)))
    with pytest.raises(_SslLikeError) as raised:
        invoke(client, {"inputs": "x"})
    assert raised.value is error
    assert raised.value.__context__ is prior
    assert isinstance(prior, OSError)


def test_zero_generation_token_count_is_recorded(spans):
    client = make_client()
    respond(client, "InvokeEndpoint", ok({"generation": "", "prompt_token_count": 5, "generation_token_count": 0}))
    invoke(client, {"prompt": "hi"})
    a = attrs(one(spans))
    assert a[OUT_TOK] == 0
