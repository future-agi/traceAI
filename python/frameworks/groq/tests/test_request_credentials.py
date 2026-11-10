"""T4-PY-CLOUD-001: request credentials must never become span attributes.

The wrapped SDK methods are fakes with explicit create() signatures. The real
Groq wrappers, extractors, finalizer and OpenTelemetry in-memory exporter run;
no HTTP client or provider service is used. Requires the package dependencies.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor
from traceai_groq._wrappers import _AsyncCompletionsWrapper, _CompletionsWrapper


FAKE_CONSTRUCTOR = "FAKE_CONSTRUCTOR_CREDENTIAL_NOT_REAL"
FAKE_AUTHORIZATION = "Bearer FAKE_PROVIDER_CREDENTIAL_NOT_REAL"
FAKE_HEADER = "FAKE_HEADER_CREDENTIAL_NOT_REAL"
FAKE_QUERY = "FAKE_QUERY_CREDENTIAL_NOT_REAL"
FAKE_BODY = "FAKE_BODY_CREDENTIAL_NOT_REAL"
FAKE_OPAQUE = "FAKE_OPAQUE_CREDENTIAL_NOT_REAL"
FAKE_VALUES = (
    FAKE_CONSTRUCTOR,
    FAKE_AUTHORIZATION,
    FAKE_HEADER,
    FAKE_QUERY,
    FAKE_BODY,
    FAKE_OPAQUE,
)


class _OpaqueConfiguration:
    def __init__(self):
        self.string_calls = 0

    def __str__(self):
        self.string_calls += 1
        return FAKE_OPAQUE

    __repr__ = __str__


class _FakeCompletions:
    def __init__(self, mode):
        self.client = SimpleNamespace(api_key=FAKE_CONSTRUCTOR)
        self.mode = mode
        self.calls = []
        self.error = RuntimeError("FAKE_COMPLETION_FAILURE")
        self.response = {
            "model": "fake-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
        }
        self.chunks = [
            {
                "model": "fake-model",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello"}}],
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    def create(
        self, *, model, messages, temperature, top_p, max_completion_tokens,
        stop, stream=False, extra_headers=None, extra_query=None, extra_body=None,
        timeout=None,
    ):
        self.calls.append({
            "model": model, "messages": messages, "temperature": temperature,
            "top_p": top_p, "max_completion_tokens": max_completion_tokens,
            "stop": stop, "stream": stream, "extra_headers": extra_headers,
            "extra_query": extra_query, "extra_body": extra_body, "timeout": timeout,
        })
        if self.mode == "error":
            raise self.error
        if stream:
            return self._stream()
        return self.response

    def _stream(self):
        yield self.chunks[0]
        if self.mode == "stream_error":
            raise self.error
        yield self.chunks[1]


class _FakeAsyncCompletions(_FakeCompletions):
    async def create(
        self, *, model, messages, temperature, top_p, max_completion_tokens,
        stop, stream=False, extra_headers=None, extra_query=None, extra_body=None,
        timeout=None,
    ):
        response = super().create(
            model=model, messages=messages, temperature=temperature, top_p=top_p,
            max_completion_tokens=max_completion_tokens, stop=stop, stream=stream,
            extra_headers=extra_headers, extra_query=extra_query, extra_body=extra_body,
            timeout=timeout,
        )
        if stream:
            async def chunks():
                for chunk in response:
                    yield chunk
            return chunks()
        return response


def _request(stream=False):
    # No api_key argument on create(): it belongs to client construction only.
    return {
        "model": "fake-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.25,
        "top_p": 0.9,
        "max_completion_tokens": 64,
        "stop": ["END"],
        "stream": stream,
        "extra_headers": {"Authorization": FAKE_AUTHORIZATION, "X-Api-Key": FAKE_HEADER},
        "extra_query": {"api_key": FAKE_QUERY},
        "extra_body": {"config": {"authentication": {"token": FAKE_BODY}}},
        "timeout": 3.0,
    }


def _expected_parameters(stream=False):
    return {
        "model": "fake-model", "temperature": 0.25, "top_p": 0.9,
        "max_completion_tokens": 64, "stop": ["END"], "stream": stream,
    }


class RequestCredentialTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("groq-request-credential-regression")
        self.addCleanup(self.provider.shutdown)

    def assert_no_credentials(self, value):
        encoded = json.dumps(value, default=str)
        for credential in FAKE_VALUES:
            self.assertNotIn(credential, encoded)

    def assert_exported_request(self, request, error=False):
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsNotNone(span.end_time)
        attributes = dict(span.attributes)
        self.assert_no_credentials(attributes)
        parameters = json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS])
        self.assertEqual(parameters, _expected_parameters(request["stream"]))
        self.assertEqual(json.loads(attributes[SpanAttributes.INPUT_VALUE]), request["messages"])
        self.assertEqual(attributes[SpanAttributes.GEN_AI_REQUEST_MODEL], "fake-model")
        self.assertEqual(span.status.status_code, StatusCode.ERROR if error else StatusCode.OK)
        for event in span.events:
            self.assert_no_credentials(dict(event.attributes))
        if not error:
            self.assertEqual(attributes[SpanAttributes.OUTPUT_VALUE], "hello")

    def assert_forwarded_unchanged(self, sdk, request):
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(sdk.calls[0], request)
        for key in ("messages", "extra_headers", "extra_query", "extra_body", "stop"):
            self.assertIs(sdk.calls[0][key], request[key])
        self.assertEqual(sdk.client.api_key, FAKE_CONSTRUCTOR)
        self.assertEqual(request["extra_headers"]["Authorization"], FAKE_AUTHORIZATION)
        self.assertEqual(request["extra_headers"]["X-Api-Key"], FAKE_HEADER)
        self.assertEqual(request["extra_body"]["config"]["authentication"]["token"], FAKE_BODY)

    def test_sync_finished_spans_never_export_transport_credentials(self):
        for mode in ("normal", "error", "stream", "stream_error"):
            with self.subTest(mode=mode):
                self.exporter.clear()
                request = _request(stream=mode.startswith("stream"))
                sdk = _FakeCompletions(mode)
                wrapper = _CompletionsWrapper(tracer=self.tracer)
                if "error" in mode:
                    with self.assertRaises(RuntimeError) as raised:
                        result = wrapper(sdk.create, sdk, (), request)
                        if request["stream"]:
                            list(result)
                    self.assertIs(raised.exception, sdk.error)
                else:
                    result = wrapper(sdk.create, sdk, (), request)
                    if request["stream"]:
                        self.assertEqual(list(result), sdk.chunks)
                    else:
                        self.assertIs(result, sdk.response)
                self.assert_forwarded_unchanged(sdk, request)
                self.assert_exported_request(request, error="error" in mode)

    def test_async_finished_spans_never_export_transport_credentials(self):
        async def exercise(mode):
            request = _request(stream=mode.startswith("stream"))
            sdk = _FakeAsyncCompletions(mode)
            wrapper = _AsyncCompletionsWrapper(tracer=self.tracer)
            if "error" in mode:
                with self.assertRaises(RuntimeError) as raised:
                    result = await wrapper(sdk.create, sdk, (), request)
                    if request["stream"]:
                        [chunk async for chunk in result]
                self.assertIs(raised.exception, sdk.error)
            else:
                result = await wrapper(sdk.create, sdk, (), request)
                if request["stream"]:
                    self.assertEqual([chunk async for chunk in result], sdk.chunks)
                else:
                    self.assertIs(result, sdk.response)
            self.assert_forwarded_unchanged(sdk, request)
            self.assert_exported_request(request, error="error" in mode)

        for mode in ("normal", "error", "stream", "stream_error"):
            with self.subTest(mode=mode):
                self.exporter.clear()
                asyncio.run(exercise(mode))

    def test_every_input_yield_is_safe_without_relying_on_overwrite(self):
        extractor = _RequestAttributesExtractor()
        for message_mode in ("normal", "empty", "none", "absent"):
            with self.subTest(messages=message_mode):
                request = _request()
                if message_mode == "empty":
                    request["messages"] = []
                elif message_mode == "none":
                    request["messages"] = None
                elif message_mode == "absent":
                    del request["messages"]
                attributes = list(extractor.get_attributes_from_request(request))
                # Check every emitted pair, not only a dict that drops earlier values.
                self.assert_no_credentials(attributes)
                final_input = json.loads(dict(attributes)[SpanAttributes.INPUT_VALUE])
                expected = request.get("messages")
                if expected is None:
                    expected = _expected_parameters()
                self.assertEqual(final_input, expected)

    def test_only_builtin_inference_values_are_serialized(self):
        extractor = _RequestAttributesExtractor()
        opaque = _OpaqueConfiguration()

        class CustomString(str):
            pass

        request = _request()
        del request["messages"]
        request.update({
            "model": opaque, "temperature": {"api_key": FAKE_BODY},
            "stop": ["END", {"token": FAKE_BODY}],
            "reasoning_effort": CustomString(FAKE_OPAQUE),
            "response_format": {"config": {"token": FAKE_BODY}},
            "client": opaque, "provider_options": {"client": opaque},
            "extra_headers": {"Authorization": opaque},
        })
        attributes = list(extractor.get_attributes_from_request(request))
        attributes.extend(extractor.get_extra_attributes_from_request(request))
        self.assert_no_credentials(attributes)
        self.assertEqual(opaque.string_calls, 0)
        final = dict(attributes)
        expected = {"top_p": 0.9, "max_completion_tokens": 64, "stream": False}
        self.assertEqual(json.loads(final[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]), expected)
        self.assertEqual(json.loads(final[SpanAttributes.INPUT_VALUE]), expected)
        self.assertNotIn(SpanAttributes.GEN_AI_REQUEST_MODEL, final)

    def test_opaque_transport_configuration_is_not_stringified_by_argument_binding(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.exporter.clear()
                opaque = _OpaqueConfiguration()
                request = _request()
                request["extra_body"]["client"] = opaque
                if asynchronous:
                    sdk = _FakeAsyncCompletions("normal")
                    wrapper = _AsyncCompletionsWrapper(tracer=self.tracer)
                    result = asyncio.run(wrapper(sdk.create, sdk, (), request))
                else:
                    sdk = _FakeCompletions("normal")
                    wrapper = _CompletionsWrapper(tracer=self.tracer)
                    result = wrapper(sdk.create, sdk, (), request)
                self.assertIs(result, sdk.response)
                self.assertEqual(opaque.string_calls, 0)
                self.assert_forwarded_unchanged(sdk, request)
                self.assert_exported_request(request)

    def test_tool_definitions_remain_separate_from_invocation_parameters(self):
        request = _request()
        tool = {"type": "function", "function": {"name": "fake_tool", "parameters": {"type": "object"}}}
        request["tools"] = [tool]
        attributes = dict(_RequestAttributesExtractor().get_extra_attributes_from_request(request))
        self.assert_no_credentials(attributes)
        self.assertEqual(
            json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]),
            _expected_parameters(),
        )
        schemas = [value for key, value in attributes.items() if key.startswith(SpanAttributes.GEN_AI_TOOL_DEFINITIONS)]
        self.assertEqual([json.loads(value) for value in schemas], [tool])


if __name__ == "__main__":
    unittest.main()
