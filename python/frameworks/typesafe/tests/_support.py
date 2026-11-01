"""Shared support: the real typesafe-sdk clients over an in-process fake of the API.

``FakeSystemOne`` answers ``POST /v1/systemone`` and ``GET /v1/models`` in the
shape of TypeSafe's published OpenAPI document
(https://api.typesafe.ai/openapi.json, read 2026-10-16) through
``httpx2.MockTransport``. The values are a shape example, not recorded model
output. Nothing leaves the process and every key is a placeholder.

Models with special behaviour:

* ``fail-401``: HTTP 401 with the body api.typesafe.ai returns for a bad key.
* ``fail-422``: HTTP 422 with a FastAPI validation body that echoes input.
* ``fail-429``: HTTP 429 with ``retry-after-ms: 0``.
* ``flaky-429``: HTTP 429 once, then a normal answer.
* ``echo-key``: HTTP 400 whose message echoes the bearer key.
"""

from __future__ import annotations

import contextlib
import inspect
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx2
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

API_KEY = "ts-placeholder-key-MUST-NOT-BE-EXPORTED"
BASE_URL = "https://api.typesafe.ai"
REQUEST_MODEL = "jev-latest"
RESOLVED_MODEL = "jev-1.13.0"
INPUT_TOKENS = 120
OUTPUT_TOKENS = 12

# Request content. Captured by default, dropped by the TraceConfig hide flags.
STATE_MARKER = "STATE-th8322-marker"
INSTRUCTION_MARKER = "INSTRUCTION-th8322-marker"
CRITERIA_MARKER = "CRITERIA-th8322-marker"
STATE = "Customer {0} was charged twice and is angry.".format(STATE_MARKER)

# Numbers the fake returns; float32/float64-exact so they compare exactly.
NOUL = 0.9375
CHOICE_CONFIDENCE = 0.875
SCORE = 1.75
SCORE_CONFIDENCE = 0.8125
BOX = {"top": 0.25, "bottom": 0.625, "left": 0.125, "right": 0.75}
# Probability-map values that must never reach a span.
PROBABILITY_MARKERS = (0.0546875, 0.0703125)


def has_box() -> bool:
    """Box questions exist from typesafe-sdk 0.7.5."""
    import typesafe_sdk

    return hasattr(typesafe_sdk, "Box")


def questions() -> Dict[str, Any]:
    """One question of each type the installed SDK has, as SDK objects."""
    import typesafe_sdk
    from typesafe_sdk import Choice, Noul, Score

    asked = {
        "billing": Noul(instructions="Is this about billing? {0}".format(INSTRUCTION_MARKER)),
        "tone": Choice(
            instructions="What is the tone?",
            criteria={"angry": "Upset {0}".format(CRITERIA_MARKER), "calm": None},
        ),
        "urgency": Score(
            instructions="How urgent?",
            criteria=["Can wait", "This week", "Today {0}".format(CRITERIA_MARKER)],
        ),
    }
    if has_box():
        asked["logo"] = typesafe_sdk.Box(instructions="Where is the logo?")
    return asked


def _answer(question: Dict[str, Any]) -> Dict[str, Any]:
    kind = question.get("type")
    if kind == "noul":
        return {"type": "noul", "noul": NOUL}
    if kind == "choice":
        names = list(question["criteria"])
        rest = [PROBABILITY_MARKERS[0]] * (len(names) - 1)
        return {
            "type": "choice",
            "choice": names[0],
            "confidence": CHOICE_CONFIDENCE,
            "probabilities": dict(zip(names, [CHOICE_CONFIDENCE] + rest)),
        }
    if kind == "score":
        levels = [str(i) for i in range(len(question["criteria"]))]
        return {
            "type": "score",
            "score": SCORE,
            "confidence": SCORE_CONFIDENCE,
            "legend": dict(zip(levels, question["criteria"])),
            "probabilities": {level: PROBABILITY_MARKERS[1] for level in levels},
        }
    if kind == "box":
        return {"type": "box", "box": dict(BOX), "confidence": None}
    return {"type": kind}


