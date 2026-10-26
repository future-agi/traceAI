"""Tests for the instrumentor module."""

import os
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from traceai_strands._instrumentor import (
    StrandsInstrumentor,
    configure_strands_tracing,
    create_traced_agent,
)


class TestStrandsInstrumentor:
    """Tests for StrandsInstrumentor class."""

    def setup_method(self):
        """Reset singleton state before each test."""
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    def test_singleton_pattern(self):
        """Test that instrumentor follows singleton pattern."""
        inst1 = StrandsInstrumentor()
        inst2 = StrandsInstrumentor()

        assert inst1 is inst2

    def test_instrument_sets_environment_variables(self, mock_tracer_provider):
        """Test that instrumentation sets OTEL environment variables."""
        instrumentor = StrandsInstrumentor()

        with patch.dict(os.environ, {}, clear=True):
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                otlp_headers={"Authorization": "Bearer token"},
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://localhost:4318"
            assert "Authorization=Bearer token" in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")

    def test_instrument_returns_self(self, mock_tracer_provider):
        """Test that instrument returns self for method chaining."""
        instrumentor = StrandsInstrumentor()

        result = instrumentor.instrument(tracer_provider=mock_tracer_provider)

        assert result is instrumentor

    def test_instrument_idempotent(self, mock_tracer_provider):
        """Test that calling instrument multiple times is safe."""
        instrumentor = StrandsInstrumentor()

        with patch.dict(os.environ, {}, clear=True):
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://first:4318",
            )
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://second:4318",
            )

            # Should keep first value since already instrumented
            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://first:4318"

    def test_is_instrumented_property(self, mock_tracer_provider):
        """Test is_instrumented property."""
        instrumentor = StrandsInstrumentor()

        assert instrumentor.is_instrumented is False

        instrumentor.instrument(tracer_provider=mock_tracer_provider)

        assert instrumentor.is_instrumented is True

    def test_uninstrument(self, mock_tracer_provider):
        """Test uninstrumentation."""
        instrumentor = StrandsInstrumentor()

        instrumentor.instrument(tracer_provider=mock_tracer_provider)
        assert instrumentor.is_instrumented is True

        instrumentor.uninstrument()
        assert instrumentor.is_instrumented is False


