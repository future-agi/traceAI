import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fi_instrumentation import TraceConfig, suppress_tracing
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, OutboundInterceptor
from temporalio.converter import PayloadConverter
from temporalio.worker import ActivityInboundInterceptor, ActivityOutboundInterceptor, Worker

from traceai_temporal import TemporalInstrumentor, TemporalInterceptor
from traceai_temporal._propagation import HEADER, decode, extract, inject


def test_headers_bounded_and_trace_only(tracing):
    provider, _ = tracing
    keep = Payload(data=b"application-header")
    original = {"app": keep}
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        headers = inject(original, context.get_current())
    assert original == {"app": keep}
    assert headers["app"] is keep
    assert set(decode(headers)) == {"traceparent"}
    extracted = trace.get_current_span(extract(headers)).get_span_context()
    assert extracted.trace_id == parent.get_span_context().trace_id
    assert extracted.span_id == parent.get_span_context().span_id
    assert extracted.is_remote


@pytest.mark.parametrize("payload", [
    Payload(data=b"{"),
    Payload(metadata={"encoding": b"json/plain"}, data=b"[1, 2]"),
    Payload(metadata={"encoding": b"json/plain"}, data=b'"not a dict"'),
    Payload(metadata={"encoding": b"json/plain"}, data=b"x" * 4097),
    PayloadConverter.default.to_payloads([{"traceparent": 123}])[0],
    PayloadConverter.default.to_payloads([{"traceparent": "invalid"}])[0],
])
def test_malformed_header_fails_open(payload):
    assert not trace.get_current_span(extract({HEADER: payload})).get_span_context().is_valid


def test_no_baggage_tracestate_or_payload_content(tracing):
    provider, _ = tracing
    from opentelemetry import baggage
    sc = trace.SpanContext(123, 456, False, trace.TraceFlags(1), trace.TraceState([("vendor", "private-token")]))
    ctx = baggage.set_baggage("api-key", "private-secret", trace.set_span_in_context(trace.NonRecordingSpan(sc)))
    headers = inject({}, ctx)
    data = headers[HEADER].data
    assert b"private" not in data
    assert b"baggage" not in data
    assert b"tracestate" not in data


def test_completed_workflow_fixed_identity_and_timestamps(tracing):
    provider, exporter = tracing
    interceptor = TemporalInterceptor(provider)
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        headers = inject({}, context.get_current())
    interceptor._complete_workflow({
        "parent": headers, "trace_id": parent.get_span_context().trace_id, "span_id": 4321,
        "name": "temporal.workflow Unit", "start_ns": 100, "end_ns": 200,
        "attributes": {"gen_ai.span.kind": "AGENT"}, "error": None,
    })
    span = exporter.get_finished_spans()[-1]
    assert span.context.span_id == 4321
    assert span.context.trace_id == parent.get_span_context().trace_id
    assert span.parent.span_id == parent.get_span_context().span_id
    assert (span.start_time, span.end_time) == (100, 200)


async def test_client_preserves_result_identity_exception_and_headers(tracing):
    provider, exporter = tracing
    sentinel = object()
    failure = ValueError("original secret")

    class Next(OutboundInterceptor):
        async def start_workflow(self, input):
            assert input.headers["app"].data == b"keep"
            assert trace.get_current_span(extract(input.headers)).get_span_context().is_valid
            if input.id == "bad":
                raise failure
            return sentinel

    interceptor = TemporalInterceptor(provider).intercept_client(Next(None))
    def input(id):
        return SimpleNamespace(workflow="Example", id=id, headers={"app": Payload(data=b"keep")})
    assert await interceptor.start_workflow(input("good")) is sentinel
    with pytest.raises(ValueError) as caught:
        await interceptor.start_workflow(input("bad"))
    assert caught.value is failure
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[-1].status.status_code is trace.StatusCode.ERROR
    assert "secret" not in repr(spans[-1].attributes)
    assert not trace.get_current_span().get_span_context().is_valid


async def test_tracer_failures_do_not_repeat_or_replace_application_calls(tracing):
    provider, _ = tracing
    interceptor = TemporalInterceptor(provider)
    calls = []
    sentinel = object()

    class Next(OutboundInterceptor):
        async def start_workflow(self, input):
            calls.append(input)
            return sentinel

    with patch.object(type(interceptor.tracer), "start_span", side_effect=RuntimeError("broken")):
        result = await interceptor.intercept_client(Next(None)).start_workflow(SimpleNamespace(workflow="X", id="x", headers={}))
    assert result is sentinel
    assert len(calls) == 1