def request_body(request: httpx2.Request) -> Dict[str, Any]:
    """The JSON body, or the ``request`` part of a multipart body with image assets."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return json.loads(request.content)
    boundary = content_type.split("boundary=", 1)[1].strip('"').encode()
    for part in request.content.split(b"--" + boundary):
        head, _, data = part.partition(b"\r\n\r\n")
        if b'name="request"' in head:
            return json.loads(data.rstrip(b"\r\n"))
    raise ValueError("no request part")


class FakeSystemOne:
    """Handler for ``httpx2.MockTransport``; records every request it sees."""

    def __init__(self) -> None:
        self.requests: List[httpx2.Request] = []
        self._flaky_seen = False

    def _json(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> httpx2.Response:
        merged = {"x-typesafe-request-id": "req_fake_{0}".format(len(self.requests))}
        merged.update(headers or {})
        return httpx2.Response(status, json=body, headers=merged)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/v1/models"):
            return self._json(
                200,
                {"models": [{"name": "jev-latest", "description": "General-purpose system one model.", "release_date": "2026-09-15"}]},
            )
        if request.method != "POST" or not path.endswith("/v1/systemone"):
            return self._json(404, {"detail": "Not Found"})
        body = request_body(request)
        model = body.get("model")
        if model == "fail-401":
            return self._json(401, {"detail": {"error_type": "authentication_error", "message": "Cannot authenticate with the server. Please check your API key and try again."}})
        if model == "fail-422":
            return self._json(422, {"detail": [{"loc": ["body", "state"], "msg": "Value error, bad state {0}".format(body.get("state")), "type": "value_error", "input": body.get("state")}]})
        if model == "fail-429":
            return self._json(429, {"detail": {"error_type": "rate_limit_error", "message": "Too many requests."}}, {"retry-after-ms": "0"})
        if model == "flaky-429" and not self._flaky_seen:
            self._flaky_seen = True
            return self._json(429, {"detail": {"error_type": "rate_limit_error", "message": "Too many requests."}}, {"retry-after-ms": "0"})
        if model == "echo-key":
            return self._json(400, {"detail": {"error_type": "invalid_request_error", "message": "bad key {0}".format(request.headers.get("authorization"))}})
        answers = {name: _answer(question) for name, question in body["questions"].items()}
        resolved = RESOLVED_MODEL if model == REQUEST_MODEL else model
        return self._json(
            200,
            {"model": resolved, "answers": answers, "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS}},
        )


def tracer_provider() -> Tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@contextlib.contextmanager
def instrumented(config: Any = None) -> Iterator[InMemorySpanExporter]:
    from traceai_typesafe import TypeSafeInstrumentor

    provider, exporter = tracer_provider()
    instrumentor = TypeSafeInstrumentor()
    kwargs: Dict[str, Any] = {"tracer_provider": provider}
    if config is not None:
        kwargs["config"] = config
    instrumentor.instrument(**kwargs)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()


def plain(cls: Any, **kwargs: Any) -> Dict[str, Any]:
    """Client options for uncompressed bodies (``compression=`` exists from typesafe-sdk 0.7.5)."""
    if "compression" in inspect.signature(cls.__init__).parameters:
        kwargs.setdefault("compression", False)
    return kwargs


def client(fake: FakeSystemOne, **kwargs: Any) -> Any:
    from typesafe_sdk import TypeSafeClient

    kwargs.setdefault("api_key", API_KEY)
    return TypeSafeClient(transport=httpx2.MockTransport(fake), **plain(TypeSafeClient, **kwargs))


def async_client(fake: FakeSystemOne, **kwargs: Any) -> Any:
    from typesafe_sdk import AsyncTypeSafeClient

    kwargs.setdefault("api_key", API_KEY)
    return AsyncTypeSafeClient(transport=httpx2.MockTransport(fake), **plain(AsyncTypeSafeClient, **kwargs))


def no_retry() -> Any:
    from typesafe_sdk import RetryPolicy

    return RetryPolicy(max_retries=0)


def everything(span: ReadableSpan) -> str:
    """Every exported field of a span as one string, for leak checks."""
    return json.dumps(
        {
            "name": span.name,
            "attributes": dict(span.attributes or {}),
            "status": [str(span.status.status_code), span.status.description],
            "events": [[event.name, dict(event.attributes or {})] for event in span.events],
            "resource": dict(span.resource.attributes),
            "links": [dict(link.attributes or {}) for link in span.links],
        },
        default=str,
    )


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [span.name for span in spans]
    return spans[0]
