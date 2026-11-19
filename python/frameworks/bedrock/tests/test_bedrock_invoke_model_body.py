"""T4-PY-CLOUD-005: request capture must not reject or consume SDK bodies."""

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.response import StreamingBody

from fi_instrumentation.fi_types import SpanAttributes
from traceai_bedrock import _model_invocation_wrapper


MODEL_ID = "meta.llama3-8b-instruct-v1:0"
REQUEST_BODY = b'{"prompt":"hello"}'
RESPONSE_BODY = b'{"generation":"hello back"}'


@pytest.fixture
def invocation():
    """Exercise the real invocation wrapper without a client, exporter, or network."""
    tracer = MagicMock()
    sdk = Mock(
        return_value={
            "body": StreamingBody(io.BytesIO(RESPONSE_BODY), len(RESPONSE_BODY)),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
    )
    client = SimpleNamespace(invoke_model=sdk, _unwrapped_invoke_model=sdk)
    return SimpleNamespace(
        invoke=_model_invocation_wrapper(tracer)(client),
        sdk=sdk,
        span=tracer.start_as_current_span.return_value.__enter__.return_value,
    )


@pytest.mark.parametrize("position", [0, 5], ids=["start", "nonzero"])
def test_invoke_model_preserves_file_like_body(invocation, position):
    with io.BytesIO(REQUEST_BODY) as body:
        body.seek(position)
        with (
            patch.object(body, "read", wraps=body.read) as read,
            patch.object(body, "seek", wraps=body.seek) as seek,
        ):
            def sdk_invoke(**kwargs):
                assert kwargs["body"] is body
                read.assert_not_called()
                seek.assert_not_called()
                assert body.tell() == position
                assert body.read() == REQUEST_BODY[position:]
                return invocation.sdk.return_value

            invocation.sdk.side_effect = sdk_invoke
            response = invocation.invoke(
                modelId=MODEL_ID, contentType="application/json", body=body
            )

            invocation.sdk.assert_called_once_with(
                modelId=MODEL_ID, contentType="application/json", body=body
            )
            read.assert_called_once_with()
            seek.assert_not_called()
            assert not body.closed

    assert response is invocation.sdk.return_value
    assert response["body"].read() == RESPONSE_BODY


@pytest.mark.parametrize(
    "body",
    [REQUEST_BODY.decode("utf-8"), REQUEST_BODY, bytearray(REQUEST_BODY)],
    ids=["string", "bytes", "bytearray"],
)
def test_invoke_model_still_captures_inline_json(invocation, body):
    response = invocation.invoke(modelId=MODEL_ID, body=body)

    invocation.sdk.assert_called_once_with(modelId=MODEL_ID, body=body)
    invocation.span.set_attribute.assert_any_call(SpanAttributes.INPUT_VALUE, "hello")
    assert response["body"].read() == RESPONSE_BODY


@pytest.mark.parametrize(
    "body_factory",
    [
        lambda: io.BytesIO(REQUEST_BODY),
        lambda: b"not-json",
        lambda: b"\xff",
        lambda: b"[]",
        lambda: b"null",
        lambda: None,
    ],
    ids=["file-like", "invalid-json", "invalid-encoding", "array", "null", "none"],
)
def test_invoke_model_preserves_sdk_error_when_capture_is_unavailable(
    invocation, body_factory
):
    body = body_factory()
    sdk_error = RuntimeError("fake SDK rejection")
    invocation.sdk.side_effect = sdk_error

    try:
        with pytest.raises(RuntimeError) as exc_info:
            invocation.invoke(modelId=MODEL_ID, body=body)

        assert exc_info.value is sdk_error
        invocation.sdk.assert_called_once_with(modelId=MODEL_ID, body=body)
    finally:
        if isinstance(body, io.BytesIO):
            body.close()
