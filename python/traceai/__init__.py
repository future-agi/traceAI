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

import dataclasses
import importlib
import importlib.util
import threading
from typing import Any, Dict, List, Optional

from fi_instrumentation import (
    SemanticConvention,
    TracerProvider,
    Transport,
    register,
)
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.instrumentation.config import TraceConfig

# traceai's floor still admits fi-instrumentation-otel releases without the
# attachments package (it can move only once a core release ships it). On such a
# core, importing traceai and plain init() keep working; asking for attachments
# raises a clear error instead.
try:
    from fi_instrumentation.attachments import AttachmentConfig, DisabledAttachmentManager
    from fi_instrumentation.attachments.policy import env_disabled as _env_disabled

    _ATTACHMENTS_SUPPORTED = True
except ImportError:  # pragma: no cover - exercised in a subprocess test
    AttachmentConfig = None
    DisabledAttachmentManager = None
    _ATTACHMENTS_SUPPORTED = False

    def _env_disabled() -> bool:
        return True

# Context keys read by the shared provider-media helper (and any adapter) BY NAME, so
# setting them never needs the attachments package. They match
# fi_instrumentation.attachments.media_capture.CAPTURE_POLICY_KEY / OWNER_KEY and the
# TypeScript `traceai_auto` keys (`createContextKey` names) exactly.
_CAPTURE_POLICY_KEY = "traceai.auto.attachment_policy.v1"
_OWNER_KEY = "traceai.auto.attachment_owner.v1"

_CORE_TOO_OLD = ("attachment_config, attachment_manager and trace_config need a fi-instrumentation-otel "
                 "release that includes fi_instrumentation.attachments; upgrade fi-instrumentation-otel")

# Instrumentors that capture media through the shared manager they receive as
# ``attachments``: (module, descriptor). A descriptor is only present in an installed
# instrumentor version that has the adapter, so the report is version aware.
_MEDIA_ADAPTERS = {
    "traceai_openai": ("traceai_openai", "openai_attachment_capabilities"),
    "traceai_anthropic": ("traceai_anthropic", "anthropic_attachment_capabilities"),
    "traceai_groq": ("traceai_groq._audio", "groq_attachment_capabilities"),
    # MCP and agent tool media. MCP and mcp-use capture only through their explicit entry
    # points (``wrap_mcp_client`` and ``mcp_use_callback`` below): the MCP instrumentor only
    # propagates context, and mcp-use has a callback, not an instrumentor.
    "traceai_mcp": ("traceai_mcp", "mcp_attachment_capabilities"),
    "traceai_mcp_use": ("traceai_mcp_use._attachments", "mcp_use_attachment_capabilities"),
    "traceai_openai_agents": ("traceai_openai_agents._attachments", "openai_agents_attachment_capabilities"),
    "traceai_claude_agent_sdk": ("traceai_claude_agent_sdk._attachments", "claude_agent_attachment_capabilities"),
    "traceai_smolagents": ("traceai_smolagents._attachments", "smolagents_attachment_capabilities"),
    "traceai_firecrawl": ("traceai_firecrawl._attachments", "firecrawl_attachment_capabilities"),
    # Provider media (shared provider_media walkers). Each package resolves its descriptor lazily from
    # fi_instrumentation.attachments.provider_media, so an installed version without the binding, or a core
    # without provider_media, reports unqualified. together, deepseek and cerebras claim no media operation
    # and are deliberately absent (their rows stay unqualified).
    **{module: (module, module[len("traceai_"):] + "_attachment_capabilities") for module in (
        "traceai_google_genai", "traceai_vertexai", "traceai_bedrock", "traceai_litellm", "traceai_fireworks",
        "traceai_xai", "traceai_mistralai", "traceai_openrouter", "traceai_portkey", "traceai_ollama",
        "traceai_vllm", "traceai_minimax")},
    # Framework media.
    "traceai_langchain": ("traceai_langchain._attachments", "langchain_attachment_capabilities"),
    "traceai_llamaindex": ("traceai_llamaindex._attachments", "llamaindex_attachment_capabilities"),
    "traceai_agno": ("traceai_agno._attachments", "agno_attachment_capabilities"),
    "traceai_pydantic_ai": ("traceai_pydantic_ai._attachments", "pydantic_ai_attachment_capabilities"),
}
# Voice integrations capture only through an explicit session binding (bind_* below).
_SESSION_BINDINGS = {"traceai_pipecat": "bind_audio_processor", "traceai_livekit": "bind_session"}
# Integrations with no Instrumentor: enabling one only records it, and the application
# connects it explicitly (a pipeline processor, a live session, a LangChain callback).
_EXPLICIT_ONLY = frozenset(("traceai_pipecat", "traceai_livekit", "traceai_mcp_use"))

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
    "openrouter": "traceai_openrouter",
    "portkey": "traceai_portkey",
    "vllm": "traceai_vllm",
    "minimax": "traceai_minimax",
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
    "mcp_use": "traceai_mcp_use",
    "mcp-use": "traceai_mcp_use",
    "agno": "traceai_agno",
    "pydantic_ai": "traceai_pydantic_ai",
    "pydantic-ai": "traceai_pydantic_ai",
    # Agent frameworks and tools
    "openai_agents": "traceai_openai_agents",
    "openai-agents": "traceai_openai_agents",
    "claude_agent_sdk": "traceai_claude_agent_sdk",
    "claude-agent-sdk": "traceai_claude_agent_sdk",
    "smolagents": "traceai_smolagents",
    "firecrawl": "traceai_firecrawl",
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
    "traceai_openai_agents": "OpenAIAgentsInstrumentor", "traceai_claude_agent_sdk": "ClaudeAgentInstrumentor",
    "traceai_smolagents": "SmolagentsInstrumentor", "traceai_firecrawl": "FirecrawlInstrumentor",
    "traceai_openrouter": "OpenRouterInstrumentor", "traceai_portkey": "PortkeyInstrumentor",
    "traceai_vllm": "VLLMInstrumentor", "traceai_minimax": "MiniMaxInstrumentor",
    "traceai_pydantic_ai": "PydanticAIInstrumentor",
    # A process-wide wrapper over OpenInference Agno, with its own keyword arguments (_agno_kwargs).
    "traceai_agno": "AgnoInstrumentorWrapper",
}

