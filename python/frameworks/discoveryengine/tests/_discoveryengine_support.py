"""Shared test support: a loopback Discovery Engine gRPC fake and an in-memory tracer.

``FakeDiscoveryEngine`` is a real gRPC server on 127.0.0.1 that implements
the four v1 RPCs the tests need (``SearchService/Search``,
``SearchService/SearchLite``, ``ConversationalSearchService/AnswerQuery``
and ``ConversationalSearchService/CreateConversation``) plus
``StreamAnswerQuery``. The real ``google-cloud-discoveryengine`` clients talk
to it through their own gRPC transports, so request serialization, metadata,
retries, paging and ``google.api_core`` error mapping are the library's own.

It listens twice: an insecure port (no credentials, as with a transport built
on ``grpc.insecure_channel``) and a local-TCP port that accepts
``grpc.local_channel_credentials()``, so a client built with real
``google.auth`` credentials (an OAuth access token or an API key) sends them
as gRPC metadata exactly as it would to Google. Nothing leaves 127.0.0.1; no
ADC, no GCP project and no live endpoint are used, and every token is a
placeholder.

The query text (``SearchRequest.query`` or ``AnswerQueryRequest.query.text``)
selects the behaviour:

- ``fail-denied``: PERMISSION_DENIED whose message echoes the query and the
  ``authorization`` / ``x-goog-api-key`` metadata the client sent
- ``fail-huge``: INVALID_ARGUMENT whose ~24 KB message echoes the credential
- ``fail-unseen``: INVALID_ARGUMENT quoting token-shaped strings the client
  never held (a ``ya29.`` access token, an ``AIza`` key, a bearer value)
- ``fail-unavailable``: UNAVAILABLE on every call
- ``unavailable-once``: UNAVAILABLE on the first call, then success
- ``pages``: a first page with ``next_page_token`` and a second page
- ``slow``: held until the fake closes (for cancellation)
- ``answer-failed``: an answer whose state is FAILED
- ``answer-missing``: a response without an ``answer``
"""

from __future__ import annotations

import contextlib
import threading
from concurrent import futures
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import grpc
from google.cloud.discoveryengine_v1.types import (
    Answer,
    AnswerQueryRequest,
    AnswerQueryResponse,
    Conversation,
    CreateConversationRequest,
    SearchRequest,
    SearchResponse,
    Session,
)
from google.protobuf import struct_pb2
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SEARCH_SERVICE = "google.cloud.discoveryengine.v1.SearchService"
CONVERSATION_SERVICE = "google.cloud.discoveryengine.v1.ConversationalSearchService"

SERVING_CONFIG = (
    "projects/test-project/locations/global/collections/default_collection/"
    "engines/test-engine/servingConfigs/default_search"
)
SESSION_NAME = (
    "projects/test-project/locations/global/collections/default_collection/"
    "engines/test-engine/sessions/session-abc"
)
NEW_SESSION = SESSION_NAME.rsplit("/", 1)[0] + "/-"
CONVERSATION_PARENT = (
    "projects/test-project/locations/global/collections/default_collection/"
    "dataStores/test-store"
)

# Placeholder credentials. The fake echoes them into error messages; none may
# reach a span. They deliberately do not look like Google tokens, so only the
# package's lookup of where the client keeps them can redact them.
ACCESS_TOKEN = "placeholder-access-token-must-not-be-exported"
API_KEY = "placeholder-api-key-must-not-be-exported"
METADATA_TOKEN = "placeholder-metadata-token-must-not-be-exported"
# Token-shaped strings the package never saw (an access token minted by a
# refresh, an API key quoted by the server): caught by their shape.
UNSEEN_ACCESS_TOKEN = "ya29.a0AfB_unseenTokenValue-123"
UNSEEN_API_KEY = "AIza" + "S" * 35

FAIL_DENIED = "fail-denied"
FAIL_HUGE = "fail-huge"
HUGE_ERROR_CHARS = 8000
FAIL_UNSEEN = "fail-unseen"
BEARER_VALUE = "opaque-bearer-value-must-not-be-exported"
FAIL_UNAVAILABLE = "fail-unavailable"
UNAVAILABLE_ONCE = "unavailable-once"
PAGES = "pages"
SLOW = "slow"
ANSWER_FAILED = "answer-failed"
ANSWER_MISSING = "answer-missing"

SEARCH_RESULTS = 2
SECOND_PAGE_RESULTS = 1
ANSWER_REFERENCES = 2