async def test_suppression_propagates_without_spans(tracing):
    provider, exporter = tracing
    interceptor = TemporalInterceptor(provider)

    class Next(OutboundInterceptor):
        async def start_workflow(self, input):
            assert decode(input.headers) == {"disabled": True}
            return "ok"

    with suppress_tracing():
        assert await interceptor.intercept_client(Next(None)).start_workflow(SimpleNamespace(workflow="X", id="x", headers={})) == "ok"
    assert not exporter.get_finished_spans()


def test_constructor_hooks_idempotent_preserve_interceptors_and_uninstrument(tracing):
    provider, _ = tracing
    seen = []
    def fake_client(self, *args, **kwargs):
        seen.append(kwargs["interceptors"])
    def fake_worker(self, *args, **kwargs):
        seen.append(kwargs["interceptors"])
    instrumentor = TemporalInstrumentor()
    marker = object()
    with patch.object(Client, "__init__", fake_client), patch.object(Worker, "__init__", fake_worker):
        instrumentor.instrument(tracer_provider=provider)
        try:
            instrumentor.instrument(tracer_provider=provider)
            values = [marker]
            Client(None, interceptors=values)
            assert values == [marker]
            assert seen[-1][0] is marker
            assert sum(isinstance(i, TemporalInterceptor) for i in seen[-1]) == 1
            explicit = TemporalInterceptor(provider)
            Client(None, interceptors=[explicit])
            assert seen[-1] == [explicit]
            inherited = SimpleNamespace(config=lambda: {"interceptors": [explicit]})
            Worker(inherited, interceptors=[marker])
            assert seen[-1] == [marker]
            Worker(SimpleNamespace(config=lambda: {"interceptors": []}), interceptors=[marker])
            assert sum(isinstance(i, TemporalInterceptor) for i in seen[-1]) == 1
        finally:
            instrumentor.uninstrument()
        assert Client.__init__ is fake_client
        assert Worker.__init__ is fake_worker


def test_span_cleanup_failure_and_cancellation_keep_original(tracing):
    provider, _ = tracing
    interceptor = TemporalInterceptor(provider)
    cancellation = asyncio.CancelledError()
    span = Mock(spec=trace.Span)
    span.get_span_context.return_value = trace.SpanContext(123, 456, False, trace.TraceFlags(1))
    span.set_attribute.side_effect = RuntimeError("status broken")
    span.end.side_effect = RuntimeError("export broken")
    with patch.object(type(interceptor.tracer), "start_span", return_value=span):
        with pytest.raises(asyncio.CancelledError) as caught:
            with interceptor._span("cancel", trace.SpanKind.SERVER, {}):
                raise cancellation
    span.end.assert_called_once()
    assert caught.value is cancellation
    assert trace.get_current_span() is trace.INVALID_SPAN


@pytest.mark.parametrize("suppressed", [False, True])
async def test_update_with_start_preserves_identity_exception_and_headers(tracing, suppressed):
    provider, exporter = tracing
    sentinel, failure = object(), ValueError("private update failure")
    requests = []
    keep = Payload(data=b"keep")

    class Next(OutboundInterceptor):
        async def start_update_with_start_workflow(self, input):
            requests.append(input)
            start = input.start_workflow_input
            assert start.headers["app"] is keep
            assert input.update_workflow_input.headers == {"update": keep}
            if suppressed:
                assert decode(start.headers) == {"disabled": True}
            else:
                assert trace.get_current_span(extract(start.headers)).get_span_context().is_valid
            if start.id == "bad":
                raise failure
            return sentinel

    def request(id):
        return SimpleNamespace(
            start_workflow_input=SimpleNamespace(workflow="Update", id=id, headers={"app": keep}),
            update_workflow_input=SimpleNamespace(headers={"update": keep}),
        )

    from contextlib import nullcontext
    outbound = TemporalInterceptor(provider).intercept_client(Next(None))
    with suppress_tracing() if suppressed else nullcontext():
        assert await outbound.start_update_with_start_workflow(request("good")) is sentinel
        with pytest.raises(ValueError) as caught:
            await outbound.start_update_with_start_workflow(request("bad"))
    assert caught.value is failure
    assert len(requests) == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == (0 if suppressed else 2)
    if not suppressed:
        assert spans[-1].status.status_code is trace.StatusCode.ERROR
        assert "private" not in repr(spans[-1].attributes)
    assert trace.get_current_span() is trace.INVALID_SPAN