_tracer_provider: Optional[TracerProvider] = None
_init_options = None
_init_lock = threading.RLock()
_instrumented = set()
_adapter_status = {}
# Whether the application lent the provider's attachment manager (auto never shuts a
# borrowed manager down).
_attachment_manager_borrowed = False


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
    attachment_config: Optional["AttachmentConfig"] = None,
    trace_config: Optional[TraceConfig] = None,
    attachment_manager: Any = None,
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
        attachment_config: Attachment options for the manager the provider creates.
            Attachments are OFF unless enabled here or with FI_ATTACHMENTS_ENABLED=true.
        trace_config: Hide and PII flags; they also restrict attachment capture.
        attachment_manager: Borrow the application's AttachmentManager instead. Auto
            never shuts it down and only adds trace_config's hide/PII restrictions to it.
            Cannot be combined with attachment_config.

    Every instrumentor auto enables receives the same manager (``attachments=``);
    ``get_attachment_manager()`` returns it and ``report()`` describes it.

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
    global _tracer_provider, _init_options, _attachment_manager_borrowed

    # Convert string enums to enum types
    project_type_enum = (
        ProjectType.OBSERVE if project_type.lower() == "observe" else ProjectType.EXPERIMENT
    )

    semantic_convention_enum = SemanticConvention(semantic_convention.lower())
    transport_enum = Transport(transport.lower())

    if not _ATTACHMENTS_SUPPORTED and (attachment_config is not None or trace_config is not None
                                       or attachment_manager is not None):
        raise RuntimeError(_CORE_TOO_OLD)
    if attachment_manager is not None and attachment_config is not None:
        raise ValueError("pass either attachment_config or attachment_manager, not both")
    # An older core's register() accepts neither keyword: pass them only when supported.
    attachment_kwargs = (dict(attachment_config=attachment_config, trace_config=trace_config)
                         if _ATTACHMENTS_SUPPORTED else {})
    if attachment_manager is not None:
        # The provider must not create a second manager (FI_ATTACHMENTS_ENABLED=true
        # would otherwise start one with its own threads).
        attachment_kwargs["attachment_config"] = AttachmentConfig(enabled=False)
    options = (project_name, project_type, project_version_name, semantic_convention,
               transport, batch, set_global_tracer_provider, attachment_config, trace_config,
               id(attachment_manager) if attachment_manager is not None else None)
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
                **attachment_kwargs,
            )
            if attachment_manager is not None:
                _restrict_borrowed(attachment_manager, getattr(provider, "trace_config", None))
                # The provider holds the ONE manager, so get_attachment_manager(), the
                # instrumentors and the bind_* helpers all use the borrowed one.
                provider.attachment_manager = attachment_manager
            _tracer_provider = provider
            _init_options = options
            _attachment_manager_borrowed = attachment_manager is not None
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

        if instrumentor_module in _EXPLICIT_ONLY:
            # These use native/session/callback integrations, not an Instrumentor. Auto
            # cannot insert a pipeline processor, guess a live job/session or attach a
            # LangChain callback (``mcp_use_callback()`` builds one on the shared manager).
            _adapter_status[lib_name_lower] = "registered"
            return True
        with _init_lock:
            if instrumentor_module in _instrumented:
                return True
            instrumentor_class = getattr(module, _ENTRY_POINTS[instrumentor_module])
            instrumentor = instrumentor_class()
            provider = _tracer_provider
            kwargs = dict(tracer_provider=provider, config=getattr(provider, "trace_config", None))
            if instrumentor_module == "traceai_agno":
                kwargs = _agno_kwargs(provider)
            elif _ATTACHMENTS_SUPPORTED:
                manager = get_attachment_manager()
                if _capturing(manager):
                    # The ONE shared manager, as ``attachments`` (``attachment_manager`` is the
                    # alias the Python instrumentors also accept).
                    kwargs.update(attachments=manager, attachment_manager=manager)
                else:
                    # Attachments OFF: exactly the keyword arguments auto passed before.
                    kwargs.update(attachment_manager=manager, attachment_policy=getattr(manager, "policy", None))
            instrumentor.instrument(**kwargs)
            if getattr(instrumentor, "is_instrumented_by_opentelemetry", True) is False:
                # BaseInstrumentor returns without instrumenting on a dependency conflict (for
                # example pydantic-ai-slim for a "pydantic-ai" requirement): never report it registered.
                _adapter_status[lib_name_lower] = "dependency_unavailable"
                if verbose:
                    print(f"Warning: {lib_name} was not instrumented (dependency check failed)")
                return False
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