# Response content the fake returns. None of it may reach a span.
DOCUMENT_TITLE = "DOCUMENT-TITLE-MUST-NOT-BE-EXPORTED"
SNIPPET = "SNIPPET-TEXT-MUST-NOT-BE-EXPORTED"
SUMMARY = "SUMMARY-TEXT-MUST-NOT-BE-EXPORTED"
ANSWER_TEXT = "ANSWER-TEXT-MUST-NOT-BE-EXPORTED"
CHUNK_CONTENT = "CHUNK-CONTENT-MUST-NOT-BE-EXPORTED"
CONTENT_MARKERS = (DOCUMENT_TITLE, SNIPPET, SUMMARY, ANSWER_TEXT, CHUNK_CONTENT)


def _struct(**values: Any) -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    struct.update(values)
    return struct


def _search_result(index: int) -> SearchResponse.SearchResult:
    return SearchResponse.SearchResult(
        id="doc-{0}".format(index),
        document={
            "id": "doc-{0}".format(index),
            "name": "{0}/branches/0/documents/doc-{1}".format(CONVERSATION_PARENT, index),
            "derived_struct_data": _struct(
                title=DOCUMENT_TITLE, snippets=[{"snippet": SNIPPET}]
            ),
        },
    )


def _answer(state: Answer.State) -> Answer:
    return Answer(
        name=SESSION_NAME + "/answers/answer-1",
        state=state,
        answer_text=ANSWER_TEXT,
        citations=[Answer.Citation(start_index=0, end_index=5, sources=[{"reference_id": "0"}])],
        references=[
            Answer.Reference(
                chunk_info=Answer.Reference.ChunkInfo(
                    chunk="chunk-{0}".format(index), content=CHUNK_CONTENT
                )
            )
            for index in range(ANSWER_REFERENCES)
        ],
    )


@dataclass
class Call:
    method: str
    request: Any
    metadata: Dict[str, Any]


