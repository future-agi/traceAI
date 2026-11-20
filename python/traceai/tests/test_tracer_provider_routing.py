"""T1-PY-CORE-001: automatic instrumentors must use TraceAI's provider."""

import sys
import unittest
from types import ModuleType
from unittest.mock import MagicMock, patch


class TestTracerProviderRouting(unittest.TestCase):
    def _assert_provider_routing(self, *, instrument_after_init, set_global=False):
        import traceai
        from opentelemetry import trace as trace_api

        global_providers = (
            ("fresh", trace_api.ProxyTracerProvider()),
            ("existing", MagicMock(spec=trace_api.TracerProvider)),
        )
        for label, global_provider in global_providers:
            with self.subTest(global_provider=label):
                configured_provider = MagicMock(spec=trace_api.TracerProvider)
                selected_providers = []

                # Model OpenAIInstrumentor's explicit-provider/global fallback
                # without importing OpenAI, wrapping requests, or exporting spans.
                def instrument(**kwargs):
                    provider = kwargs.get("tracer_provider")
                    if provider is None:
                        provider = trace_api.get_tracer_provider()
                    selected_providers.append(provider)

                instrumentor = MagicMock()
                instrumentor.instrument.side_effect = instrument
                module = ModuleType("traceai_openai")
                module.OpenAIInstrumentor = MagicMock(return_value=instrumentor)

                with (
                    patch.object(traceai, "_tracer_provider", None),
                    patch.object(
                        traceai, "register", return_value=configured_provider
                    ) as register,
                    patch.dict(sys.modules, {"traceai_openai": module}),
                    patch.object(
                        trace_api, "get_tracer_provider", return_value=global_provider
                    ) as get_global,
                ):
                    result = traceai.init(
                        project_name="provider-routing-test",
                        project_type="observe",
                        auto_instrument=None if instrument_after_init else ["openai"],
                        set_global_tracer_provider=set_global,
                        verbose=False,
                    )
                    if instrument_after_init:
                        instrumentor.instrument.assert_not_called()
                        traceai.instrument("openai", verbose=False)

                    self.assertIs(result, configured_provider)
                    self.assertIs(traceai.get_tracer_provider(), configured_provider)
                    self.assertIs(
                        register.call_args.kwargs["set_global_tracer_provider"],
                        set_global,
                    )
                    instrumentor.instrument.assert_called_once_with(
                        tracer_provider=configured_provider
                    )
                    self.assertIs(selected_providers[0], configured_provider)
                    get_global.assert_not_called()

    def test_init_routes_to_non_global_provider(self):
        self._assert_provider_routing(instrument_after_init=False)

    def test_later_instrument_routes_to_non_global_provider(self):
        self._assert_provider_routing(instrument_after_init=True)

    def test_init_passes_provider_when_global_registration_is_enabled(self):
        self._assert_provider_routing(instrument_after_init=False, set_global=True)