def test_local_suppression_survives_extracted_context_and_is_restored(tracing):
    provider, exporter = tracing
    interceptor = TemporalInterceptor(provider)
    with provider.get_tracer("test").start_as_current_span("remote") as parent:
        headers = inject({}, context.get_current())
    with suppress_tracing():
        with interceptor._span("activity", trace.SpanKind.SERVER, {}, headers=headers):
            assert context.get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
            assert trace.get_current_span().get_span_context().trace_id == parent.get_span_context().trace_id
            with interceptor.tracer.start_as_current_span("model", attributes={"output.value": "private output"}):
                pass
        assert context.get_value(_SUPPRESS_INSTRUMENTATION_KEY) is True
    assert not context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
    assert [s.name for s in exporter.get_finished_spans()] == ["remote"]


def test_worker_hook_uses_active_client_configuration(tracing):
    provider, _ = tracing
    explicit = TemporalInterceptor(provider)
    observed = []

    def config(*, active_config=False):
        observed.append(active_config)
        return {"interceptors": [explicit] if active_config else []}

    seen = []
    def fake_worker(self, *args, **kwargs):
        seen.append(kwargs["interceptors"])

    instrumentor = TemporalInstrumentor()
    marker = object()
    with patch.object(Worker, "__init__", fake_worker):
        instrumentor.instrument(tracer_provider=provider)
        try:
            Worker(SimpleNamespace(config=config), interceptors=[marker])
        finally:
            instrumentor.uninstrument()
    assert observed == [True]
    assert seen == [[marker]]


@pytest.mark.parametrize("plugin_tracing", ["none", "one", "duplicates"])
def test_client_plugin_finalization_preserves_config_and_clone_idempotence(tracing, plugin_tracing):
    import temporalio.client as sdk_client
    if not hasattr(sdk_client, "Plugin"):
        pytest.skip("client plugins require a newer Temporal SDK")
    provider, _ = tracing
    first, last = sdk_client.Interceptor(), sdk_client.Interceptor()
    explicit, duplicate = TemporalInterceptor(provider), TemporalInterceptor(provider)
    original = [first]
    calls = []

    class Plugin(sdk_client.Plugin):
        def configure_client(self, config):
            calls.append(config["namespace"])
            additions = {"none": [], "one": [explicit], "duplicates": [explicit, duplicate]}[plugin_tracing]
            return {**config, "interceptors": [*config["interceptors"], *additions, last]}

        async def connect_service_client(self, config, next):
            return await next(config)

    plugins = [Plugin()]
    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = Client(None, namespace="unchanged", interceptors=original, plugins=plugins)
        clone = Client(**client.config())
        for configured in (client, clone):
            active = configured.config(active_config=True)["interceptors"]
            assert [i for i in active if not isinstance(i, TemporalInterceptor)] == [first, last]
            traced = [i for i in active if isinstance(i, TemporalInterceptor)]
            assert len(traced) == 1
            if plugin_tracing != "none":
                assert active == [first, explicit, last]
            assert configured.config()["interceptors"] == [first]
            assert configured.config()["namespace"] == "unchanged"
        assert original == [first]
        assert len(plugins) == 1
        assert calls == ["unchanged", "unchanged"]
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("initial_plugin", [False, True])
@pytest.mark.parametrize("prepend", [False, True])
def test_cloned_client_new_privacy_plugin_overrides_retained_automatic_default(tracing, initial_plugin, prepend):
    import temporalio.client as sdk_client
    from traceai_temporal._instrumentor import _TracingPlugin

    if not hasattr(sdk_client, "Plugin"):
        pytest.skip("client plugins require a newer Temporal SDK")
    provider, _ = tracing
    hidden = TemporalInterceptor(provider, TraceConfig(hide_inputs=True, hide_outputs=True))
    calls = []

    class Plugin(sdk_client.Plugin):
        def configure_client(self, config):
            calls.append("original")
            return config

        async def connect_service_client(self, config, next):
            return await next(config)

    class PrivacyPlugin(Plugin):
        def configure_client(self, config):
            calls.append("privacy")
            existing = config["interceptors"]
            return {**config, "interceptors": [hidden, *existing] if prepend else [*existing, hidden]}

    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = Client(None, plugins=[Plugin()] if initial_plugin else [])
        original_config = client.config()
        clone_plugins = [*original_config["plugins"], PrivacyPlugin()]
        clone = Client(**{**original_config, "plugins": clone_plugins})
        # Cloning that clone must not reintroduce an earlier finalizer either.
        clone_again = Client(**clone.config())
        for configured in (clone, clone_again):
            active = configured.config(active_config=True)["interceptors"]
            assert [i for i in active if isinstance(i, TemporalInterceptor)] == [hidden]
            assert sum(isinstance(p, _TracingPlugin) for p in configured.config()["plugins"]) == 1
        assert client.config(active_config=True)["interceptors"][0]._automatic
        assert len(original_config["plugins"]) == (2 if initial_plugin else 0)
        assert len(clone_plugins) == len(original_config["plugins"]) + 1
        assert calls == (["original", "original", "privacy", "original", "privacy"]
                         if initial_plugin else ["privacy", "privacy"])
    finally:
        instrumentor.uninstrument()