class TestConfigureStrandsTracing:
    """Tests for configure_strands_tracing function."""

    def setup_method(self):
        """Reset singleton state before each test."""
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    def test_returns_instrumentor(self, mock_tracer_provider):
        """Test that function returns StrandsInstrumentor."""
        result = configure_strands_tracing(tracer_provider=mock_tracer_provider)

        assert isinstance(result, StrandsInstrumentor)

    def test_with_otlp_endpoint(self, mock_tracer_provider):
        """Test configuration with OTLP endpoint."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://localhost:4318"

    def test_with_project_name(self, mock_tracer_provider):
        """Test configuration with project name."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                project_name="my-project",
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "x-project-name=my-project" in headers

    def test_with_headers(self, mock_tracer_provider):
        """Test configuration with custom headers."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                otlp_headers={"Authorization": "Bearer token"},
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "Authorization=Bearer token" in headers


class TestCreateTracedAgent:
    """Tests for create_traced_agent function."""

    def test_creates_agent_with_trace_attributes(self):
        """Test creating agent with trace attributes."""
        # Mock strands.Agent
        mock_agent_class = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        with patch.dict("sys.modules", {"strands": MagicMock(Agent=mock_agent_class)}):
            from traceai_strands._instrumentor import create_traced_agent

            agent = create_traced_agent(
                model="gpt-4",
                system_prompt="You are helpful.",
                session_id="sess-123",
                user_id="user@example.com",
                tags=["production"],
            )

            # Verify Agent was called with trace_attributes
            call_kwargs = mock_agent_class.call_args[1]
            assert "trace_attributes" in call_kwargs
            assert call_kwargs["trace_attributes"]["session.id"] == "sess-123"
            assert call_kwargs["trace_attributes"]["user.id"] == "user@example.com"
            assert call_kwargs["trace_attributes"]["tags"] == ["production"]

    def test_merges_existing_trace_attributes(self):
        """Test that existing trace_attributes are merged."""
        mock_agent_class = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        with patch.dict("sys.modules", {"strands": MagicMock(Agent=mock_agent_class)}):
            from traceai_strands._instrumentor import create_traced_agent

            agent = create_traced_agent(
                model="gpt-4",
                system_prompt="You are helpful.",
                session_id="sess-123",
                trace_attributes={"custom": "value"},
            )

            call_kwargs = mock_agent_class.call_args[1]
            assert call_kwargs["trace_attributes"]["session.id"] == "sess-123"
            assert call_kwargs["trace_attributes"]["custom"] == "value"


class _RecordingStrandsTelemetry:
    """Stands in for strands.telemetry.StrandsTelemetry and records which exporters were set up."""

    calls: list = []

    def __init__(self, tracer_provider=None):
        self.tracer_provider = tracer_provider

    def setup_otlp_exporter(self, **kwargs):
        self.calls.append("otlp")
        return self

    def setup_console_exporter(self, **kwargs):
        self.calls.append("console")
        return self

    def setup_meter(self, **kwargs):
        self.calls.append("meter")
        return self


def _env_headers():
    raw = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or os.environ.get(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    headers = {}
    for pair in raw.split(","):
        name, sep, value = pair.partition("=")
        if sep and name.strip():
            headers[name.strip().lower()] = value.strip()
    return headers


class _ResolvingOtlpSpanExporter:
    """Stands in for the upstream OTLP/HTTP span exporter that Strands' setup_otlp_exporter() builds.

    conftest.py mocks opentelemetry, so this resolves the endpoint and headers from the
    OTEL_EXPORTER_OTLP_* variables the way the upstream exporter does, and keeps them where
    releases before 1.45 keep them (`_endpoint`, `_headers`). The real exporter is covered by
    test_real_export.py.
    """

    built: list = []

    def __init__(self):
        base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "http://localhost:4318"
        self._endpoint = (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or base.removesuffix("/") + "/v1/traces"
        )
        self._headers = _env_headers()
        self.shut_down = False
        self.built.append(self)

    def shutdown(self):
        self.shut_down = True


class _ResolvingOtlpSpanExporterWithClient(_ResolvingOtlpSpanExporter):
    """The same, with the headers on an inner `_client`, where releases from 1.45 keep them."""

    def __init__(self):
        super().__init__()
        self._client = SimpleNamespace(_headers=self.__dict__.pop("_headers"))


_FUTURE_AGI = "http://127.0.0.1:5999/tracer/v1/traces"
_FI_KEYS = {"X-Api-Key": "fi-api-key", "X-Secret-Key": "fi-secret-key"}


def _provider_exporting_to(
    endpoint, headers=_FI_KEYS, batch=False, marked=None, headers_on_client=False
):
    """A tracer provider shaped like the SDK's, with one processor exporting to `endpoint`.

    `headers` are what that exporter sends; None leaves them unreadable. `marked` sets the
    `_default_processor` flag fi-instrumentation-otel 1.1.0 and earlier leave on the provider
    `register()` returns, after which the next add_span_processor() call replaces the exporter.
    """
    exporter = SimpleNamespace(_endpoint=endpoint)
    if headers is not None:
        if headers_on_client:
            exporter._client = SimpleNamespace(_headers=dict(headers))
        else:
            exporter._headers = dict(headers)
    if batch:
        # Recent OTel SDKs keep a BatchSpanProcessor's exporter on an inner _batch_processor.
        processor = SimpleNamespace(_batch_processor=SimpleNamespace(_exporter=exporter))
    else:
        processor = SimpleNamespace(span_exporter=exporter)
    provider = SimpleNamespace(
        _active_span_processor=SimpleNamespace(_span_processors=(processor,))
    )
    if marked is not None:
        provider._default_processor = marked
    return provider


def _future_agi_keys_header():
    return ",".join(f"{name}={value}" for name, value in _FI_KEYS.items())


class TestStrandsOtlpExporterIsNotDoubled:
    """Strands' OTLP exporter is not added when it would only send copies of what the provider sends.

    With register()'s provider, the Future AGI exporter already sends every span. Pointing
    OTEL_EXPORTER_OTLP_* at Future AGI as well used to add a second exporter for the same
    collector, so every span arrived twice once fi-instrumentation-otel stopped dropping
    register()'s exporter on add_span_processor() (TH-8727). The exporter is skipped only for the
    same traces URL, with exactly the same header names and values on both exporters, and only
    when the provider keeps its exporter on the next add_span_processor() call.
    """

    _ENV = (
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
        "OTEL_EXPORTER_OTLP_CLIENT_KEY",
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
    )

    @pytest.fixture(autouse=True)
    def _recording_telemetry(self, monkeypatch):
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False
        _RecordingStrandsTelemetry.calls = []
        _ResolvingOtlpSpanExporter.built = []
        monkeypatch.setattr(
            sys.modules["strands.telemetry"], "StrandsTelemetry", _RecordingStrandsTelemetry
        )
        monkeypatch.setattr(
            sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"],
            "OTLPSpanExporter",
            _ResolvingOtlpSpanExporter,
        )
        for name in self._ENV:
            # setenv first, so monkeypatch restores the variable even when
            # configure_strands_tracing() writes it (otlp_endpoint=, otlp_headers=).
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
        # Model README Option 1: Strands' exporter and the provider both carry the Future AGI keys.
        # Tests that need absent/different headers set their own signal-specific override.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", _future_agi_keys_header())
        yield
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    @pytest.mark.parametrize("batch", [False, True], ids=["simple", "batch"])
    def test_not_added_when_the_traces_endpoint_is_the_providers(self, monkeypatch, batch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, batch=batch),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_not_added_when_the_base_endpoint_resolves_to_the_providers(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5999/tracer/")

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_not_added_when_otlp_endpoint_resolves_to_the_providers(self):
        # configure_strands_tracing() stores otlp_endpoint in OTEL_EXPORTER_OTLP_ENDPOINT.
        configure_strands_tracing(
            tracer_provider=_provider_exporting_to("https://api.futureagi.com/tracer/v1/traces"),
            otlp_endpoint="https://API.futureagi.com:443/tracer",
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    @pytest.mark.parametrize(
        "traces_endpoint",
        [
            "http://127.0.0.1:5999/tracer/v1/traces/",  # trailing slash
            "http://127.0.0.1:5999//tracer//v1/traces",  # repeated slashes
            "HTTP://127.0.0.1:5999/tracer/v1/traces",  # scheme case
            "http://127.0.0.1:5999/tracer/v1/traces#fragment",  # never sent
        ],
    )
    def test_not_added_for_the_same_url_written_differently(self, monkeypatch, traces_endpoint):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", traces_endpoint)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_not_added_for_the_default_port_written_out(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://Collector.Example:80/t")

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to("http://collector.example/t"),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_console_exporter_and_meter_are_still_set_up(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI),
            enable_console_exporter=True,
            enable_metrics=True,
        )

        assert _RecordingStrandsTelemetry.calls == ["console", "meter"]

    @pytest.mark.parametrize(
        "traces_endpoint",
        [
            "http://127.0.0.1:6000/tracer/v1/traces",  # another port
            "http://collector.internal:5999/tracer/v1/traces",  # another host
            "http://127.0.0.1:5999/v1/traces",  # same host and port, another path
            "https://127.0.0.1:5999/tracer/v1/traces",  # another scheme
            "http://127.0.0.1:5999/tracer/v1/traces?tenant=b",  # another query
            "http://user:pw@127.0.0.1:5999/tracer/v1/traces",  # credentials in the URL
        ],
        ids=["port", "host", "path", "scheme", "query", "userinfo"],
    )
    def test_added_for_another_traces_url(self, monkeypatch, traces_endpoint):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", traces_endpoint)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_for_the_default_endpoint_when_nothing_is_set(self):
        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_when_the_provider_has_no_span_processors(
        self, monkeypatch, mock_tracer_provider
    ):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(tracer_provider=mock_tracer_provider, enable_metrics=False)

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    # Headers: skip only when both exporters send the exact same set of names and values.

    def test_not_added_when_it_sends_the_same_keys(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", _future_agi_keys_header().lower())

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_added_when_only_it_carries_the_keys(self):
        # register() without FI_API_KEY/FI_SECRET_KEY: its exporter sends no keys, so only
        # Strands' exporter, given them through otlp_headers, is accepted by Future AGI.
        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, headers={}),
            otlp_endpoint="http://127.0.0.1:5999/tracer",
            otlp_headers=dict(_FI_KEYS),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    @pytest.mark.parametrize(
        "provider_headers,env_headers",
        [
            pytest.param(
                {**_FI_KEYS, "X-Provider-Route": "provider-only"},
                _future_agi_keys_header(),
                id="provider-extra",
            ),
            pytest.param(
                _FI_KEYS,
                f"{_future_agi_keys_header()},X-Strands-Route=strands-only",
                id="strands-extra",
            ),
        ],
    )
    def test_added_when_either_side_has_an_extra_header(
        self, monkeypatch, provider_headers, env_headers
    ):
        # An extra routing header on either exporter can change where the collector routes the
        # spans. A subset check would wrongly skip Strands' exporter in the provider-extra case.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", env_headers)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, headers=provider_headers),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_when_it_sends_other_values_for_the_same_names(self, monkeypatch):
        # Same header names, other keys: the two exporters authenticate as different projects or
        # tenants, so Strands' exporter is not a duplicate. Comparing names alone would skip it.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "X-Api-Key=other-key,X-Secret-Key=other-secret"
        )

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_when_project_name_adds_a_header(self, monkeypatch):
        # project_name= reaches the backend only as Strands' x-project-name header.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI),
            project_name="strands-agent",
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_when_the_providers_headers_cannot_be_read(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, headers=None),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    @pytest.mark.parametrize(
        "variable",
        [
            "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
            "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
            "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
            "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
        ],
    )
    def test_added_when_it_would_authenticate_outside_its_headers(self, monkeypatch, variable):
        # A client certificate or a credential provider is not visible in the headers, so the
        # exporters cannot be shown to send the same thing.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)
        monkeypatch.setenv(variable, "set")

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_headers_are_read_where_recent_exporters_keep_them(self, monkeypatch):
        # From opentelemetry-exporter-otlp-proto-http 1.45 the headers sit on an inner _client.
        monkeypatch.setattr(
            sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"],
            "OTLPSpanExporter",
            _ResolvingOtlpSpanExporterWithClient,
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", _future_agi_keys_header())

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, headers_on_client=True),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_the_exporter_built_to_resolve_the_endpoint_is_shut_down(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert [e.shut_down for e in _ResolvingOtlpSpanExporter.built] == [True]

    # fi-instrumentation-otel 1.1.0 and earlier: the next add_span_processor() call replaces
    # register()'s exporter, so skipping Strands' exporter would leave nothing exporting there
    # once any processor is added (the console exporter, or one the application adds).

    @pytest.mark.parametrize("console", [False, True], ids=["no-console", "console"])
    def test_added_when_the_next_processor_would_replace_the_providers_exporter(
        self, monkeypatch, console
    ):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, marked=True),
            enable_console_exporter=console,
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == (["otlp", "console"] if console else ["otlp"])

    def test_not_added_when_the_provider_has_no_mark_set(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, marked=False),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []
