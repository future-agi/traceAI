"""Tests for the span-processor / exporter / provider tuning exposed by the SDK.

These verify that the tuning params (previously silently dropped) are now accepted
and forwarded, and that env vars / upstream defaults still apply when unset.

Helpers below read upstream OTel internals in a version-tolerant way: OTel SDK
>=1.34 delegates to a `_batch_processor`, older versions store the values directly
on the processor.
"""

import os
import warnings
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import SpanLimits
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    TracerProvider,
    Transport,
    register,
)


def _bp_get(processor, name):
    """Read a batch-processor setting across OTel layouts."""
    holder = getattr(processor, "_batch_processor", processor)
    for obj, attr in ((holder, "_" + name), (processor, name), (processor, "_" + name)):
        if hasattr(obj, attr):
            return getattr(obj, attr)
    raise AttributeError(name)


def _queue_batch(processor):
    return _bp_get(processor, "max_queue_size"), _bp_get(processor, "max_export_batch_size")


def _exporter_of(processor):
    """Effective exporter across OTel layouts (batch and simple processors)."""
    bp = getattr(processor, "_batch_processor", None)
    if bp is not None and hasattr(bp, "_exporter"):
        return bp._exporter
    return getattr(processor, "span_exporter", None)


def _clear_env(keys):
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


@pytest.fixture
def clean_bsp_env():
    yield from _clear_env(
        [
            "OTEL_BSP_MAX_QUEUE_SIZE",
            "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
            "OTEL_BSP_SCHEDULE_DELAY",
            "OTEL_BSP_EXPORT_TIMEOUT",
        ]
    )


@pytest.fixture
def clean_exporter_env():
    yield from _clear_env(
        ["OTEL_EXPORTER_OTLP_TIMEOUT", "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT"]
    )


class TestBatchSpanProcessorTuning:
    def test_defaults_match_upstream(self, clean_bsp_env):
        """No kwargs, no env -> upstream OTel defaults (unchanged)."""
        p = BatchSpanProcessor(span_exporter=InMemorySpanExporter())
        assert _queue_batch(p) == (2048, 512)

    def test_explicit_kwargs_are_forwarded(self, clean_bsp_env):
        """The core fix: kwargs are now honored instead of dropped."""
        p = BatchSpanProcessor(
            span_exporter=InMemorySpanExporter(),
            max_queue_size=10000,
            max_export_batch_size=1024,
        )
        assert _queue_batch(p) == (10000, 1024)

    def test_schedule_delay_and_export_timeout_forwarded(self, clean_bsp_env):
        p = BatchSpanProcessor(
            span_exporter=InMemorySpanExporter(),
            schedule_delay_millis=1234,
            export_timeout_millis=5678,
        )
        assert _bp_get(p, "schedule_delay_millis") == 1234
        assert _bp_get(p, "export_timeout_millis") == 5678

    def test_env_vars_respected_when_unset(self, clean_bsp_env):
        os.environ["OTEL_BSP_MAX_QUEUE_SIZE"] = "3333"
        p = BatchSpanProcessor(span_exporter=InMemorySpanExporter())
        queue, batch = _queue_batch(p)
        assert queue == 3333
        assert batch == 512  # untouched -> upstream default

    def test_env_var_batch_size_respected_when_unset(self, clean_bsp_env):
        os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"] = "128"
        p = BatchSpanProcessor(span_exporter=InMemorySpanExporter())
        assert _queue_batch(p)[1] == 128

    def test_explicit_kwarg_overrides_env(self, clean_bsp_env):
        os.environ["OTEL_BSP_MAX_QUEUE_SIZE"] = "3333"
        p = BatchSpanProcessor(
            span_exporter=InMemorySpanExporter(), max_queue_size=9000
        )
        assert _queue_batch(p)[0] == 9000


class TestExporterTuning:
    """timeout must reach the default OTLP exporter (both processors)."""

    def test_batch_forwards_timeout(self):
        p = BatchSpanProcessor(timeout=42)
        assert _exporter_of(p)._timeout == 42

    def test_simple_forwards_timeout(self):
        p = SimpleSpanProcessor(timeout=7)
        assert p.span_exporter._timeout == 7

    def test_unset_timeout_uses_upstream_default(self, clean_exporter_env):
        """Backward-compat: no timeout -> upstream default (10s)."""
        p = BatchSpanProcessor(span_exporter=None)
        assert _exporter_of(p)._timeout == 10

    def test_prebuilt_span_exporter_is_used_as_is(self):
        """A caller-supplied exporter is used verbatim; timeout ignored."""
        exp = InMemorySpanExporter()
        p = BatchSpanProcessor(span_exporter=exp, timeout=99)
        assert _exporter_of(p) is exp


