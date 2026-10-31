"""Every traceAI Python integration, and how to switch it on without user code.

Each entry names the import that triggers it (``targets``), the PyPI package that provides
it, and how to enable it. ``instrumentor`` entries are BaseInstrumentor-style classes;
``setup`` entries call a function from ``_SETUPS`` below; ``manual``, ``plugin`` and
``service`` entries cannot be enabled from inside the process and are reported with
``note``. ``wire`` lists the wire-layer formats that still trace the library when the
dedicated package is not installed.
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
    wire: Tuple[str, ...] = ()


_OPENAI_WIRE = ("openai.chat", "openai.responses", "openai.embeddings", "openai.audio_transcription", "openai.audio_speech")


def _covered_by_openai(provider: str) -> str:
    return (
        f"Wraps the OpenAI SDK for {provider}. The openai integration and the wire layer already "
        f"trace these calls, so it is off by default; add it to FI_INSTRUMENTATIONS to use it instead."
    )


INTEGRATIONS: Tuple[Integration, ...] = (
    # Model providers
    Integration("openai", ("openai",), "traceAI-openai", "traceai_openai", "OpenAIInstrumentor", wire=_OPENAI_WIRE),
    Integration("openai_realtime", ("openai.resources.realtime.realtime",), "traceAI-openai-realtime", "traceai_openai_realtime", "OpenAIRealtimeInstrumentor", category="voice"),
    Integration("anthropic", ("anthropic",), "traceAI-anthropic", "traceai_anthropic", "AnthropicInstrumentor", wire=("anthropic.messages",)),
    Integration("google_genai", ("google.genai",), "traceAI-google-genai", "traceai_google_genai", "GoogleGenAIInstrumentor", wire=("gemini.generate_content", "gemini.embed_content")),
    Integration("google_genai_live", ("google.genai",), "traceAI-google-genai-live", "traceai_google_genai_live", "GoogleGenAILiveInstrumentor", category="voice"),
    Integration("vertexai", ("vertexai",), "traceAI-vertexai", "traceai_vertexai", "VertexAIInstrumentor", wire=("gemini.generate_content",)),
    Integration("bedrock", ("boto3",), "traceAI-bedrock", "traceai_bedrock", "BedrockInstrumentor"),
    Integration("mistralai", ("mistralai",), "traceAI-mistralai", "traceai_mistralai", "MistralAIInstrumentor", wire=("openai.chat", "openai.embeddings")),
    Integration("cohere", ("cohere",), "traceAI-cohere", "traceai_cohere", "CohereInstrumentor", wire=("cohere.chat", "cohere.embed", "rerank")),
    Integration("groq", ("groq",), "traceAI-groq", "traceai_groq", "GroqInstrumentor", wire=("openai.chat", "openai.audio_transcription")),
    Integration("together", ("together",), "traceAI-together", "traceai_together", "TogetherInstrumentor", wire=("openai.chat", "openai.embeddings")),
    Integration("cerebras", ("cerebras.cloud.sdk",), "traceAI-cerebras", "traceai_cerebras", "CerebrasInstrumentor", wire=("openai.chat",)),
    Integration("ollama", ("ollama",), "traceAI-ollama", "traceai_ollama", "OllamaInstrumentor", wire=("ollama.chat", "ollama.generate", "ollama.embed")),
    Integration("huggingface", ("huggingface_hub",), "traceAI-huggingface", "traceai_huggingface", "HuggingFaceInstrumentor", wire=("openai.chat",)),
    Integration("litellm", ("litellm",), "traceAI-litellm", "traceai_litellm", "LiteLLMInstrumentor", category="gateway"),
    Integration("portkey", ("portkey_ai",), "traceAI-portkey", "traceai_portkey", "PortkeyInstrumentor", category="gateway", wire=("openai.chat",)),
    Integration("replicate", ("replicate",), "traceAI-replicate", "traceai_replicate", "ReplicateInstrumentor"),
    Integration("voyage", ("voyageai",), "traceAI-voyage", "traceai_voyage", "VoyageInstrumentor", wire=("openai.embeddings", "rerank")),
    Integration("discoveryengine", ("google.cloud.discoveryengine",), "traceAI-discoveryengine", "traceai_discoveryengine", "DiscoveryEngineInstrumentor", category="retrieval"),
    Integration("typesafe", ("typesafe_sdk",), "traceAI-typesafe", "traceai_typesafe", "TypeSafeInstrumentor", category="guardrail"),
    Integration("deepseek", ("openai",), "traceAI-deepseek", "traceai_deepseek", "DeepSeekInstrumentor", default=False, note=_covered_by_openai("DeepSeek"), wire=("openai.chat",)),
    Integration("fireworks", ("openai",), "traceAI-fireworks", "traceai_fireworks", "FireworksInstrumentor", default=False, note=_covered_by_openai("Fireworks"), wire=("openai.chat",)),
    Integration("xai", ("openai",), "traceAI-xai", "traceai_xai", "XAIInstrumentor", default=False, note=_covered_by_openai("xAI"), wire=("openai.chat",)),
    Integration("minimax", ("openai",), "traceAI-minimax", "traceai_minimax", "MiniMaxInstrumentor", default=False, note=_covered_by_openai("MiniMax"), wire=("openai.chat",)),
    Integration("vllm", ("openai",), "traceAI-vllm", "traceai_vllm", "VLLMInstrumentor", default=False, note=_covered_by_openai("vLLM servers"), wire=("openai.chat",)),
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
    Integration("semantic_kernel", ("semantic_kernel",), "traceAI-semantic-kernel", "traceai_semantic_kernel", "semantic_kernel", kind=SETUP, category="framework"),
    Integration("strands", ("strands",), "traceai-strands", "traceai_strands", "strands", kind=SETUP, category="framework"),
    Integration("agno", ("agno",), "traceai-agno", "traceai_agno", "agno", kind=SETUP, category="framework"),
    Integration("beeai", ("beeai_framework",), "traceai-beeai", "traceai_beeai", "beeai", kind=SETUP, category="framework"),
    Integration("agent_framework", ("agent_framework",), "traceAI-agent-framework", "traceai_agent_framework", "agent_framework", kind=SETUP, category="framework"),
    Integration("ag2_classic", ("autogen",), "traceAI-ag2-classic", "traceai_ag2_classic", "ag2_classic", kind=SETUP, category="framework"),
    Integration("ag2", ("autogen.beta",), "traceAI-ag2", "traceai_ag2", kind=MANUAL, category="framework",
                note="AG2 telemetry middleware attaches per agent: call traceai_ag2.setup(agent, ...) once your agents exist."),
    Integration("mcp", ("mcp",), "traceAI-mcp", "traceai_mcp", "MCPInstrumentor", category="tool"),
    Integration("mcp_use", ("mcp_use",), "traceAI-mcp-use", "traceai_mcp_use", kind=MANUAL, category="tool",
                note="mcp-use reports through a callback: pass traceai_mcp_use.FutureAGICallback() to your MCPAgent."),
    Integration("a2a", ("a2a",), "traceAI-a2a", "traceai_a2a", "A2AInstrumentor", category="tool"),
    Integration("guardrails", ("guardrails",), "traceAI-guardrails", "traceai_guardrails", "GuardrailsInstrumentor", category="guardrail"),
    Integration("hermes_agent", ("hermes",), "traceAI-hermes-agent", "traceai_hermes_agent", kind=PLUGIN, category="framework",
                note="Hermes loads traceAI-hermes-agent itself through its plugin entry point; installing the package is enough."),
    # Voice agents
    Integration("livekit", ("livekit.agents",), "traceAI-livekit", "traceai_livekit", "livekit", kind=SETUP, category="voice"),
    Integration("pipecat", ("pipecat",), "traceAI-pipecat", "traceai_pipecat", "pipecat", kind=SETUP, category="voice",
                note="Pipecat only emits spans for a PipelineTask created with enable_tracing=True."),
    Integration("elevenlabs_agents", ("elevenlabs",), "traceAI-elevenlabs-agents", "traceai_elevenlabs_agents", kind=SERVICE, category="voice",
                note="ElevenLabs Agents run on ElevenLabs; forward their webhooks with `traceai-elevenlabs-agents serve`. Direct ElevenLabs TTS/STT API calls are traced by the wire layer.",
                wire=("elevenlabs.tts", "elevenlabs.stt")),
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


# Web servers: incoming requests become root SERVER spans (the "transaction"), so every AI
# call is nested under the request that caused it. These use the OpenTelemetry contrib
# instrumentors; `fi-instrument bootstrap` installs the ones the environment needs.
WEB: Tuple[Integration, ...] = tuple(
    Integration(name, (target,), f"opentelemetry-instrumentation-{dist}", f"opentelemetry.instrumentation.{module}", entry, category="web")
    for name, target, dist, module, entry in (
        ("fastapi", "fastapi", "fastapi", "fastapi", "FastAPIInstrumentor"),
        ("flask", "flask", "flask", "flask", "FlaskInstrumentor"),
        ("django", "django", "django", "django", "DjangoInstrumentor"),
        ("falcon", "falcon", "falcon", "falcon", "FalconInstrumentor"),
        ("tornado", "tornado", "tornado", "tornado", "TornadoInstrumentor"),
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


def _strands(module: Any, provider: Any, config: Any) -> str:
    # Strands' singleton __init__ resets its provider/method handles. Do not
    # reconstruct an application's live singleton merely to inspect its state.
    instrumentor = getattr(module.StrandsInstrumentor, "_instance", None) or module.StrandsInstrumentor()
    if (getattr(instrumentor, "is_instrumented", False)
            or getattr(instrumentor, "is_instrumented_by_opentelemetry", False)):
        return "already instrumented"
    instrumentor.instrument(tracer_provider=provider)
    return _SetupResult("Strands telemetry routed to traceAI", instrumentor.uninstrument)


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
    # Install the package's pure mapper on the actual FI route, not a legacy
    # global-provider exporter slot. Privacy stays outside mapping at final export.
    from fi_instrumentation.auto import _manager

    raw = getattr(provider, "__wrapped__", provider)
    route = _manager._state.overlays.get(raw)
    if route is None:
        if not module.enable_fi_attribute_mapping():
            raise RuntimeError("no Future AGI exporter to map")
        return "exporter attribute mapping installed"
    mapper = module.BaseMappedSpanExporter()._convert_attributes
    route.mappers.append(mapper)
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
    "strands": _strands,
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
