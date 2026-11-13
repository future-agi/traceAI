"""Every traceAI Python integration, and how to switch it on without user code.

Each entry names the imports that trigger it (``targets``), the PyPI package that provides it,
and how to enable it. ``instrumentor`` entries are BaseInstrumentor-style classes; ``setup``
entries call a function from ``SETUPS`` below; ``manual``, ``plugin`` and ``service`` entries
cannot be enabled from inside the process and are reported with ``note``, which says what to do.

The Future AGI route carries spans from traceAI instrumentation scopes. ``scopes`` adds the
instrumentation scope prefixes of spans an integration routes but does not create itself: the
library's own spans, which its traceAI package maps. For ``web`` entries they are the server
instrumentation's scopes, and only request root spans of traces with an AI span are carried.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

INSTRUMENTOR = "instrumentor"
SETUP = "setup"
MANUAL = "manual"
PLUGIN = "plugin"
SERVICE = "service"


@dataclass(frozen=True)
class Integration:
    name: str
    targets: Tuple[str, ...]
    package: str
    module: str
    entry: str = ""
    kind: str = INSTRUMENTOR
    category: str = "llm"
    default: bool = True
    note: str = ""
    scopes: Tuple[str, ...] = ()


def _covered_by_openai(provider: str) -> str:
    return (
        f"Wraps the OpenAI SDK for {provider}, so the openai integration already traces these calls "
        f"and it is off by default. To use it instead, list the integrations you want in FI_INSTRUMENTATIONS "
        f"and leave openai out."
    )


INTEGRATIONS: Tuple[Integration, ...] = (
    # Model providers
    Integration("openai", ("openai",), "traceAI-openai", "traceai_openai", "OpenAIInstrumentor"),
    Integration("openai_realtime", ("openai.resources.realtime.realtime",), "traceAI-openai-realtime", "traceai_openai_realtime", "OpenAIRealtimeInstrumentor", category="voice"),
    Integration("anthropic", ("anthropic",), "traceAI-anthropic", "traceai_anthropic", "AnthropicInstrumentor"),
    Integration("google_genai", ("google.genai",), "traceAI-google-genai", "traceai_google_genai", "GoogleGenAIInstrumentor"),
    Integration("google_genai_live", ("google.genai",), "traceAI-google-genai-live", "traceai_google_genai_live", "GoogleGenAILiveInstrumentor", category="voice"),
    Integration("vertexai", ("vertexai",), "traceAI-vertexai", "traceai_vertexai", "VertexAIInstrumentor"),
    Integration("bedrock", ("boto3",), "traceAI-bedrock", "traceai_bedrock", "BedrockInstrumentor"),
    Integration("mistralai", ("mistralai",), "traceAI-mistralai", "traceai_mistralai", "MistralAIInstrumentor",
                scopes=("mistralai",)),
    Integration("cohere", ("cohere",), "traceAI-cohere", "traceai_cohere", "CohereInstrumentor"),
    Integration("groq", ("groq",), "traceAI-groq", "traceai_groq", "GroqInstrumentor"),
    Integration("together", ("together",), "traceAI-together", "traceai_together", "TogetherInstrumentor"),
    Integration("cerebras", ("cerebras.cloud.sdk",), "traceAI-cerebras", "traceai_cerebras", "CerebrasInstrumentor"),
    Integration("ollama", ("ollama",), "traceAI-ollama", "traceai_ollama", "OllamaInstrumentor"),
    Integration("huggingface", ("huggingface_hub",), "traceAI-huggingface", "traceai_huggingface", "HuggingFaceInstrumentor"),
    Integration("transformers", ("transformers",), "traceAI-transformers", "traceai_transformers", "TransformersInstrumentor"),
    Integration("litellm", ("litellm",), "traceAI-litellm", "traceai_litellm", "LiteLLMInstrumentor", category="gateway"),
    Integration("openrouter", ("openrouter",), "traceAI-openrouter", "traceai_openrouter", "OpenRouterInstrumentor", category="gateway"),
    Integration("portkey", ("portkey_ai",), "traceAI-portkey", "traceai_portkey", "PortkeyInstrumentor", category="gateway"),
    Integration("replicate", ("replicate",), "traceAI-replicate", "traceai_replicate", "ReplicateInstrumentor"),
    Integration("voyage", ("voyageai",), "traceAI-voyage", "traceai_voyage", "VoyageInstrumentor"),
    Integration("discoveryengine", ("google.cloud.discoveryengine",), "traceAI-discoveryengine", "traceai_discoveryengine", "DiscoveryEngineInstrumentor", category="retrieval"),
    Integration("typesafe", ("typesafe_sdk",), "traceAI-typesafe", "traceai_typesafe", "TypeSafeInstrumentor", category="guardrail"),
    Integration("deepseek", ("openai",), "traceAI-deepseek", "traceai_deepseek", "DeepSeekInstrumentor", default=False, note=_covered_by_openai("DeepSeek")),
    Integration("fireworks", ("openai",), "traceAI-fireworks", "traceai_fireworks", "FireworksInstrumentor", default=False, note=_covered_by_openai("Fireworks")),
    Integration("xai", ("openai",), "traceAI-xai", "traceai_xai", "XAIInstrumentor", default=False, note=_covered_by_openai("xAI")),
    Integration("minimax", ("openai",), "traceAI-minimax", "traceai_minimax", "MiniMaxInstrumentor", default=False, note=_covered_by_openai("MiniMax")),
    Integration("vllm", ("openai",), "traceAI-vllm", "traceai_vllm", "VLLMInstrumentor", default=False, note=_covered_by_openai("vLLM servers")),
    # Agent and orchestration frameworks
    Integration("langchain", ("langchain_core",), "traceAI-langchain", "traceai_langchain", "LangChainInstrumentor", category="framework"),
    Integration("langgraph", ("langgraph",), "traceAI-langchain", "traceai_langchain", "LangGraphInstrumentor", category="framework"),
    Integration("llama_index", ("llama_index.core",), "traceAI-llamaindex", "traceai_llamaindex", "LlamaIndexInstrumentor", category="framework"),
    Integration("openai_agents", ("agents",), "traceAI-openai-agents", "traceai_openai_agents", "OpenAIAgentsInstrumentor", category="framework"),
    Integration("claude_agent_sdk", ("claude_agent_sdk",), "traceai-claude-agent-sdk", "traceai_claude_agent_sdk", "ClaudeAgentInstrumentor", category="framework"),
    Integration("google_adk", ("google.adk",), "traceAI-google-adk", "traceai_google_adk", "GoogleADKInstrumentor", category="framework"),
    Integration("crewai", ("crewai",), "traceAI-crewai", "traceai_crewai", "CrewAIInstrumentor", category="framework"),
    Integration("autogen", ("autogen_agentchat",), "traceAI-autogen", "traceai_autogen", "AutogenInstrumentor", category="framework"),
    Integration("dspy", ("dspy",), "traceAI-DSPy", "traceai_dspy", "DSPyInstrumentor", category="framework"),
    Integration("haystack", ("haystack",), "traceAI-haystack", "traceai_haystack", "HaystackInstrumentor", category="framework"),
    Integration("smolagents", ("smolagents",), "traceAI-smolagents", "traceai_smolagents", "SmolagentsInstrumentor", category="framework"),
    Integration("pydantic_ai", ("pydantic_ai",), "traceai-pydantic-ai", "traceai_pydantic_ai", "PydanticAIInstrumentor", category="framework"),
    Integration("instructor", ("instructor",), "traceAI-instructor", "traceai_instructor", "InstructorInstrumentor", category="framework"),
    Integration("semantic_kernel", ("semantic_kernel",), "traceAI-semantic-kernel", "traceai_semantic_kernel", "semantic_kernel", kind=SETUP, category="framework",
                scopes=("semantic_kernel",)),
    # Its privacy and attribute conversion happen in an exporter wrapper the agent does not install.
    Integration("strands", ("strands",), "traceai-strands", "traceai_strands", kind=MANUAL, category="framework",
                note="Install traceai-strands and, before creating an Agent, call register(set_global_tracer_provider=True, "
                     "span_exporter=StrandsSpanExporter(HTTPSpanExporter(), capture_content=True)) as its README shows. "
                     "To trace other libraries too, call fi_instrumentation.init() after it instead of starting with "
                     "fi-instrument."),
    Integration("agno", ("agno",), "traceai-agno", "traceai_agno", "agno", kind=SETUP, category="framework"),
    Integration("beeai", ("beeai_framework",), "traceai-beeai", "traceai_beeai", "beeai", kind=SETUP, category="framework"),
    Integration("agent_framework", ("agent_framework",), "traceAI-agent-framework", "traceai_agent_framework", "agent_framework", kind=SETUP, category="framework",
                scopes=("agent_framework",)),
    Integration("ag2_classic", ("autogen",), "traceAI-ag2-classic", "traceai_ag2_classic", "ag2_classic", kind=SETUP, category="framework",
                scopes=("opentelemetry.instrumentation.ag2",)),
    Integration("ag2", ("autogen.beta",), "traceAI-ag2", "traceai_ag2", kind=MANUAL, category="framework",
                note="AG2 telemetry middleware attaches per agent: install traceAI-ag2 and call traceai_ag2.setup(agent, ...) once your agents exist."),
    Integration("agentscope", ("agentscope",), "traceAI-agentscope", "traceai_agentscope", kind=MANUAL, category="framework",
                note="Install traceAI-agentscope, call tracing = traceai_agentscope.configure(project_name=...) once, "
                     "and pass middlewares=[traceai_agentscope.TracingMiddleware()] to each Agent."),
    Integration("temporal", ("temporalio",), "traceAI-temporal", "traceai_temporal", kind=MANUAL, category="framework",
                note="Install traceAI-temporal and call telemetry = traceai_temporal.configure() before Client.connect(..., "
                     "plugins=[telemetry.plugin]). It installs its own tracer provider, so start this app without the agent."),
    Integration("cognee", ("cognee",), "traceai-cognee", "traceai_cognee", kind=MANUAL, category="framework",
                note="Install traceai-cognee and call traceai_cognee.configure(project_name=...) once, before cognee.add, "
                     "cognify or search."),
    Integration("openwebui", ("open_webui",), "traceAI-openwebui", "traceai_openwebui", kind=MANUAL, category="framework",
                note="Open WebUI traces model calls through a Pipe Function: install traceAI-openwebui in Open WebUI's "
                     "environment and enable its Traced model Function."),
    Integration("mcp", ("mcp",), "traceAI-mcp", "traceai_mcp", "MCPInstrumentor", category="tool"),
    Integration("mcp_use", ("mcp_use",), "traceAI-mcp-use", "traceai_mcp_use", kind=MANUAL, category="tool",
                note="mcp-use reports through a callback: install traceAI-mcp-use and pass traceai_mcp_use.FutureAGICallback() to your MCPAgent."),
    Integration("a2a", ("a2a",), "traceAI-a2a", "traceai_a2a", "A2AInstrumentor", category="tool", scopes=("a2a",)),
    Integration("guardrails", ("guardrails",), "traceAI-guardrails", "traceai_guardrails", "GuardrailsInstrumentor", category="guardrail",
                scopes=("guardrails",)),
    Integration("hermes_agent", ("hermes",), "traceAI-hermes-agent", "traceai_hermes_agent", kind=PLUGIN, category="framework",
                note="Hermes loads traceAI-hermes-agent itself through its plugin entry point; installing the package is enough."),
    # Voice agents
    Integration("livekit", ("livekit.agents",), "traceAI-livekit", "traceai_livekit", "livekit", kind=SETUP, category="voice",
                scopes=("livekit",)),
    Integration("pipecat", ("pipecat",), "traceAI-pipecat", "traceai_pipecat", "pipecat", kind=SETUP, category="voice",
                note="Pipecat only emits spans for a PipelineTask created with enable_tracing=True.", scopes=("pipecat",)),
    Integration("elevenlabs_agents", ("elevenlabs",), "traceAI-elevenlabs-agents", "traceai_elevenlabs_agents", kind=SERVICE, category="voice",
                note="ElevenLabs Agents run on ElevenLabs; forward their webhooks with `traceai-elevenlabs-agents serve`."),
    # Tools and search
    Integration("exa", ("exa_py",), "traceAI-exa", "traceai_exa", "ExaInstrumentor", category="tool"),
    Integration("firecrawl", ("firecrawl",), "traceAI-firecrawl", "traceai_firecrawl", "FirecrawlInstrumentor", category="tool"),
    Integration("tavily", ("tavily",), "traceAI-tavily", "traceai_tavily", "TavilyInstrumentor", category="tool"),
    Integration("parallel", ("parallel",), "traceAI-parallel", "traceai_parallel", "ParallelInstrumentor", category="tool"),
    # Vector databases
    Integration("chromadb", ("chromadb",), "traceAI-chromadb", "traceai_chromadb", "ChromaDBInstrumentor", category="vector_db"),
    Integration("pinecone", ("pinecone",), "traceAI-pinecone", "traceai_pinecone", "PineconeInstrumentor", category="vector_db"),
    Integration("qdrant", ("qdrant_client",), "traceAI-qdrant", "traceai_qdrant", "QdrantInstrumentor", category="vector_db"),
    Integration("weaviate", ("weaviate",), "traceAI-weaviate", "traceai_weaviate", "WeaviateInstrumentor", category="vector_db"),
    Integration("milvus", ("pymilvus",), "traceAI-milvus", "traceai_milvus", "MilvusInstrumentor", category="vector_db"),
    Integration("lancedb", ("lancedb",), "traceAI-lancedb", "traceai_lancedb", "LanceDBInstrumentor", category="vector_db"),
    Integration("mongodb", ("pymongo",), "traceAI-mongodb", "traceai_mongodb", "MongoDBInstrumentor", category="vector_db"),
    Integration("pgvector", ("pgvector",), "traceAI-pgvector", "traceai_pgvector", "PgVectorInstrumentor", category="vector_db"),
    Integration("redis", ("redisvl", "redis.commands.search"), "traceAI-redis", "traceai_redis", "RedisInstrumentor", category="vector_db"),
)


# Web servers: incoming requests become root SERVER spans, so every AI call is nested under the
# request that caused it. These use the OpenTelemetry contrib instrumentors;
# `fi-instrument bootstrap --all` installs the ones the environment needs. The Future AGI route
# carries a request's root span only when that request made an AI call. FastAPI's server spans
# come from the ASGI middleware it installs.
WEB: Tuple[Integration, ...] = tuple(
    Integration(name, (target,), f"opentelemetry-instrumentation-{dist}", f"opentelemetry.instrumentation.{module}", entry,
                category="web", scopes=(f"opentelemetry.instrumentation.{module}",) + extra)
    for name, target, dist, module, entry, extra in (
        ("fastapi", "fastapi", "fastapi", "fastapi", "FastAPIInstrumentor", ("opentelemetry.instrumentation.asgi",)),
        ("flask", "flask", "flask", "flask", "FlaskInstrumentor", ()),
        ("django", "django", "django", "django", "DjangoInstrumentor", ()),
        ("falcon", "falcon", "falcon", "falcon", "FalconInstrumentor", ()),
        ("tornado", "tornado", "tornado", "tornado", "TornadoInstrumentor", ()),
    )
)

ALL: Tuple[Integration, ...] = INTEGRATIONS + WEB


# Setup adapters for packages that are not BaseInstrumentor classes. Each takes the
# imported traceAI module, the tracer provider and the TraceConfig, and returns a short
# detail string for the report.


class _SetupResult(str):
    """A report detail with the exact handle needed to undo this setup."""

    def __new__(cls, detail: str, cleanup: Callable[[], None]) -> "_SetupResult":
        result = super().__new__(cls, detail)
        result.cleanup = cleanup
        return result


def _semantic_kernel(module: Any, provider: Any, config: Any) -> str:
    instrumentor = module.SemanticKernelInstrumentor()
    if (getattr(instrumentor, "is_instrumented", False)
            or getattr(instrumentor, "is_instrumented_by_opentelemetry", False)):
        return "already instrumented"
    instrumentor.instrument(tracer_provider=provider, config=config)
    return _SetupResult("diagnostics on, mapping processor installed", instrumentor.uninstrument)


def _agno(module: Any, provider: Any, config: Any) -> str:
    handle = getattr(getattr(module, "AgnoInstrumentorWrapper", None), "_instance", None)
    if handle is not None and handle.is_instrumented:
        return "already instrumented"
    handle = module.configure_agno_tracing(tracer_provider=provider)
    return _SetupResult("Agno tracing configured", handle.uninstrument)


def _beeai(module: Any, provider: Any, config: Any) -> str:
    handle = getattr(getattr(module, "BeeAIInstrumentorWrapper", None), "_instance", None)
    if handle is not None and handle.is_instrumented:
        return "already instrumented"
    handle = module.configure_beeai_tracing(tracer_provider=provider)
    return _SetupResult("BeeAI tracing configured", handle.uninstrument)


def _agent_framework(module: Any, provider: Any, config: Any) -> str:
    try:
        from agent_framework.observability import OBSERVABILITY_SETTINGS

        native_settings = OBSERVABILITY_SETTINGS
        was_enabled = native_settings.enable_instrumentation
    except ImportError:
        native_settings, was_enabled = None, True
    if not module.enable_fi_attribute_mapping(tracer_provider=provider, config=config):
        raise RuntimeError("mapping processor was not installed")

    def cleanup() -> None:
        if native_settings is not None and not was_enabled:
            native_settings.enable_instrumentation = False

    return _SetupResult("attribute mapping processor installed", cleanup)


def _ag2_classic(module: Any, provider: Any, config: Any) -> str:
    handle = module.setup(provider, config=config)
    return _SetupResult("AG2 Classic native tracing enabled", handle.uninstrument)


def _exporter_mapping(module: Any, provider: Any, config: Any) -> str:
    # Install the package's mapper on the agent's Future AGI route only, so other
    # exporters on the same provider keep the original attributes.
    from fi_instrumentation.auto import _manager

    raw = getattr(provider, "__wrapped__", provider)
    route = _manager._state.overlays.get(raw)
    if route is None:
        if not module.enable_fi_attribute_mapping():
            raise RuntimeError("no Future AGI exporter to map")
        return "exporter attribute mapping installed"
    route.mappers.append(module.BaseMappedSpanExporter()._convert_attributes)
    if module.__name__ == "traceai_livekit":
        try:
            from livekit.agents.telemetry import set_tracer_provider

            set_tracer_provider(raw)
        except ImportError:
            pass
        module.flush_on_job_shutdown(raw)
    return "attribute mapper installed on the Future AGI route"


SETUPS: Dict[str, Callable[[Any, Any, Any], str]] = {
    "semantic_kernel": _semantic_kernel,
    "agno": _agno,
    "beeai": _beeai,
    "agent_framework": _agent_framework,
    "ag2_classic": _ag2_classic,
    "livekit": _exporter_mapping,
    "pipecat": _exporter_mapping,
}


def by_name() -> Dict[str, Integration]:
    return {i.name: i for i in ALL}


def find(name: str) -> Optional[Integration]:
    return by_name().get(name.strip().lower().replace("-", "_"))
