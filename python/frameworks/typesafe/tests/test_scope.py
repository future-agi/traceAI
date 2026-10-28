"""Scope: models.list is not traced (AC-09); spans nest under the caller's span; uninstrument restores."""

from __future__ import annotations

import pytest

pytest.importorskip("typesafe_sdk")

import typesafe_sdk  # noqa: E402
from fi_instrumentation import using_session  # noqa: E402
from opentelemetry import trace as trace_api  # noqa: E402

from tests._support import (  # noqa: E402
    STATE,
    FakeSystemOne,
    client,
    instrumented,
    only_span,
    questions,
    tracer_provider,
)


def test_models_list_produces_no_span():
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            models = typesafe.models.list()
    assert models.models[0].name == "jev-latest"
    assert fake.requests[0].url.path == "/v1/models"
    assert exporter.get_finished_spans() == ()


def test_span_is_a_child_of_the_callers_span_and_carries_the_session():
    from traceai_typesafe import TypeSafeInstrumentor

    provider, exporter = tracer_provider()
    TypeSafeInstrumentor().instrument(tracer_provider=provider)
    try:
        tracer = provider.get_tracer("app")
        with using_session("session-th8322"):
            with tracer.start_as_current_span("triage") as parent:
                with client(FakeSystemOne()) as typesafe:
                    typesafe.system_one(STATE, questions())
                    current = trace_api.get_current_span()
    finally:
        TypeSafeInstrumentor().uninstrument()
    child, root = exporter.get_finished_spans()
    assert child.parent.span_id == parent.get_span_context().span_id
    assert child.attributes["session.id"] == "session-th8322"
    assert current is parent  # the decision span was detached afterwards


def test_uninstrument_restores_the_methods():
    from traceai_typesafe import TypeSafeInstrumentor

    sync_original = typesafe_sdk.TypeSafeClient.__dict__["system_one"]
    async_original = typesafe_sdk.AsyncTypeSafeClient.__dict__["system_one"]
    instrumentor = TypeSafeInstrumentor()
    provider, exporter = tracer_provider()
    instrumentor.instrument(tracer_provider=provider)
    assert typesafe_sdk.TypeSafeClient.__dict__["system_one"] is not sync_original
    instrumentor.uninstrument()
    assert typesafe_sdk.TypeSafeClient.__dict__["system_one"] is sync_original
    assert typesafe_sdk.AsyncTypeSafeClient.__dict__["system_one"] is async_original
    with client(FakeSystemOne()) as typesafe:
        typesafe.system_one(STATE, questions())
    assert exporter.get_finished_spans() == ()


def test_suppressed_instrumentation_makes_no_span():
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value

    with instrumented() as exporter:
        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            with client(FakeSystemOne()) as typesafe:
                typesafe.system_one(STATE, questions())
        finally:
            detach(token)
    assert exporter.get_finished_spans() == ()


def test_a_broken_response_object_never_breaks_the_call(monkeypatch):
    import traceai_typesafe._wrappers as wrappers

    def boom(*args, **kwargs):
        raise RuntimeError("extractor bug")

    monkeypatch.setattr(wrappers, "_response_attributes", boom)
    monkeypatch.setattr(wrappers, "_request_attributes", boom)
    with instrumented() as exporter:
        with client(FakeSystemOne()) as typesafe:
            response = typesafe.system_one(STATE, questions())
    assert response.answers
    assert only_span(exporter).status.is_ok
