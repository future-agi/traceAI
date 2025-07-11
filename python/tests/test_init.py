"""Tests for fi_instrumentation.__init__ module (public API)."""

import pytest
from unittest.mock import patch, MagicMock

# Test that all expected symbols are properly exported
def test_public_api_exports():
    """Test that all expected symbols are available in the public API."""
    import fi_instrumentation
    
    # Context and attributes
    assert hasattr(fi_instrumentation, 'get_attributes_from_context')
    assert hasattr(fi_instrumentation, 'using_attributes')
    assert hasattr(fi_instrumentation, 'using_metadata')
    assert hasattr(fi_instrumentation, 'using_prompt_template')
    assert hasattr(fi_instrumentation, 'using_session')
    assert hasattr(fi_instrumentation, 'using_tags')
    assert hasattr(fi_instrumentation, 'using_user')
    
    # Helpers and config
    assert hasattr(fi_instrumentation, 'safe_json_dumps')
    assert hasattr(fi_instrumentation, 'suppress_tracing')
    assert hasattr(fi_instrumentation, 'TraceConfig')
    assert hasattr(fi_instrumentation, 'FITracer')
    assert hasattr(fi_instrumentation, 'REDACTED_VALUE')
    
    # OpenTelemetry components
    assert hasattr(fi_instrumentation, 'TracerProvider')
    assert hasattr(fi_instrumentation, 'SimpleSpanProcessor')
    assert hasattr(fi_instrumentation, 'BatchSpanProcessor')
    assert hasattr(fi_instrumentation, 'HTTPSpanExporter')
    assert hasattr(fi_instrumentation, 'Resource')
    assert hasattr(fi_instrumentation, 'PROJECT_NAME')
    assert hasattr(fi_instrumentation, 'PROJECT_TYPE')
    assert hasattr(fi_instrumentation, 'PROJECT_VERSION_NAME')
    assert hasattr(fi_instrumentation, 'Transport')
    assert hasattr(fi_instrumentation, 'register')


def test_context_managers_import():
    """Test that context managers can be imported and used."""
    from fi_instrumentation import using_session, using_user, using_metadata
    
    # Test that they can be instantiated
    session_mgr = using_session("test-session")
    user_mgr = using_user("test-user")
    metadata_mgr = using_metadata({"key": "value"})
    
    assert session_mgr is not None
    assert user_mgr is not None
    assert metadata_mgr is not None


def test_tracer_import():
    """Test that FITracer can be imported and instantiated."""
    from fi_instrumentation import FITracer, TraceConfig
    from unittest.mock import MagicMock
    from opentelemetry.trace import Tracer
    
    mock_tracer = MagicMock(spec=Tracer)
    config = TraceConfig()
    fi_tracer = FITracer(mock_tracer, config)
    
    assert fi_tracer is not None
    assert hasattr(fi_tracer, 'agent')
    assert hasattr(fi_tracer, 'chain')
    assert hasattr(fi_tracer, 'tool')


def test_otel_components_import():
    """Test that OpenTelemetry components can be imported."""
    from fi_instrumentation import (
        TracerProvider,
        SimpleSpanProcessor,
        BatchSpanProcessor,
        HTTPSpanExporter,
        Transport,
        register
    )
    
    # Test that classes/functions are available
    assert TracerProvider is not None
    assert SimpleSpanProcessor is not None
    assert BatchSpanProcessor is not None
    assert HTTPSpanExporter is not None
    assert Transport is not None
    assert register is not None


def test_constants_import():
    """Test that constants can be imported."""
    from fi_instrumentation import (
        PROJECT_NAME,
        PROJECT_TYPE,
        PROJECT_VERSION_NAME,
        REDACTED_VALUE
    )
    
    assert PROJECT_NAME == "project_name"
    assert PROJECT_TYPE == "project_type"
    assert PROJECT_VERSION_NAME == "project_version_name"
    assert REDACTED_VALUE == "__REDACTED__"


def test_helper_functions_import():
    """Test that helper functions can be imported and used."""
    from fi_instrumentation import safe_json_dumps, get_attributes_from_context
    
    # Test safe_json_dumps
    test_data = {"key": "value", "number": 123}
    json_str = safe_json_dumps(test_data)
    assert isinstance(json_str, str)
    assert "key" in json_str
    assert "value" in json_str
    
    # Test get_attributes_from_context
    attributes = list(get_attributes_from_context())
    assert isinstance(attributes, list)


