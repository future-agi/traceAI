"""
TraceAI - Simple one-liner SDK initialization for FutureAGI observability.

Example usage:
    import traceai
    traceai.init(project_name="my-app", auto_instrument=["openai", "anthropic"])

    # Now use OpenAI/Anthropic normally - traces are automatically captured
    import openai
    client = openai.OpenAI()
    response = client.chat.completions.create(...)
"""

import importlib
from typing import Any, Dict, List, Optional

from fi_instrumentation import (
    SemanticConvention,
    TracerProvider,
    Transport,
    register,
)
from fi_instrumentation.fi_types import ProjectType

# Registry of available instrumentors
_INSTRUMENTOR_REGISTRY: Dict[str, str] = {
    # LLM provider instrumentors
    "openai": "traceai_openai",
    "anthropic": "traceai_anthropic",
    "litellm": "traceai_litellm",
    "groq": "traceai_groq",
    "mistralai": "traceai_mistralai",
    "mistral": "traceai_mistralai",
    "cohere": "traceai_cohere",
    "bedrock": "traceai_bedrock",
    "vertexai": "traceai_vertexai",
    "vertex": "traceai_vertexai",
    "together": "traceai_together",
    "cerebras": "traceai_cerebras",
    "deepseek": "traceai_deepseek",
    "fireworks": "traceai_fireworks",
    "xai": "traceai_xai",
    "ollama": "traceai_ollama",
    "google_genai": "traceai_google_genai",
    "google-genai": "traceai_google_genai",
    # Framework instrumentors
    "langchain": "traceai_langchain",
    "llamaindex": "traceai_llamaindex",
    "llama_index": "traceai_llamaindex",
    "crewai": "traceai_crewai",
    "crew": "traceai_crewai",
    "autogen": "traceai_autogen",
    "dspy": "traceai_dspy",
    "haystack": "traceai_haystack",
    "instructor": "traceai_instructor",
    "guardrails": "traceai_guardrails",
    "mcp": "traceai_mcp",
}

_tracer_provider: Optional[TracerProvider] = None


def init(
    *,
    project_name: Optional[str] = None,
    project_type: str = "experiment",
    project_version_name: Optional[str] = None,
    auto_instrument: Optional[List[str]] = None,
    semantic_convention: str = "fi",
    transport: str = "http",
    batch: bool = False,
    verbose: bool = True,
    set_global_tracer_provider: bool = True,
) -> TracerProvider:
    """
    Initialize TraceAI with a simple one-liner.

    This function registers the tracer provider and optionally instruments
    specified libraries automatically.

    Args:
        project_name: Name of the project (uses FI_PROJECT_NAME env var if not set)
        project_type: Type of project - "experiment" or "observe"
        project_version_name: Optional version name for the project
        auto_instrument: List of libraries to auto-instrument (e.g., ["openai", "anthropic"])
        semantic_convention: Attribute naming convention - "fi", "otel_genai", "openinference", "openllmetry"
        transport: Transport protocol - "http" or "grpc"
        batch: Use batch span processor instead of simple (better for production)
        verbose: Print configuration details
        set_global_tracer_provider: Set as global OpenTelemetry tracer provider

    Returns:
        TracerProvider: The configured tracer provider

    Example:
        # Basic usage
        import traceai
        traceai.init(project_name="my-app")

        # With auto-instrumentation
        import traceai
        traceai.init(
            project_name="my-app",
            auto_instrument=["openai", "anthropic", "langchain"],
        )

        # Using OpenTelemetry GenAI convention (for compatibility with other tools)
        import traceai
        traceai.init(
            project_name="my-app",
            semantic_convention="otel_genai",
        )

        # Production setup with batching
        import traceai
        traceai.init(
            project_name="my-app",
            project_type="observe",
            batch=True,
            verbose=False,
        )
    """
    global _tracer_provider

    # Convert string enums to enum types
    project_type_enum = (
        ProjectType.OBSERVE if project_type.lower() == "observe" else ProjectType.EXPERIMENT
    )

    semantic_convention_enum = SemanticConvention(semantic_convention.lower())
    transport_enum = Transport(transport.lower())

    # Register the tracer provider
    _tracer_provider = register(
        project_name=project_name,
        project_type=project_type_enum,
        project_version_name=project_version_name,
        batch=batch,
        set_global_tracer_provider=set_global_tracer_provider,
        verbose=verbose,
        transport=transport_enum,
        semantic_convention=semantic_convention_enum,
    )

    # Auto-instrument specified libraries
    if auto_instrument:
        for lib_name in auto_instrument:
            _instrument_library(lib_name, verbose=verbose)

    return _tracer_provider


def _instrument_library(lib_name: str, verbose: bool = True) -> bool:
    """
    Instrument a specific library.

    Args:
        lib_name: Name of the library to instrument
        verbose: Print status messages

    Returns:
        bool: True if instrumentation succeeded
    """
    lib_name_lower = lib_name.lower()

    if lib_name_lower not in _INSTRUMENTOR_REGISTRY:
        if verbose:
            print(f"Warning: Unknown library '{lib_name}'. Available: {list(_INSTRUMENTOR_REGISTRY.keys())}")
        return False

    instrumentor_module = _INSTRUMENTOR_REGISTRY[lib_name_lower]

    try:
        # Import the instrumentor package
        module = importlib.import_module(instrumentor_module)

        # Look for the Instrumentor class
        instrumentor_class = None
        for attr_name in dir(module):
            if attr_name.endswith("Instrumentor") and attr_name != "BaseInstrumentor":
                instrumentor_class = getattr(module, attr_name)
                break

        if instrumentor_class is None:
            if verbose:
                print(f"Warning: Could not find Instrumentor class in {instrumentor_module}")
            return False

        # Create and instrument
        instrumentor = instrumentor_class()
        instrumentor.instrument()

        if verbose:
            print(f"|  Auto-instrumented: {lib_name}")

        return True

    except ImportError as e:
        if verbose:
            print(f"Warning: Could not import {instrumentor_module}. Install it with: pip install {instrumentor_module}")
        return False
    except Exception as e:
        if verbose:
            print(f"Warning: Failed to instrument {lib_name}: {e}")
        return False


def get_tracer_provider() -> Optional[TracerProvider]:
    """Get the current tracer provider, if initialized."""
    return _tracer_provider


def instrument(*libraries: str, verbose: bool = True) -> None:
    """
    Instrument additional libraries after init().

    Args:
        *libraries: Names of libraries to instrument
        verbose: Print status messages

    Example:
        import traceai
        traceai.init(project_name="my-app")
        traceai.instrument("openai", "anthropic")
    """
    for lib_name in libraries:
        _instrument_library(lib_name, verbose=verbose)


__all__ = [
    "init",
    "instrument",
    "get_tracer_provider",
    "SemanticConvention",
    "Transport",
]
