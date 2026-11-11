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
import threading
from typing import Any, Dict, List, Optional

from fi_instrumentation import (
    SemanticConvention,
    TracerProvider,
    Transport,
    register,
)
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.attachments import AttachmentConfig
from fi_instrumentation.instrumentation.config import TraceConfig

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
    "pipecat": "traceai_pipecat",
    "livekit": "traceai_livekit",
}

# Explicit entry points: never choose the first imported *Instrumentor class.
_ENTRY_POINTS = {
    "traceai_openai": "OpenAIInstrumentor", "traceai_anthropic": "AnthropicInstrumentor",
    "traceai_litellm": "LiteLLMInstrumentor", "traceai_groq": "GroqInstrumentor",
    "traceai_mistralai": "MistralAIInstrumentor", "traceai_cohere": "CohereInstrumentor",
    "traceai_bedrock": "BedrockInstrumentor", "traceai_vertexai": "VertexAIInstrumentor",
    "traceai_together": "TogetherInstrumentor", "traceai_cerebras": "CerebrasInstrumentor",
    "traceai_deepseek": "DeepSeekInstrumentor", "traceai_fireworks": "FireworksInstrumentor",
    "traceai_xai": "XAIInstrumentor", "traceai_ollama": "OllamaInstrumentor",
    "traceai_google_genai": "GoogleGenAIInstrumentor", "traceai_langchain": "LangChainInstrumentor",
    "traceai_llamaindex": "LlamaIndexInstrumentor", "traceai_crewai": "CrewAIInstrumentor",
    "traceai_autogen": "AutogenInstrumentor", "traceai_dspy": "DSPyInstrumentor",
    "traceai_haystack": "HaystackInstrumentor", "traceai_instructor": "InstructorInstrumentor",
    "traceai_guardrails": "GuardrailsInstrumentor", "traceai_mcp": "MCPInstrumentor",
}

_tracer_provider: Optional[TracerProvider] = None
_init_options = None
_init_lock = threading.RLock()
_instrumented = set()
_adapter_status = {}


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
    attachment_config: Optional[AttachmentConfig] = None,
    trace_config: Optional[TraceConfig] = None,
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
    global _tracer_provider, _init_options

    # Convert string enums to enum types
    project_type_enum = (
        ProjectType.OBSERVE if project_type.lower() == "observe" else ProjectType.EXPERIMENT
    )

    semantic_convention_enum = SemanticConvention(semantic_convention.lower())
    transport_enum = Transport(transport.lower())

    options = (project_name, project_type, project_version_name, semantic_convention,
               transport, batch, set_global_tracer_provider, attachment_config, trace_config)
    with _init_lock:
        if _tracer_provider is not None and _init_options != options:
            raise ValueError("traceai is already initialized with different configuration")
        if _tracer_provider is not None and _init_options == options:
            provider = _tracer_provider
        else:
            provider = register(
                project_name=project_name,
                project_type=project_type_enum,
                project_version_name=project_version_name,
                batch=batch,
                set_global_tracer_provider=set_global_tracer_provider,
                verbose=verbose,
                transport=transport_enum,
                semantic_convention=semantic_convention_enum,
                attachment_config=attachment_config,
                trace_config=trace_config,
            )
            _tracer_provider = provider
            _init_options = options
            _instrumented.clear()
            _adapter_status.clear()

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

        if lib_name_lower in ("pipecat", "livekit"):
            # These use native/session integrations, not an Instrumentor. Auto
            # cannot insert a pipeline processor or guess a live job/session.
            _adapter_status[lib_name_lower] = "registered"
            return True
        with _init_lock:
            if instrumentor_module in _instrumented:
                return True
            instrumentor_class = getattr(module, _ENTRY_POINTS[instrumentor_module])
            instrumentor = instrumentor_class()
            provider = _tracer_provider
            manager = getattr(provider, "attachment_manager", None)
            instrumentor.instrument(
                tracer_provider=provider,
                config=getattr(provider, "trace_config", None),
                attachment_manager=manager,
                attachment_policy=getattr(manager, "policy", None),
            )
            _instrumented.add(instrumentor_module)
            _adapter_status[lib_name_lower] = "registered"

        if verbose:
            print(f"|  Auto-instrumented: {lib_name}")

        return True

    except ImportError:
        _adapter_status[lib_name_lower] = "dependency_unavailable"
        if verbose:
            print(f"Warning: Could not import {instrumentor_module}. Install it with: pip install {instrumentor_module}")
        return False
    except Exception:
        _adapter_status[lib_name_lower] = "unsupported_version"
        if verbose:
            print(f"Warning: Failed to instrument {lib_name}")
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


def attachment_status() -> dict:
    """Content-free setup status, never an integrated recording-ready claim."""
    manager = getattr(_tracer_provider, "attachment_manager", None)
    return {"runtime": manager.status().state if manager is not None else "uninitialized",
            "adapters": dict(_adapter_status)}


def _bind(library: str, entry_point: str, *args, **kwargs):
    manager = getattr(_tracer_provider, "attachment_manager", None)
    if manager is None:
        raise RuntimeError("initialize traceai before binding a session")
    try:
        module = importlib.import_module(_INSTRUMENTOR_REGISTRY[library])
        binder = getattr(module, entry_point)
    except (ImportError, AttributeError):
        _adapter_status[library] = "unsupported_version"
        return None
    try:
        binding = binder(*args, manager=manager, **kwargs)
    except Exception:
        _adapter_status[library] = "source_unavailable"
        return None
    # A binding alone is not proof of recorded media. The adapter owns reporting
    # when a real source becomes available; Q02 qualifies end-to-end playback.
    _adapter_status[library] = "session_bound" if binding is not None else "source_unavailable"
    return binding


def bind_pipecat_audio_processor(processor, session_context):
    """Observe an existing processor; never start a new recording implicitly."""
    return _bind("pipecat", "bind_audio_processor", processor, session_context)


def bind_livekit_session(session, job_context, *, owned_recording_files=False,
                         finalization_timeout=30):
    """Bind the actual LiveKit session; local-file access is separately opt-in."""
    return _bind("livekit", "bind_session", session, job_context,
                 owned_recording_files=owned_recording_files,
                 finalization_timeout=finalization_timeout)


__all__ = [
    "init",
    "AttachmentConfig",
    "attachment_status",
    "bind_pipecat_audio_processor",
    "bind_livekit_session",
    "instrument",
    "get_tracer_provider",
    "SemanticConvention",
    "Transport",
]