def _agno_kwargs(provider) -> Dict[str, Any]:
    """Keyword arguments for ``AgnoInstrumentorWrapper.instrument``.

    It takes OpenInference's TraceConfig (not fi's) and no ``attachment_*`` alias. Auto's
    hide flags are carried over, and only when one is set, so an unrestricted setup keeps
    Agno's own default and environment policy. ``attachments`` is passed only while capturing.
    """
    kwargs: Dict[str, Any] = {"tracer_provider": provider}
    trace_config = getattr(provider, "trace_config", None)
    hidden = {field.name: True for field in dataclasses.fields(TraceConfig)
              if field.name.startswith("hide_") and getattr(trace_config, field.name, False) is True}
    if hidden:
        from openinference.instrumentation import TraceConfig as OpenInferenceTraceConfig

        names = {field.name for field in dataclasses.fields(OpenInferenceTraceConfig)}
        kwargs["config"] = OpenInferenceTraceConfig(**{name: True for name in hidden if name in names})
    if _ATTACHMENTS_SUPPORTED:
        manager = get_attachment_manager()
        if _capturing(manager):
            kwargs["attachments"] = manager
    return kwargs


def get_tracer_provider() -> Optional[TracerProvider]:
    """Get the current tracer provider, if initialized."""
    return _tracer_provider


def get_attachment_manager():
    """The one attachment manager auto passes to instrumentors, or None before init().

    It is the borrowed ``attachment_manager``, else the one the provider created
    (the disabled null object while attachments are OFF). Call ``ready()`` on it
    to wait for the attachment session at startup.
    """
    return getattr(_tracer_provider, "attachment_manager", None)


def _capturing(manager) -> bool:
    """A manager that can admit media now: enabled, and not killed by FI_ATTACHMENTS_ENABLED."""
    return (manager is not None and DisabledAttachmentManager is not None
            and not isinstance(manager, DisabledAttachmentManager)
            and callable(getattr(manager, "preflight", None)) and not _env_disabled())


_RESTRICTIONS = ("hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages",
                 "hide_input_images", "hide_input_text", "hide_output_text", "pii_redaction")


def _restrict_borrowed(manager, trace_config) -> None:
    """Add auto's hide/PII flags to a borrowed manager's policy. Never removes one."""
    policy = getattr(manager, "policy", None)
    current = getattr(policy, "trace_config", None)
    if policy is None or current is None or trace_config is None:
        return
    added = {name: True for name in _RESTRICTIONS
             if getattr(trace_config, name, False) and not getattr(current, name, False)}
    if getattr(trace_config, "hide_error_details", False) and not getattr(current, "hide_error_details", False):
        added["hide_error_details"] = True
    if added:
        # TraceConfig is frozen: replace it whole; every flag already set stays set.
        policy.trace_config = dataclasses.replace(current, **added)