class TestRegisterPlumbsTuning:
    """register() must forward the tuning kwargs to the processor / provider."""

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.BatchSpanProcessor")
    def test_register_forwards_tuning(self, mock_bsp, _mock_tp, _mock_check):
        register(
            batch=True,
            verbose=False,
            max_queue_size=7777,
            schedule_delay_millis=1000,
            max_export_batch_size=512,
            export_timeout_millis=15000,
        )
        _, kwargs = mock_bsp.call_args
        assert kwargs["max_queue_size"] == 7777
        assert kwargs["schedule_delay_millis"] == 1000
        assert kwargs["max_export_batch_size"] == 512
        assert kwargs["export_timeout_millis"] == 15000

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.BatchSpanProcessor")
    def test_register_defaults_to_none(self, mock_bsp, _mock_tp, _mock_check):
        """Unset -> None, so upstream env/default behavior is preserved."""
        register(batch=True, verbose=False)
        _, kwargs = mock_bsp.call_args
        assert kwargs["max_queue_size"] is None
        assert kwargs["schedule_delay_millis"] is None
        assert kwargs["max_export_batch_size"] is None
        assert kwargs["export_timeout_millis"] is None

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.BatchSpanProcessor")
    def test_register_forwards_exporter_and_provider_config(
        self, mock_bsp, mock_tp, _mock_check
    ):
        sampler = TraceIdRatioBased(0.5)
        limits = SpanLimits(max_attributes=10)
        exp = InMemorySpanExporter()
        register(
            verbose=False,
            timeout=30,
            sampler=sampler,
            span_limits=limits,
            span_exporter=exp,
        )
        _, bkw = mock_bsp.call_args
        assert bkw["timeout"] == 30
        assert bkw["span_exporter"] is exp

        _, pkw = mock_tp.call_args
        assert pkw["sampler"] is sampler
        assert pkw["span_limits"] is limits

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.BatchSpanProcessor")
    def test_register_omits_provider_kwargs_when_unset(
        self, _mock_bsp, mock_tp, _mock_check
    ):
        """Unset sampler/span_limits are not forwarded -> upstream defaults apply."""
        register(verbose=False)
        _, pkw = mock_tp.call_args
        assert "sampler" not in pkw
        assert "span_limits" not in pkw


class TestRegisterBatchFalse:
    """batch=False -> SimpleSpanProcessor path: exporter tuning wired, batch tuning warned."""

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.SimpleSpanProcessor")
    def test_wires_simple_processor(self, mock_ssp, _mock_tp, _mock_check):
        register(batch=False, verbose=False, timeout=5)
        _, kw = mock_ssp.call_args
        assert kw["timeout"] == 5
        # batch-only tunables are not passed to SimpleSpanProcessor
        assert "max_queue_size" not in kw

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.SimpleSpanProcessor")
    def test_warns_on_batch_only_params(self, _mock_ssp, _mock_tp, _mock_check):
        with pytest.warns(UserWarning, match="batch=False"):
            register(batch=False, verbose=False, max_queue_size=10000)

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.SimpleSpanProcessor")
    def test_no_warning_without_batch_params(self, _mock_ssp, _mock_tp, _mock_check):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            register(batch=False, verbose=False, timeout=5)

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    @patch("fi_instrumentation.otel.TracerProvider")
    @patch("fi_instrumentation.otel.SimpleSpanProcessor")
    def test_forwards_span_exporter(self, mock_ssp, _mock_tp, _mock_check):
        exp = InMemorySpanExporter()
        register(batch=False, verbose=False, span_exporter=exp)
        _, kw = mock_ssp.call_args
        assert kw["span_exporter"] is exp

    def test_warning_fires_across_python_versions(self):
        """Regression: the warning must not rely on locals() inside a comprehension
        (broken on <3.12), so it fires regardless of interpreter."""
        with patch(
            "fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False
        ), patch("fi_instrumentation.otel.TracerProvider"), patch(
            "fi_instrumentation.otel.SimpleSpanProcessor"
        ), pytest.warns(
            UserWarning, match=r"schedule_delay_millis"
        ):
            register(batch=False, verbose=False, schedule_delay_millis=1000)


