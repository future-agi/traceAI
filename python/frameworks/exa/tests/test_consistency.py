"""Family consistency: span kinds and keys, class-local wrapping, isolation, PII in error text."""

from __future__ import annotations

import types

import pytest
from opentelemetry.sdk.trace import TracerProvider

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import ECHO_QUERY, EXA_KEY, FakeExa, attrs, instrumented  # noqa: E402
from exa_py import Exa  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

from traceai_exa import ExaInstrumentor  # noqa: E402

KIND = "gen_ai.span.kind"


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def _client(fake: FakeExa) -> Exa:
    return Exa(api_key=EXA_KEY, base_url=fake.origin)


def test_search_and_answer_are_retrievers_and_get_contents_is_a_tool(fake):
    with instrumented() as traced:
        client = _client(fake)
        client.search("q")
        client.answer("q")
        client.get_contents(["https://example.com/a"])

    kinds = {span.name: attrs(span)[KIND] for span in traced.spans()}
    assert kinds == {"exa.search": "RETRIEVER", "exa.answer": "RETRIEVER", "exa.get_contents": "TOOL"}
    assert all("fi.span.kind" not in attrs(span) for span in traced.spans())


def test_search_query_uses_the_repo_retrieval_key(fake):
    with instrumented() as traced:
        _client(fake).search("open telemetry")

    values = attrs(traced.one())
    assert values["gen_ai.retrieval.query"] == "open telemetry"
    assert values["input.value"] == "open telemetry"
    assert values["exa.result_count"] == 2
    assert not [key for key in values if key.startswith("fi.retrieval.")]


def test_a_method_only_inherited_by_a_class_is_not_wrapped_on_it():
    class Parent:
        def search(self, query):
            return query

    class Child(Parent):
        pass

    module = types.ModuleType("fake_exa_api")
    module.Parent, module.Child = Parent, Child
    instrumentor = ExaInstrumentor()
    instrumentor._original_methods = {}
    instrumentor._wrap_method(module, "Child", "search", lambda w, i, a, k: w(*a, **k))

    assert "search" not in vars(Child)
    assert instrumentor._original_methods == {}


class _BrokenTracerProvider(TracerProvider):
    def get_tracer(self, *args, **kwargs):
        tracer = super().get_tracer(*args, **kwargs)

        def start_span(*_args, **_kwargs):
            raise RuntimeError("tracer is broken")

        tracer.start_span = start_span
        return tracer


def test_a_failing_start_span_never_reaches_the_caller(fake):
    instrumentor = ExaInstrumentor()
    instrumentor.instrument(tracer_provider=_BrokenTracerProvider())
    try:
        client = _client(fake)
        result = client.search("q")
        assert len(result.results) == 2
        assert [chunk for chunk in client.stream_answer("q")]
    finally:
        instrumentor.uninstrument()


def test_pii_redaction_also_covers_error_text(fake):
    query = "{0} mail jane.doe@example.com".format(ECHO_QUERY)
    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        with pytest.raises(ValueError):
            _client(fake).search(query)

    span = traced.one()
    (event,) = [event for event in span.events if event.name == "exception"]
    assert "jane.doe@example.com" not in span.status.description
    assert "jane.doe@example.com" not in event.attributes["exception.message"]
    assert "jane.doe@example.com" not in event.attributes["exception.stacktrace"]
    assert "<EMAIL_ADDRESS>" in event.attributes["exception.message"]
