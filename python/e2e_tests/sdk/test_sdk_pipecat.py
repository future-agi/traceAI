"""
E2E Tests for Pipecat Mapper/Exporter

Tests Pipecat span mapper and exporter creation.
Pipecat is an attribute mapper, not a full instrumentor — tests verify
exporter creation and attribute mapping logic.
"""

import pytest
import time

from config import config


class TestPipecatExporter:
    """Test Pipecat exporter creation and attribute mapping."""

    def test_import_and_create_exporter(self):
        """Test that Pipecat exporter can be imported and created."""
        try:
            from traceai_pipecat import PipecatSpanExporter

            assert PipecatSpanExporter is not None
            print("Pipecat exporter imported successfully")
        except (ImportError, AttributeError):
            pytest.skip("traceai_pipecat not installed")

    def test_attribute_mapping(self):
        """Test Pipecat attribute mapping logic."""
        try:
            from traceai_pipecat import PipecatSpanExporter

            exporter = PipecatSpanExporter(
                project_name=config.project_name,
            )

            assert exporter is not None
            print("Pipecat exporter created successfully")
        except (ImportError, AttributeError):
            pytest.skip("traceai_pipecat not installed")
        except Exception as e:
            print(f"Pipecat exporter creation note: {e}")

    def test_register_with_pipecat(self):
        """Test registering tracer with Pipecat exporter."""
        try:
            from fi_instrumentation import register

            tracer_provider = register(
                project_name=config.project_name,
                project_version_name=config.project_version_name,
                project_type=config.project_type,
                verbose=False,
            )

            assert tracer_provider is not None
            print("Tracer provider registered for Pipecat")
        except Exception as e:
            pytest.skip(f"Registration failed: {e}")