class TestTracerProviderForwarding:
    def test_accepts_sampler_and_span_limits(self):
        """Direct construction forwards provider kwargs to upstream."""
        sampler = TraceIdRatioBased(0.25)
        tp = TracerProvider(sampler=sampler, span_limits=SpanLimits(max_attributes=3))
        assert tp.sampler is sampler
        assert tp._span_limits.max_attributes == 3


class TestPublicReExports:
    """Concrete samplers are re-exported so `sampler=` is usable without reaching
    into opentelemetry.*"""

    def test_samplers_and_limits_reexported(self):
        import fi_instrumentation as fi

        for name in ("Sampler", "ParentBased", "TraceIdRatioBased", "SpanLimits"):
            assert name in fi.__all__
            assert hasattr(fi, name)
        # constructable + accepted by the provider
        tp = TracerProvider(sampler=fi.ParentBased(fi.TraceIdRatioBased(0.5)), verbose=False)
        assert tp.sampler is not None


class TestEnvPrecedence:
    """schedule_delay / export_timeout also honor their OTEL_BSP_* env vars."""

    def test_schedule_delay_env_respected(self, clean_bsp_env):
        os.environ["OTEL_BSP_SCHEDULE_DELAY"] = "7000"
        p = BatchSpanProcessor(span_exporter=InMemorySpanExporter())
        assert _bp_get(p, "schedule_delay_millis") == 7000

    def test_export_timeout_env_respected(self, clean_bsp_env):
        os.environ["OTEL_BSP_EXPORT_TIMEOUT"] = "4000"
        p = BatchSpanProcessor(span_exporter=InMemorySpanExporter())
        assert _bp_get(p, "export_timeout_millis") == 4000


class TestGrpcTransport:
    def test_grpc_forwards_timeout(self):
        pytest.importorskip("grpc")
        p = BatchSpanProcessor(transport=Transport.GRPC, timeout=11)
        assert _exporter_of(p)._timeout == 11


class TestEndpointRejected:
    """The corrected docstrings: these classes do NOT accept `endpoint`."""

    def test_batch_rejects_endpoint(self):
        with pytest.raises(TypeError):
            BatchSpanProcessor(endpoint="http://x")

    def test_simple_rejects_endpoint(self):
        with pytest.raises(TypeError):
            SimpleSpanProcessor(endpoint="http://x")

    def test_provider_rejects_endpoint(self):
        with pytest.raises(TypeError):
            TracerProvider(endpoint="http://x")


class TestSamplerEffectiveness:
    """sampler is not just stored -> it actually drops/keeps spans."""

    def _emit_and_count(self, sampler) -> int:
        exp = InMemorySpanExporter()
        tp = TracerProvider(sampler=sampler, verbose=False)
        tp.add_span_processor(SimpleSpanProcessor(span_exporter=exp))
        tracer = tp.get_tracer(__name__)
        for i in range(20):
            with tracer.start_as_current_span(f"s{i}"):
                pass
        tp.force_flush()
        return len(exp.get_finished_spans())

    def test_ratio_zero_drops_all(self):
        assert self._emit_and_count(TraceIdRatioBased(0.0)) == 0

    def test_ratio_one_keeps_all(self):
        assert self._emit_and_count(TraceIdRatioBased(1.0)) == 20


class TestEndToEndEmit:
    """Full chain (no mocks): tuned processor actually exports spans."""

    def test_batch_processor_emits(self):
        exp = InMemorySpanExporter()
        tp = TracerProvider(verbose=False)
        tp.add_span_processor(
            BatchSpanProcessor(span_exporter=exp, max_queue_size=100, max_export_batch_size=32)
        )
        tracer = tp.get_tracer(__name__)
        with tracer.start_as_current_span("root"):
            with tracer.start_as_current_span("child"):
                pass
        tp.force_flush()
        assert len(exp.get_finished_spans()) == 2

    @patch("fi_instrumentation.otel.check_custom_eval_config_exists", return_value=False)
    def test_register_with_custom_exporter_does_not_crash(self, _mock_check):
        """Regression: register() called _tracing_details() unconditionally, which
        crashed on exporters lacking _endpoint/_headers (e.g. InMemorySpanExporter)."""
        exp = InMemorySpanExporter()
        tp = register(
            project_type=ProjectType.OBSERVE,
            verbose=False,
            span_exporter=exp,
        )
        tracer = tp.get_tracer(__name__)
        with tracer.start_as_current_span("s"):
            pass
        tp.force_flush()
        assert len(exp.get_finished_spans()) == 1
        tp.shutdown()