def with_attachment_capture_policy(policy, context=None):
    """Return a context whose calls capture media under ``policy`` as well.

    ``policy`` is a ``fi_instrumentation.attachments.CapturePolicy``. It can only
    restrict: it is intersected with any policy already in the context and with every
    other layer (server, credential, hide/PII flags, modality). Anything that is not a
    ``CapturePolicy`` denies capture. Attach the result with
    ``opentelemetry.context.attach`` around the calls it covers.
    """
    from opentelemetry import context as otel_context

    try:
        from fi_instrumentation.attachments.media_capture import merge_capture_policies
    except ImportError:
        merged = policy
    else:
        merged = merge_capture_policies(otel_context.get_value(_CAPTURE_POLICY_KEY, context), policy)
    return otel_context.set_value(_CAPTURE_POLICY_KEY, merged, context)


def with_attachment_ownership(context=None):
    """Return a context marking its calls' media as captured by your own adapter.

    Instrumentors called inside it capture no media for those calls, so the same
    media is not uploaded twice. Mark only the downstream call, not your own capture.
    """
    from opentelemetry import context as otel_context

    return otel_context.set_value(_OWNER_KEY, True, context)


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


_AUTO_SOURCES = (
    "inline base64 and data-URI images, PDFs and files in OpenAI chat and Responses requests, "
    "including computer_call_output screenshots (traceai_openai)",
    "OpenAI images.generate b64_json and completed Responses image_generation_call output (traceai_openai)",
    "OpenAI and Groq speech the SDK buffered, and OpenAI speech the application consumed",
    "OpenAI and Groq transcription/translation audio the SDK holds as bytes",
    "Anthropic Messages base64 images and PDFs, and computer-use tool_result screenshots (traceai_anthropic)",
    "Pipecat and LiveKit recordings, only through an explicit bind_* call",
    "for every other integration, exactly the media_operations of its provider_adapter row below "
    "(the installed instrumentor version decides)",
)
_AUTO_UNSUPPORTED = (
    "integrations whose media row is 'unqualified'",
    "remote URLs and provider file ids (never fetched)",
    "open file objects and file paths the SDK does not read itself",
    "partial or streamed images, OpenAI images.edit and variations",
    "Anthropic beta.messages and generated output media",
    "HTTP wire capture (the Python auto module has no wire layer)",
    "new recordings without an explicit voice binding",
    "product playback qualification",
)


def _attachment_report() -> dict:
    manager = get_attachment_manager()
    if not _ATTACHMENTS_SUPPORTED:
        state = "unsupported_version"
    elif manager is None:
        state = "not_initialized"
    elif _env_disabled():
        # The kill switch denies at every admission even for an enabled manager,
        # whose own status() does not consult it.
        state = "disabled"
    else:
        try:
            status = manager.status()
            state = status.state
        except Exception:
            status, state = None, "unavailable"
    report = {"state": state, "transport": "shared_manager", "borrowed": _attachment_manager_borrowed,
              "sources": list(_AUTO_SOURCES), "unsupported": list(_AUTO_UNSUPPORTED)}
    if state not in ("unsupported_version", "not_initialized", "disabled", "unavailable"):
        report["status"] = {name: getattr(status, name) for name in
                            ("queued", "active", "tickets", "streams", "recordings", "owned_bytes", "dropped")
                            if isinstance(getattr(status, name, None), int)}
    return report


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _media_support(module: str) -> Dict[str, Any]:
    """Whether the INSTALLED instrumentor version captures media; imports, never instruments."""
    if not _ATTACHMENTS_SUPPORTED or not _installed(module):
        return {"media": "unqualified"}
    try:
        if module in _MEDIA_ADAPTERS:
            path, name = _MEDIA_ADAPTERS[module]
            descriptor = getattr(importlib.import_module(path), name)
            operations = list(descriptor["operations"])
            if operations:
                return {"media": "provider_adapter", "media_operations": operations}
            # A descriptor that claims no operation (tracing only) is not a media adapter.
            return {"media": "unqualified"}
        if module in _SESSION_BINDINGS and callable(getattr(importlib.import_module(module),
                                                            _SESSION_BINDINGS[module], None)):
            return {"media": "session_binding"}
    except Exception:
        # An older instrumentor or core: no descriptor means no media capture.
        pass
    return {"media": "unqualified"}