class FakeDiscoveryEngine:
    """Loopback stand-in for discoveryengine.googleapis.com (gRPC, v1)."""

    def __init__(self) -> None:
        self.calls: List[Call] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        self.received = threading.Event()
        self._unavailable_sent: set = set()
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        unary = grpc.unary_unary_rpc_method_handler
        self._server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    SEARCH_SERVICE,
                    {
                        "Search": unary(
                            self._search("Search"),
                            request_deserializer=SearchRequest.deserialize,
                            response_serializer=SearchResponse.serialize,
                        ),
                        "SearchLite": unary(
                            self._search("SearchLite"),
                            request_deserializer=SearchRequest.deserialize,
                            response_serializer=SearchResponse.serialize,
                        ),
                    },
                ),
                grpc.method_handlers_generic_handler(
                    CONVERSATION_SERVICE,
                    {
                        "AnswerQuery": unary(
                            self._answer_query,
                            request_deserializer=AnswerQueryRequest.deserialize,
                            response_serializer=AnswerQueryResponse.serialize,
                        ),
                        "StreamAnswerQuery": grpc.unary_stream_rpc_method_handler(
                            self._stream_answer_query,
                            request_deserializer=AnswerQueryRequest.deserialize,
                            response_serializer=AnswerQueryResponse.serialize,
                        ),
                        "CreateConversation": unary(
                            self._create_conversation,
                            request_deserializer=CreateConversationRequest.deserialize,
                            response_serializer=Conversation.serialize,
                        ),
                    },
                ),
            )
        )
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        self.secure_port = self._server.add_secure_port(
            "127.0.0.1:0", grpc.local_server_credentials(grpc.LocalConnectionType.LOCAL_TCP)
        )
        self.target = "127.0.0.1:{0}".format(self.port)
        self.secure_target = "127.0.0.1:{0}".format(self.secure_port)
        self._server.start()

    # -- recording -----------------------------------------------------------

    def _record(self, method: str, request: Any, context: grpc.ServicerContext) -> Dict[str, Any]:
        metadata = {key.lower(): value for key, value in context.invocation_metadata()}
        with self._lock:
            self.calls.append(Call(method, request, metadata))
        self.received.set()
        return metadata

    def methods(self) -> List[str]:
        with self._lock:
            return [call.method for call in self.calls]

    # -- behaviours ----------------------------------------------------------

    def _fail(self, trigger: str, query: str, metadata: Dict[str, Any], context: Any) -> None:
        credential = "authorization={0} x-goog-api-key={1}".format(
            metadata.get("authorization", ""), metadata.get("x-goog-api-key", "")
        )
        if FAIL_DENIED in trigger:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Permission denied on serving config for query '{0}' ({1})".format(query, credential),
            )
        if FAIL_HUGE in trigger:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "echo {0} {1}".format(credential, "\u20ac" * HUGE_ERROR_CHARS),
            )
        if FAIL_UNSEEN in trigger:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "token {0} key {1} header Authorization: Bearer {2}".format(
                    UNSEEN_ACCESS_TOKEN, UNSEEN_API_KEY, BEARER_VALUE
                ),
            )
        if FAIL_UNAVAILABLE in trigger:
            context.abort(grpc.StatusCode.UNAVAILABLE, "backend unavailable")
        if UNAVAILABLE_ONCE in trigger:
            with self._lock:
                first = query not in self._unavailable_sent
                self._unavailable_sent.add(query)
            if first:
                context.abort(grpc.StatusCode.UNAVAILABLE, "try again")
        if SLOW in trigger:
            self._release.wait(30)
            context.abort(grpc.StatusCode.UNAVAILABLE, "released")

    def _search(self, method: str):
        def handler(request: SearchRequest, context: Any) -> SearchResponse:
            metadata = self._record(method, request, context)
            self._fail(request.query, request.query, metadata, context)
            if PAGES in request.query and request.page_token == "page-2":
                return SearchResponse(
                    results=[_search_result(SEARCH_RESULTS + index) for index in range(SECOND_PAGE_RESULTS)],
                    total_size=SEARCH_RESULTS + SECOND_PAGE_RESULTS,
                )
            return SearchResponse(
                results=[_search_result(index) for index in range(SEARCH_RESULTS)],
                total_size=SEARCH_RESULTS,
                attribution_token="attribution-token",
                next_page_token="page-2" if PAGES in request.query else "",
                summary={"summary_text": SUMMARY},
            )

        return handler

    def _answer_query(self, request: AnswerQueryRequest, context: Any) -> AnswerQueryResponse:
        metadata = self._record("AnswerQuery", request, context)
        query = request.query.text
        self._fail(query, query, metadata, context)
        if ANSWER_MISSING in query:
            return AnswerQueryResponse(answer_query_token="token-1")
        state = Answer.State.FAILED if ANSWER_FAILED in query else Answer.State.SUCCEEDED
        response = AnswerQueryResponse(answer=_answer(state), answer_query_token="token-1")
        if request.session:
            # A new session ("sessions/-") comes back with its real name.
            response.session = Session(name=SESSION_NAME)
        return response

    def _stream_answer_query(self, request: AnswerQueryRequest, context: Any) -> Iterator[AnswerQueryResponse]:
        self._record("StreamAnswerQuery", request, context)
        yield AnswerQueryResponse(answer=_answer(Answer.State.STREAMING))
        yield AnswerQueryResponse(answer=_answer(Answer.State.SUCCEEDED))

    def _create_conversation(self, request: CreateConversationRequest, context: Any) -> Conversation:
        self._record("CreateConversation", request, context)
        return Conversation(name=request.parent + "/conversations/conversation-1")

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._release.set()
        self._server.stop(grace=None).wait(5)

    def __enter__(self) -> "FakeDiscoveryEngine":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# -- clients ------------------------------------------------------------------


def _local_credentials() -> grpc.ChannelCredentials:
    return grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP)


def search_client(fake: FakeDiscoveryEngine, credentials: Any = None) -> Any:
    """A real SearchServiceClient on the gRPC transport, talking to the fake."""
    from google.cloud.discoveryengine_v1 import SearchServiceClient
    from google.cloud.discoveryengine_v1.services.search_service.transports.grpc import (
        SearchServiceGrpcTransport,
    )

    if credentials is None:
        transport = SearchServiceGrpcTransport(channel=grpc.insecure_channel(fake.target))
    else:
        transport = SearchServiceGrpcTransport(
            host=fake.secure_target,
            credentials=credentials,
            ssl_channel_credentials=_local_credentials(),
        )
    return SearchServiceClient(transport=transport)


def search_client_with_api_key(fake: FakeDiscoveryEngine, api_key: str) -> Any:
    """The search_lite set-up: ``client_options.api_key``, the client builds the credentials."""
    from google.cloud.discoveryengine_v1 import SearchServiceClient
    from google.cloud.discoveryengine_v1.services.search_service.transports.grpc import (
        SearchServiceGrpcTransport,
    )

    def transport(**kwargs: Any) -> Any:
        kwargs["host"] = fake.secure_target
        return SearchServiceGrpcTransport(ssl_channel_credentials=_local_credentials(), **kwargs)

    return SearchServiceClient(client_options={"api_key": api_key}, transport=transport)