def test_cloned_client_explicit_privacy_overrides_default_without_plugins(tracing):
    provider, _ = tracing
    hidden = TemporalInterceptor(provider, TraceConfig(hide_inputs=True, hide_outputs=True))
    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = Client(None)
        config = client.config()
        clone = Client(**{**config, "interceptors": [*config["interceptors"], hidden]})
        try:
            effective = clone.config(active_config=True)["interceptors"]
        except TypeError:
            effective = clone.config()["interceptors"]
        assert effective == [hidden]
        assert config["interceptors"][0]._automatic
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("explicit_first", [False, True])
async def test_final_worker_interceptor_chain_prefers_explicit_privacy_once(tracing, explicit_first):
    from unittest.mock import AsyncMock
    from traceai_temporal._interceptor import _activity_selection
    from traceai_temporal._workflow import EXTERN, TemporalWorkflowInterceptor

    provider, exporter = tracing
    automatic = TemporalInterceptor(provider)
    automatic._automatic = True
    explicit = TemporalInterceptor(provider, TraceConfig(hide_inputs=True, hide_outputs=True))
    roots = [explicit, automatic] if explicit_first else [automatic, explicit]
    externs = {}
    classes = [root.workflow_interceptor_class(SimpleNamespace(unsafe_extern_functions=externs)) for root in roots]
    assert [cls for cls in classes if cls is not None] == [TemporalWorkflowInterceptor]
    assert externs[EXTERN].__self__ is explicit
    terminal = Mock(spec=ActivityInboundInterceptor)
    terminal.execute_activity = AsyncMock(return_value="private-output")
    inbound = terminal
    # Include an ordinary application interceptor between both tracing entries.
    for root in reversed(roots):
        inbound = root.intercept_activity(ActivityInboundInterceptor(inbound))
    inbound.init(ActivityOutboundInterceptor(None))
    assert _activity_selection.get() is None
    info = SimpleNamespace(activity_type="echo", workflow_id="unit", workflow_run_id="run",
                           activity_id="1", attempt=1, is_local=False)
    with patch("traceai_temporal._interceptor.activity.info", return_value=info):
        assert await inbound.execute_activity(SimpleNamespace(headers={}, args=["private-input"])) == "private-output"
    terminal.execute_activity.assert_awaited_once()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "private-" not in str(spans[0].attributes)
    # Selecting a worker's explicit config must never mutate the inherited root
    # or change a later worker that uses only the automatic interceptor.
    assert not automatic.config.hide_inputs and not automatic.config.hide_outputs
    exporter.clear()
    other = automatic.intercept_activity(terminal)
    other.init(ActivityOutboundInterceptor(None))
    with patch("traceai_temporal._interceptor.activity.info", return_value=info):
        await other.execute_activity(SimpleNamespace(headers={}, args=["private-input"]))
    assert "private-input" in exporter.get_finished_spans()[0].attributes["input.value"]


def test_application_plugin_failure_preserves_exception_and_call_count(tracing):
    import temporalio.client as sdk_client
    if not hasattr(sdk_client, "Plugin"):
        pytest.skip("client plugins require a newer Temporal SDK")
    provider, _ = tracing
    failure = ValueError("original plugin failure")
    calls = []

    class Plugin(sdk_client.Plugin):
        def configure_client(self, config):
            calls.append(config)
            raise failure

        async def connect_service_client(self, config, next):
            return await next(config)

    with pytest.raises(ValueError) as baseline:
        Client(None, plugins=[Plugin()])
    instrumentor = TemporalInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with pytest.raises(ValueError) as instrumented:
            Client(None, plugins=[Plugin()])
    finally:
        instrumentor.uninstrument()
    assert baseline.value is instrumented.value is failure
    assert len(calls) == 2


def test_tracing_plugin_finalization_fails_open(tracing):
    from traceai_temporal._instrumentor import _TracingPlugin

    provider, _ = tracing
    config = {"interceptors": None}
    assert _TracingPlugin(TemporalInterceptor(provider)).configure_client(config) is config