def report() -> dict:
    """Content-free setup report: attachment state and per-integration media support.

    ``integrations[].media`` is ``provider_adapter`` when the installed instrumentor
    captures media through the shared manager, ``session_binding`` for voice
    integrations that capture only through an explicit ``bind_*`` call, otherwise
    ``unqualified``. It describes capability, not that any media was captured, and
    never a playback claim. No endpoint, header or credential is included.
    """
    names = {}
    for name, module in _INSTRUMENTOR_REGISTRY.items():
        names.setdefault(module, name)
    integrations = []
    for module, name in names.items():
        aliases = [alias for alias, target in _INSTRUMENTOR_REGISTRY.items() if target == module]
        status = next((_adapter_status[a] for a in aliases if a in _adapter_status), None)
        if status is None:
            status = "not_enabled" if _installed(module) else "not_installed"
        integrations.append({"name": name, "package": module, "status": status, **_media_support(module)})
    return {"initialized": _tracer_provider is not None, "attachments": _attachment_report(),
            "integrations": integrations}


def attachment_status() -> dict:
    """Content-free setup status, never an integrated recording-ready claim."""
    if not _ATTACHMENTS_SUPPORTED:
        return {"runtime": "unsupported_version", "adapters": dict(_adapter_status)}
    manager = get_attachment_manager()
    if manager is None:
        runtime = "uninitialized"
    else:
        runtime = "disabled" if _env_disabled() else manager.status().state
    return {"runtime": runtime, "adapters": dict(_adapter_status)}


def _bind(library: str, entry_point: str, *args, **kwargs):
    if not _ATTACHMENTS_SUPPORTED:
        _adapter_status[library] = "unsupported_version"
        return None
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


def _explicit_kwargs(library: str, kwargs: dict, provider_key: str, config_key: str) -> dict:
    """Defaults for an explicit entry point: auto's provider, its trace config and, only
    while capture is on and the installed package has the media adapter, the ONE manager."""
    if _tracer_provider is None:
        raise RuntimeError("initialize traceai before connecting " + library)
    kwargs.setdefault(provider_key, _tracer_provider)
    config = getattr(_tracer_provider, "trace_config", None)
    if config is not None:
        # Auto's hide and PII flags reach the integration; the caller can pass its own.
        kwargs.setdefault(config_key, config)
    if _ATTACHMENTS_SUPPORTED:
        manager = get_attachment_manager()
        module = _INSTRUMENTOR_REGISTRY[library]
        if _capturing(manager) and _media_support(module)["media"] == "provider_adapter":
            kwargs.setdefault("attachments", manager)
    return kwargs


def mcp_use_callback(**kwargs):
    """A ``traceai_mcp_use.FutureAGICallback`` on auto's provider and shared manager.

    Pass it to the agent: ``MCPAgent(..., callbacks=[traceai.mcp_use_callback()])``.
    With attachments off, or an installed ``traceAI-mcp-use`` without the media adapter,
    it is the same callback as before (no ``attachments=`` argument). Keyword arguments
    are passed through and win over auto's defaults.
    """
    module = importlib.import_module(_INSTRUMENTOR_REGISTRY["mcp_use"])
    callback = module.FutureAGICallback(**_explicit_kwargs("mcp_use", kwargs, "tracer_provider", "config"))
    _adapter_status["mcp_use"] = "registered"
    return callback


def wrap_mcp_client(session, **kwargs):
    """Trace an ``mcp.ClientSession`` with ``traceai_mcp.wrap_mcp_client`` on auto's provider
    and shared manager, so tool and resource media reach Future AGI.

    The MCP instrumentor only propagates context; this is the entry point that captures
    media. An installed ``traceAI-mcp`` without ``wrap_mcp_client`` returns the session
    unchanged and reports ``unsupported_version``.
    """
    module = importlib.import_module(_INSTRUMENTOR_REGISTRY["mcp"])
    wrap = getattr(module, "wrap_mcp_client", None)
    if wrap is None:
        _adapter_status["mcp"] = "unsupported_version"
        return session
    wrapped = wrap(session, **_explicit_kwargs("mcp", kwargs, "tracer_provider", "trace_config"))
    _adapter_status.setdefault("mcp", "registered")
    return wrapped


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
    "get_attachment_manager",
    "report",
    "with_attachment_capture_policy",
    "with_attachment_ownership",
    "bind_pipecat_audio_processor",
    "bind_livekit_session",
    "mcp_use_callback",
    "wrap_mcp_client",
    "instrument",
    "get_tracer_provider",
    "SemanticConvention",
    "Transport",
]
