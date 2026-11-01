"""Scope: only v1 search, search_lite and answer_query (sync and async) are traced.

AC-05: ``create_conversation`` and the other admin RPCs emit no span.
AC-09: instrumenting Discovery Engine does not import or instrument
``google.genai`` or ``traceai_google_genai``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from google.api_core import exceptions as core_exceptions

from _discoveryengine_support import (
    CONVERSATION_PARENT,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    instrumented,
    search_request,
)

WRAPPED = {
    ("discoveryengine_v1", "SearchServiceClient", "search"),
    ("discoveryengine_v1", "SearchServiceClient", "search_lite"),
    ("discoveryengine_v1", "SearchServiceAsyncClient", "search"),
    ("discoveryengine_v1", "SearchServiceAsyncClient", "search_lite"),
    ("discoveryengine_v1", "ConversationalSearchServiceClient", "answer_query"),
    ("discoveryengine_v1", "ConversationalSearchServiceAsyncClient", "answer_query"),
}


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def _client_methods():
    """Every function defined on every client class of every API version."""
    import importlib

    methods = {}
    for version in ("discoveryengine_v1", "discoveryengine_v1alpha", "discoveryengine_v1beta"):
        module = importlib.import_module("google.cloud." + version)
        for name in module.__all__:
            cls = getattr(module, name)
            if not (isinstance(cls, type) and name.endswith("Client")):
                continue
            for attribute, value in vars(cls).items():
                if callable(value):
                    methods[(version, name, attribute)] = value
    return methods


def test_only_the_six_v1_methods_are_wrapped():
    before = _client_methods()
    with instrumented():
        during = _client_methods()
    after = _client_methods()

    assert len(before) > 500
    changed = {key for key in before if during[key] is not before[key]}
    assert changed == WRAPPED
    assert all(after[key] is before[key] for key in before)


def test_the_unversioned_alias_is_v1beta_and_is_not_traced(fake):
    # google.cloud.discoveryengine re-exports the v1beta clients at 0.20.5.
    # The package targets v1 only (PRD section 3), so a client imported from
    # the alias produces no span. The fake serves v1 only, so the call fails,
    # and even that failure is not traced.
    import grpc
    from google.cloud import discoveryengine
    from google.cloud.discoveryengine_v1beta.services.search_service.transports.grpc import (
        SearchServiceGrpcTransport,
    )

    assert discoveryengine.SearchServiceClient.__module__.startswith(
        "google.cloud.discoveryengine_v1beta."
    )
    client = discoveryengine.SearchServiceClient(
        transport=SearchServiceGrpcTransport(channel=grpc.insecure_channel(fake.target))
    )
    with instrumented() as traced:
        with pytest.raises(core_exceptions.MethodNotImplemented):
            client.search(request=search_request())

    assert traced.spans() == []


def test_create_conversation_emits_no_span(fake):
    with instrumented() as traced:
        conversation = answer_client(fake).create_conversation(
            parent=CONVERSATION_PARENT, conversation={}
        )

    assert conversation.name == CONVERSATION_PARENT + "/conversations/conversation-1"
    assert fake.methods() == ["CreateConversation"]
    assert traced.spans() == []


def test_stream_answer_query_is_not_traced(fake):
    # The architecture lists search, search_lite and answer_query only.
    with instrumented() as traced:
        responses = list(answer_client(fake).stream_answer_query(request=answer_request()))

    assert [response.answer.state.name for response in responses] == ["STREAMING", "SUCCEEDED"]
    assert fake.methods() == ["StreamAnswerQuery"]
    assert traced.spans() == []


def test_instrumenting_does_not_import_or_instrument_google_genai():
    # AC-09, in a fresh interpreter so no other test's imports count.
    code = textwrap.dedent(
        """
        import sys
        from opentelemetry.sdk.trace import TracerProvider
        from traceai_discoveryengine import DiscoveryEngineInstrumentor

        instrumentor = DiscoveryEngineInstrumentor()
        instrumentor.instrument(tracer_provider=TracerProvider())
        loaded = sorted(
            name for name in sys.modules
            if name == "google.genai" or name.startswith(("google.genai.", "traceai_google_genai"))
        )
        instrumentor.uninstrument()
        print("loaded=" + ",".join(loaded))
        """
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "loaded="
