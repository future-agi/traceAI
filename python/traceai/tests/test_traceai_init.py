"""
Tests for traceai.init() wrapper and SDK semantic conventions.

These tests verify:
- SemanticConvention enum values
- traceai.init() parameter handling
- Auto-instrumentation registry
- Instrumentor loading
"""

import unittest
from unittest.mock import patch, MagicMock
import sys


class TestSemanticConventionEnum(unittest.TestCase):
    """Tests for SemanticConvention enum."""

    def test_semantic_convention_values(self):
        """Test that SemanticConvention has expected values."""
        from fi_instrumentation.otel import SemanticConvention

        self.assertEqual(SemanticConvention.FI.value, "fi")
        self.assertEqual(SemanticConvention.OTEL_GENAI.value, "otel_genai")
        self.assertEqual(SemanticConvention.OPENINFERENCE.value, "openinference")
        self.assertEqual(SemanticConvention.OPENLLMETRY.value, "openllmetry")

    def test_semantic_convention_from_string(self):
        """Test creating SemanticConvention from string."""
        from fi_instrumentation.otel import SemanticConvention

        self.assertEqual(SemanticConvention("fi"), SemanticConvention.FI)
        self.assertEqual(SemanticConvention("otel_genai"), SemanticConvention.OTEL_GENAI)
        self.assertEqual(SemanticConvention("openinference"), SemanticConvention.OPENINFERENCE)
        self.assertEqual(SemanticConvention("openllmetry"), SemanticConvention.OPENLLMETRY)

    def test_semantic_convention_is_string_enum(self):
        """Test that SemanticConvention inherits from str."""
        from fi_instrumentation.otel import SemanticConvention

        # Should be usable as string
        self.assertIsInstance(SemanticConvention.FI, str)
        self.assertEqual(f"Convention: {SemanticConvention.FI}", "Convention: fi")


class TestTraceAIInit(unittest.TestCase):
    """Tests for traceai.init() function."""

    @patch("traceai.register")
    def test_init_default_parameters(self, mock_register):
        """Test init() with default parameters."""
        from traceai import init
        from fi_instrumentation import SemanticConvention, Transport
        from fi_instrumentation.fi_types import ProjectType

        mock_provider = MagicMock()
        mock_register.return_value = mock_provider

        result = init(project_name="test-project", verbose=False)

        mock_register.assert_called_once()
        call_kwargs = mock_register.call_args.kwargs

        self.assertEqual(call_kwargs["project_name"], "test-project")
        self.assertEqual(call_kwargs["project_type"], ProjectType.EXPERIMENT)
        self.assertEqual(call_kwargs["semantic_convention"], SemanticConvention.FI)
        self.assertEqual(call_kwargs["transport"], Transport.HTTP)
        self.assertFalse(call_kwargs["batch"])
        self.assertTrue(call_kwargs["set_global_tracer_provider"])
        self.assertEqual(result, mock_provider)

    @patch("traceai.register")
    def test_init_with_otel_genai_convention(self, mock_register):
        """Test init() with OTEL GenAI convention."""
        from traceai import init
        from fi_instrumentation import SemanticConvention

        mock_register.return_value = MagicMock()

        init(
            project_name="test-project",
            semantic_convention="otel_genai",
            verbose=False,
        )

        call_kwargs = mock_register.call_args.kwargs
        self.assertEqual(call_kwargs["semantic_convention"], SemanticConvention.OTEL_GENAI)

    @patch("traceai.register")
    def test_init_with_observe_project_type(self, mock_register):
        """Test init() with observe project type."""
        from traceai import init
        from fi_instrumentation.fi_types import ProjectType

        mock_register.return_value = MagicMock()

        init(
            project_name="test-project",
            project_type="observe",
            verbose=False,
        )

        call_kwargs = mock_register.call_args.kwargs
        self.assertEqual(call_kwargs["project_type"], ProjectType.OBSERVE)

    @patch("traceai.register")
    def test_init_with_grpc_transport(self, mock_register):
        """Test init() with gRPC transport."""
        from traceai import init
        from fi_instrumentation import Transport

        mock_register.return_value = MagicMock()

        init(
            project_name="test-project",
            transport="grpc",
            verbose=False,
        )

        call_kwargs = mock_register.call_args.kwargs
        self.assertEqual(call_kwargs["transport"], Transport.GRPC)

    @patch("traceai.register")
    def test_init_with_batch_mode(self, mock_register):
        """Test init() with batch mode enabled."""
        from traceai import init

        mock_register.return_value = MagicMock()

        init(
            project_name="test-project",
            batch=True,
            verbose=False,
        )

        call_kwargs = mock_register.call_args.kwargs
        self.assertTrue(call_kwargs["batch"])