def async_search_client(fake: FakeDiscoveryEngine, credentials: Any = None) -> Any:
    """A real SearchServiceAsyncClient; build it inside the running event loop."""
    from google.cloud.discoveryengine_v1 import SearchServiceAsyncClient
    from google.cloud.discoveryengine_v1.services.search_service.transports.grpc_asyncio import (
        SearchServiceGrpcAsyncIOTransport,
    )

    if credentials is None:
        transport = SearchServiceGrpcAsyncIOTransport(channel=grpc.aio.insecure_channel(fake.target))
    else:
        transport = SearchServiceGrpcAsyncIOTransport(
            host=fake.secure_target,
            credentials=credentials,
            ssl_channel_credentials=_local_credentials(),
        )
    return SearchServiceAsyncClient(transport=transport)


def answer_client(fake: FakeDiscoveryEngine, credentials: Any = None) -> Any:
    from google.cloud.discoveryengine_v1 import ConversationalSearchServiceClient
    from google.cloud.discoveryengine_v1.services.conversational_search_service.transports.grpc import (
        ConversationalSearchServiceGrpcTransport,
    )

    if credentials is None:
        transport = ConversationalSearchServiceGrpcTransport(
            channel=grpc.insecure_channel(fake.target)
        )
    else:
        transport = ConversationalSearchServiceGrpcTransport(
            host=fake.secure_target,
            credentials=credentials,
            ssl_channel_credentials=_local_credentials(),
        )
    return ConversationalSearchServiceClient(transport=transport)


def async_answer_client(fake: FakeDiscoveryEngine, credentials: Any = None) -> Any:
    from google.cloud.discoveryengine_v1 import ConversationalSearchServiceAsyncClient
    from google.cloud.discoveryengine_v1.services.conversational_search_service.transports.grpc_asyncio import (
        ConversationalSearchServiceGrpcAsyncIOTransport,
    )

    if credentials is None:
        transport = ConversationalSearchServiceGrpcAsyncIOTransport(
            channel=grpc.aio.insecure_channel(fake.target)
        )
    else:
        transport = ConversationalSearchServiceGrpcAsyncIOTransport(
            host=fake.secure_target,
            credentials=credentials,
            ssl_channel_credentials=_local_credentials(),
        )
    return ConversationalSearchServiceAsyncClient(transport=transport)


def oauth_credentials(token: str = ACCESS_TOKEN) -> Any:
    """User credentials holding a placeholder access token (never refreshed)."""
    from google.oauth2.credentials import Credentials

    return Credentials(token=token)


def api_key_credentials(key: str = API_KEY) -> Any:
    from google.auth import api_key

    return api_key.Credentials(key)


def search_request(query: str = "open telemetry retrieval", **fields: Any) -> Dict[str, Any]:
    request: Dict[str, Any] = {"serving_config": SERVING_CONFIG, "query": query}
    request.update(fields)
    return request


def answer_request(query: str = "what is open telemetry", **fields: Any) -> Dict[str, Any]:
    request: Dict[str, Any] = {"serving_config": SERVING_CONFIG, "query": {"text": query}}
    request.update(fields)
    return request


# -- tracing ------------------------------------------------------------------


@dataclass
class Traced:
    exporter: InMemorySpanExporter
    provider: TracerProvider

    def spans(self) -> List[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def names(self) -> List[str]:
        return [span.name for span in self.spans()]

    def one(self) -> ReadableSpan:
        spans = self.spans()
        assert len(spans) == 1, [span.name for span in spans]
        return spans[0]

    def wire(self) -> str:
        """Everything an exporter could send: attributes, events, status."""
        return "".join(span.to_json() for span in self.spans())


def new_provider() -> Tuple[InMemorySpanExporter, TracerProvider]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-discoveryengine"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


@contextlib.contextmanager
def instrumented(**options: Any) -> Iterator[Traced]:
    """Instrument Discovery Engine against a fresh in-memory provider; uninstrument after."""
    from traceai_discoveryengine import DiscoveryEngineInstrumentor

    exporter, provider = new_provider()
    instrumentor = DiscoveryEngineInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        yield Traced(exporter, provider)
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def event(span: ReadableSpan, name: str) -> Optional[Dict[str, Any]]:
    for item in span.events:
        if item.name == name:
            return dict(item.attributes or {})
    return None