def test_config_import():
    """Test that configuration classes can be imported and used."""
    from fi_instrumentation import TraceConfig, suppress_tracing
    
    # Test TraceConfig
    config = TraceConfig()
    assert config is not None
    assert hasattr(config, 'hide_inputs')
    assert hasattr(config, 'hide_outputs')
    
    # Test suppress_tracing
    assert suppress_tracing is not None
    # Test that it can be used as context manager
    with suppress_tracing():
        pass  # Should work without error


@patch('fi_instrumentation.register')  # Mock register directly from the module
def test_register_function_integration(mock_register):
    """Test that register function is accessible and can be called."""
    from fi_instrumentation import register, Transport
    from fi_instrumentation.fi_types import ProjectType
    
    # Mock the register function to avoid actual registration
    mock_tracer_provider = MagicMock()
    mock_register.return_value = mock_tracer_provider
    
    # Test that register can be called with various parameters
    result = register(
        project_name="test_project",
        project_type=ProjectType.EXPERIMENT,
        transport=Transport.HTTP
    )
    
    mock_register.assert_called_once()
    assert result == mock_tracer_provider


def test_resource_import():
    """Test that OpenTelemetry Resource can be imported."""
    from fi_instrumentation import Resource
    
    # Test that Resource class is available
    assert Resource is not None
    
    # Test that it can be instantiated
    resource = Resource(attributes={"service.name": "test-service"})
    assert resource is not None


def test_all_exports_in___all__():
    """Test that __all__ contains all expected exports."""
    import fi_instrumentation
    
    expected_exports = {
        # Context and attributes
        "get_attributes_from_context",
        "using_attributes",
        "using_metadata", 
        "using_prompt_template",
        "using_session",
        "using_tags",
        "using_user",
        # Helpers and config
        "safe_json_dumps",
        "suppress_tracing",
        "TraceConfig",
        "FITracer",
        "REDACTED_VALUE",
        # OpenTelemetry components
        "TracerProvider",
        "SimpleSpanProcessor",
        "BatchSpanProcessor",
        "HTTPSpanExporter",
        "Resource",
        "PROJECT_NAME",
        "PROJECT_TYPE",
        "PROJECT_VERSION_NAME",
        "Transport",
        "register",
    }
    
    # Check that __all__ is defined and contains expected exports
    assert hasattr(fi_instrumentation, '__all__')
    actual_exports = set(fi_instrumentation.__all__)
    
    # All expected exports should be in __all__
    missing_exports = expected_exports - actual_exports
    assert not missing_exports, f"Missing exports in __all__: {missing_exports}"
    
    # All exports in __all__ should be actually available
    for export in fi_instrumentation.__all__:
        assert hasattr(fi_instrumentation, export), f"Export '{export}' in __all__ but not available"


def test_import_star():
    """Test that 'from fi_instrumentation import *' works correctly."""
    # This test verifies that star imports don't cause issues
    import importlib
    import sys
    
    # Create a new module to test star import
    test_module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("test_star_import", loader=None)
    )
    
    # Execute star import in the test module
    exec("from fi_instrumentation import *", test_module.__dict__)
    
    # Verify that expected symbols are available
    assert hasattr(test_module, 'register')
    assert hasattr(test_module, 'FITracer')
    assert hasattr(test_module, 'using_session')
    assert hasattr(test_module, 'TracerProvider')


def test_version_info():
    """Test that version information is accessible if defined."""
    import fi_instrumentation
    
    # Check if version info is available (it might not be in development)
    if hasattr(fi_instrumentation, '__version__'):
        assert isinstance(fi_instrumentation.__version__, str)
        assert len(fi_instrumentation.__version__) > 0


def test_circular_imports():
    """Test that there are no circular import issues."""
    # This test simply tries to import all main components to ensure
    # no circular import errors occur
    try:
        from fi_instrumentation import (
            FITracer,
            TraceConfig,
            register,
            TracerProvider,
            using_session,
            get_attributes_from_context,
            safe_json_dumps,
            suppress_tracing,
        )
        # If we get here without ImportError, circular imports are OK
        assert True
    except ImportError as e:
        pytest.fail(f"Circular import detected: {e}")


def test_lazy_imports():
    """Test that imports don't cause expensive operations at import time."""
    import time
    import importlib
    import sys
    
    # Remove the module if already imported
    if 'fi_instrumentation' in sys.modules:
        del sys.modules['fi_instrumentation']
    
    # Time the import
    start_time = time.time()
    import fi_instrumentation
    import_time = time.time() - start_time
    
    # Import should be reasonably fast (less than 1 second)
    assert import_time < 1.0, f"Import took too long: {import_time} seconds" 