class TestInstrumentorRegistry(unittest.TestCase):
    """Tests for the instrumentor registry."""

    def test_registry_contains_common_libraries(self):
        """Test that registry contains common libraries."""
        from traceai import _INSTRUMENTOR_REGISTRY

        expected_libraries = [
            "openai",
            "anthropic",
            "langchain",
            "llamaindex",
            "litellm",
            "groq",
            "mistralai",
            "cohere",
            "bedrock",
            "crewai",
            "dspy",
            "haystack",
        ]

        for lib in expected_libraries:
            self.assertIn(lib, _INSTRUMENTOR_REGISTRY)

    def test_registry_maps_to_correct_packages(self):
        """Test that registry maps to correct package names."""
        from traceai import _INSTRUMENTOR_REGISTRY

        self.assertEqual(_INSTRUMENTOR_REGISTRY["openai"], "traceai_openai")
        self.assertEqual(_INSTRUMENTOR_REGISTRY["anthropic"], "traceai_anthropic")
        self.assertEqual(_INSTRUMENTOR_REGISTRY["langchain"], "traceai_langchain")
        self.assertEqual(_INSTRUMENTOR_REGISTRY["llamaindex"], "traceai_llamaindex")

    def test_registry_aliases(self):
        """Test that registry includes common aliases."""
        from traceai import _INSTRUMENTOR_REGISTRY

        # llama_index should map same as llamaindex
        self.assertEqual(
            _INSTRUMENTOR_REGISTRY["llama_index"],
            _INSTRUMENTOR_REGISTRY["llamaindex"],
        )

        # mistral should map same as mistralai
        self.assertEqual(
            _INSTRUMENTOR_REGISTRY["mistral"],
            _INSTRUMENTOR_REGISTRY["mistralai"],
        )


class TestInstrumentLibrary(unittest.TestCase):
    """Tests for _instrument_library function."""

    def test_unknown_library_warning(self):
        """Test that unknown library prints warning."""
        from traceai import _instrument_library

        # Should return False for unknown library
        result = _instrument_library("unknown_library_xyz", verbose=False)
        self.assertFalse(result)

    @patch("traceai.importlib.import_module")
    def test_missing_package_warning(self, mock_import):
        """Test that missing package returns False."""
        from traceai import _instrument_library

        mock_import.side_effect = ImportError("No module named 'traceai_openai'")

        result = _instrument_library("openai", verbose=False)
        self.assertFalse(result)

    @patch("traceai.importlib.import_module")
    def test_successful_instrumentation(self, mock_import):
        """Test successful library instrumentation."""
        from traceai import _instrument_library

        # Mock the module with an Instrumentor class
        mock_module = MagicMock()
        mock_instrumentor_class = MagicMock()
        mock_instrumentor_instance = MagicMock()
        mock_instrumentor_class.return_value = mock_instrumentor_instance
        mock_module.OpenAIInstrumentor = mock_instrumentor_class

        mock_import.return_value = mock_module

        result = _instrument_library("openai", verbose=False)

        self.assertTrue(result)
        mock_instrumentor_class.assert_called_once()
        mock_instrumentor_instance.instrument.assert_called_once()


class TestInstrumentFunction(unittest.TestCase):
    """Tests for traceai.instrument() function."""

    @patch("traceai._instrument_library")
    def test_instrument_multiple_libraries(self, mock_instrument):
        """Test instrumenting multiple libraries."""
        from traceai import instrument

        mock_instrument.return_value = True

        instrument("openai", "anthropic", "langchain", verbose=False)

        self.assertEqual(mock_instrument.call_count, 3)
        mock_instrument.assert_any_call("openai", verbose=False)
        mock_instrument.assert_any_call("anthropic", verbose=False)
        mock_instrument.assert_any_call("langchain", verbose=False)


class TestGetTracerProvider(unittest.TestCase):
    """Tests for get_tracer_provider function."""

    def test_get_tracer_provider_before_init(self):
        """Test get_tracer_provider returns None before init."""
        import traceai

        # Reset the module state
        traceai._tracer_provider = None

        result = traceai.get_tracer_provider()
        self.assertIsNone(result)

    @patch("traceai.register")
    def test_get_tracer_provider_after_init(self, mock_register):
        """Test get_tracer_provider returns provider after init."""
        import traceai

        mock_provider = MagicMock()
        mock_register.return_value = mock_provider

        traceai.init(project_name="test", verbose=False)

        result = traceai.get_tracer_provider()
        self.assertEqual(result, mock_provider)


class TestModuleExports(unittest.TestCase):
    """Tests for module exports."""

    def test_traceai_exports(self):
        """Test that traceai exports expected symbols."""
        import traceai

        self.assertTrue(hasattr(traceai, "init"))
        self.assertTrue(hasattr(traceai, "instrument"))
        self.assertTrue(hasattr(traceai, "get_tracer_provider"))
        self.assertTrue(hasattr(traceai, "SemanticConvention"))
        self.assertTrue(hasattr(traceai, "Transport"))

    def test_fi_instrumentation_exports(self):
        """Test that fi_instrumentation exports SemanticConvention."""
        from fi_instrumentation import SemanticConvention, SEMANTIC_CONVENTION

        self.assertIsNotNone(SemanticConvention)
        self.assertEqual(SEMANTIC_CONVENTION, "semantic_convention")


if __name__ == "__main__":
    unittest.main()
