package ai.traceai;

/**
 * GenAI semantic conventions for AI observability.
 * Based on OpenTelemetry GenAI semantic conventions.
 *
 * @see <a href="https://opentelemetry.io/docs/specs/semconv/gen-ai/">GenAI Semantic Conventions</a>
 */
public final class SemanticConventions {

    // ── FI Span Kind ─────────────────────────────────────────────────
    public static final String FI_SPAN_KIND = "fi.span.kind";

    // ── GenAI Core ───────────────────────────────────────────────────
    public static final String GEN_AI_OPERATION_NAME = "gen_ai.operation.name";
    public static final String GEN_AI_SYSTEM = "gen_ai.system";

    // LLM System/Provider — both resolve to gen_ai.provider.name
    public static final String LLM_SYSTEM = "gen_ai.provider.name";
    public static final String LLM_PROVIDER = "gen_ai.provider.name";

    // ── GenAI Request ────────────────────────────────────────────────
    public static final String LLM_MODEL_NAME = "gen_ai.request.model";
    public static final String LLM_REQUEST_MODEL = "gen_ai.request.model";
    public static final String LLM_REQUEST_TEMPERATURE = "gen_ai.request.temperature";
    public static final String LLM_REQUEST_TOP_P = "gen_ai.request.top_p";
    public static final String GEN_AI_REQUEST_TOP_K = "gen_ai.request.top_k";
    public static final String LLM_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens";
    public static final String GEN_AI_REQUEST_FREQUENCY_PENALTY = "gen_ai.request.frequency_penalty";
    public static final String GEN_AI_REQUEST_PRESENCE_PENALTY = "gen_ai.request.presence_penalty";
    public static final String LLM_REQUEST_STOP_SEQUENCES = "gen_ai.request.stop_sequences";
    public static final String GEN_AI_REQUEST_SEED = "gen_ai.request.seed";
    public static final String GEN_AI_REQUEST_PARAMETERS = "gen_ai.request.parameters";

    // ── GenAI Response ───────────────────────────────────────────────
    public static final String LLM_RESPONSE_MODEL = "gen_ai.response.model";
    public static final String LLM_RESPONSE_ID = "gen_ai.response.id";
    public static final String LLM_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reasons";
    public static final String GEN_AI_OUTPUT_TYPE = "gen_ai.output.type";

    // ── GenAI Usage / Tokens ─────────────────────────────────────────
    public static final String LLM_TOKEN_COUNT_PROMPT = "gen_ai.usage.input_tokens";
    public static final String LLM_TOKEN_COUNT_COMPLETION = "gen_ai.usage.output_tokens";
    public static final String LLM_TOKEN_COUNT_TOTAL = "gen_ai.usage.total_tokens";
    public static final String GEN_AI_USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_tokens";
    public static final String GEN_AI_USAGE_CACHE_WRITE_TOKENS = "gen_ai.usage.cache_write_tokens";

    // ── Token Detail Breakdowns ──────────────────────────────────────
    public static final String GEN_AI_USAGE_OUTPUT_TOKENS_AUDIO = "gen_ai.usage.output_tokens.audio";
    public static final String GEN_AI_USAGE_OUTPUT_TOKENS_REASONING = "gen_ai.usage.output_tokens.reasoning";
    public static final String GEN_AI_USAGE_INPUT_TOKENS_DETAILS = "gen_ai.usage.input_tokens.details";
    public static final String GEN_AI_USAGE_INPUT_TOKENS_AUDIO = "gen_ai.usage.input_tokens.audio";
    public static final String GEN_AI_USAGE_INPUT_TOKENS_CACHE_INPUT = "gen_ai.usage.input_tokens.cache_input";
    public static final String GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ = "gen_ai.usage.input_tokens.cache_read";
    public static final String GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE = "gen_ai.usage.input_tokens.cache_write";

    // ── GenAI Messages ───────────────────────────────────────────────
    public static final String LLM_INPUT_MESSAGES = "gen_ai.input.messages";
    public static final String LLM_OUTPUT_MESSAGES = "gen_ai.output.messages";
    public static final String GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions";

    // ── GenAI Tools ──────────────────────────────────────────────────
    public static final String GEN_AI_TOOL_NAME = "gen_ai.tool.name";
    public static final String GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description";
    public static final String GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id";
    public static final String GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments";
    public static final String GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result";
    public static final String GEN_AI_TOOL_TYPE = "gen_ai.tool.type";
    public static final String GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions";
    public static final String GEN_AI_TOOL_CALL = "gen_ai.tool.call";
    public static final String GEN_AI_TOOL_PARAMETERS = "gen_ai.tool.parameters";

    // ── GenAI Cost ───────────────────────────────────────────────────
    public static final String GEN_AI_COST_TOTAL = "gen_ai.cost.total";
    public static final String GEN_AI_COST_INPUT = "gen_ai.cost.input";
    public static final String GEN_AI_COST_OUTPUT = "gen_ai.cost.output";
    public static final String GEN_AI_COST_CACHE_WRITE = "gen_ai.cost.cache_write";

    // ── GenAI Prompts ────────────────────────────────────────────────
    public static final String GEN_AI_PROMPTS = "gen_ai.prompts";
    public static final String GEN_AI_PROMPT_NAME = "gen_ai.prompt.name";
    public static final String GEN_AI_PROMPT_VENDOR = "gen_ai.prompt.vendor";
    public static final String GEN_AI_PROMPT_ID = "gen_ai.prompt.id";
    public static final String GEN_AI_PROMPT_URL = "gen_ai.prompt.url";
    public static final String GEN_AI_PROMPT_TEMPLATE_NAME = "gen_ai.prompt.template.name";
    public static final String GEN_AI_PROMPT_TEMPLATE_VERSION = "gen_ai.prompt.template.version";
    public static final String GEN_AI_PROMPT_TEMPLATE_LABEL = "gen_ai.prompt.template.label";
    public static final String GEN_AI_PROMPT_TEMPLATE_VARIABLES = "gen_ai.prompt.template.variables";

    // ── GenAI Conversation ───────────────────────────────────────────
    public static final String GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id";

    // ── GenAI Agent ──────────────────────────────────────────────────
    public static final String GEN_AI_AGENT_ID = "gen_ai.agent.id";
    public static final String GEN_AI_AGENT_NAME = "gen_ai.agent.name";
    public static final String GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description";
    public static final String GEN_AI_AGENT_GRAPH_NODE_ID = "gen_ai.agent.graph.node_id";
    public static final String GEN_AI_AGENT_GRAPH_NODE_NAME = "gen_ai.agent.graph.node_name";
    public static final String GEN_AI_AGENT_GRAPH_PARENT_NODE_ID = "gen_ai.agent.graph.parent_node_id";

    // ── GenAI Evaluation ─────────────────────────────────────────────
    public static final String GEN_AI_EVALUATION_NAME = "gen_ai.evaluation.name";
    public static final String GEN_AI_EVALUATION_SCORE_VALUE = "gen_ai.evaluation.score.value";
    public static final String GEN_AI_EVALUATION_SCORE_LABEL = "gen_ai.evaluation.score.label";
    public static final String GEN_AI_EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation";
    public static final String GEN_AI_EVALUATION_TARGET_SPAN_ID = "gen_ai.evaluation.target_span_id";

    // ── GenAI Embeddings ─────────────────────────────────────────────
    public static final String GEN_AI_EMBEDDINGS_DIMENSION_COUNT = "gen_ai.embeddings.dimension.count";
    public static final String GEN_AI_REQUEST_ENCODING_FORMATS = "gen_ai.request.encoding_formats";
    public static final String GEN_AI_EMBEDDINGS_VECTORS = "gen_ai.embeddings.vectors";

    // ── GenAI Retrieval ──────────────────────────────────────────────
    public static final String GEN_AI_RETRIEVAL_DOCUMENTS = "gen_ai.retrieval.documents";
    public static final String GEN_AI_RETRIEVAL_QUERY = "gen_ai.retrieval.query";
    public static final String GEN_AI_RETRIEVAL_TOP_K = "gen_ai.retrieval.top_k";

    // ── GenAI Reranker ───────────────────────────────────────────────
    public static final String GEN_AI_RERANKER_MODEL = "gen_ai.reranker.model";
    public static final String GEN_AI_RERANKER_QUERY = "gen_ai.reranker.query";
    public static final String GEN_AI_RERANKER_TOP_N = "gen_ai.reranker.top_n";
    public static final String GEN_AI_RERANKER_INPUT_DOCUMENTS = "gen_ai.reranker.input_documents";
    public static final String GEN_AI_RERANKER_OUTPUT_DOCUMENTS = "gen_ai.reranker.output_documents";

    // ── GenAI Guardrails ─────────────────────────────────────────────
    public static final String GEN_AI_GUARDRAIL_NAME = "gen_ai.guardrail.name";
    public static final String GEN_AI_GUARDRAIL_TYPE = "gen_ai.guardrail.type";
    public static final String GEN_AI_GUARDRAIL_RESULT = "gen_ai.guardrail.result";
    public static final String GEN_AI_GUARDRAIL_SCORE = "gen_ai.guardrail.score";
    public static final String GEN_AI_GUARDRAIL_CATEGORIES = "gen_ai.guardrail.categories";
    public static final String GEN_AI_GUARDRAIL_MODIFIED_OUTPUT = "gen_ai.guardrail.modified_output";

    // ── GenAI Performance / Streaming ────────────────────────────────
    public static final String GEN_AI_CLIENT_OPERATION_DURATION = "gen_ai.client.operation.duration";
    public static final String GEN_AI_SERVER_TIME_TO_FIRST_TOKEN = "gen_ai.server.time_to_first_token";
    public static final String GEN_AI_SERVER_TIME_PER_OUTPUT_TOKEN = "gen_ai.server.time_per_output_token";
    public static final String GEN_AI_SERVER_QUEUE_TIME = "gen_ai.server.queue_time";

    // ── GenAI Voice / Conversation ───────────────────────────────────
    public static final String GEN_AI_VOICE_CALL_ID = "gen_ai.voice.call_id";
    public static final String GEN_AI_VOICE_PROVIDER = "gen_ai.voice.provider";
    public static final String GEN_AI_VOICE_CALL_DURATION_SECS = "gen_ai.voice.call_duration_secs";
    public static final String GEN_AI_VOICE_ENDED_REASON = "gen_ai.voice.ended_reason";
    public static final String GEN_AI_VOICE_FROM_NUMBER = "gen_ai.voice.from_number";
    public static final String GEN_AI_VOICE_TO_NUMBER = "gen_ai.voice.to_number";
    public static final String GEN_AI_VOICE_CHANNEL_TYPE = "gen_ai.voice.channel_type";
    public static final String GEN_AI_VOICE_TRANSCRIPT = "gen_ai.voice.transcript";
    public static final String GEN_AI_VOICE_RECORDING_URL = "gen_ai.voice.recording.url";
    public static final String GEN_AI_VOICE_RECORDING_STEREO_URL = "gen_ai.voice.recording.stereo_url";
    public static final String GEN_AI_VOICE_RECORDING_CUSTOMER_URL = "gen_ai.voice.recording.customer_url";
    public static final String GEN_AI_VOICE_RECORDING_ASSISTANT_URL = "gen_ai.voice.recording.assistant_url";
    public static final String GEN_AI_VOICE_STT_MODEL = "gen_ai.voice.stt.model";
    public static final String GEN_AI_VOICE_STT_PROVIDER = "gen_ai.voice.stt.provider";
    public static final String GEN_AI_VOICE_STT_LANGUAGE = "gen_ai.voice.stt.language";
    public static final String GEN_AI_VOICE_TTS_MODEL = "gen_ai.voice.tts.model";
    public static final String GEN_AI_VOICE_TTS_PROVIDER = "gen_ai.voice.tts.provider";
    public static final String GEN_AI_VOICE_TTS_VOICE_ID = "gen_ai.voice.tts.voice_id";
    public static final String GEN_AI_VOICE_LATENCY_MODEL_AVG_MS = "gen_ai.voice.latency.model_avg_ms";
    public static final String GEN_AI_VOICE_LATENCY_VOICE_AVG_MS = "gen_ai.voice.latency.voice_avg_ms";
    public static final String GEN_AI_VOICE_LATENCY_TRANSCRIBER_AVG_MS = "gen_ai.voice.latency.transcriber_avg_ms";
    public static final String GEN_AI_VOICE_LATENCY_TURN_AVG_MS = "gen_ai.voice.latency.turn_avg_ms";
    public static final String GEN_AI_VOICE_LATENCY_TTFB_MS = "gen_ai.voice.latency.ttfb_ms";
    public static final String GEN_AI_VOICE_INTERRUPTIONS_USER_COUNT = "gen_ai.voice.interruptions.user_count";
    public static final String GEN_AI_VOICE_INTERRUPTIONS_ASSISTANT_COUNT = "gen_ai.voice.interruptions.assistant_count";
    public static final String GEN_AI_VOICE_COST_TOTAL = "gen_ai.voice.cost.total";
    public static final String GEN_AI_VOICE_COST_STT = "gen_ai.voice.cost.stt";
    public static final String GEN_AI_VOICE_COST_TTS = "gen_ai.voice.cost.tts";
    public static final String GEN_AI_VOICE_COST_LLM = "gen_ai.voice.cost.llm";
    public static final String GEN_AI_VOICE_COST_TELEPHONY = "gen_ai.voice.cost.telephony";

    // ── GenAI Image Generation ───────────────────────────────────────
    public static final String GEN_AI_IMAGE_PROMPT = "gen_ai.image.prompt";
    public static final String GEN_AI_IMAGE_NEGATIVE_PROMPT = "gen_ai.image.negative_prompt";
    public static final String GEN_AI_IMAGE_WIDTH = "gen_ai.image.width";
    public static final String GEN_AI_IMAGE_HEIGHT = "gen_ai.image.height";
    public static final String GEN_AI_IMAGE_SIZE = "gen_ai.image.size";
    public static final String GEN_AI_IMAGE_QUALITY = "gen_ai.image.quality";
    public static final String GEN_AI_IMAGE_STYLE = "gen_ai.image.style";
    public static final String GEN_AI_IMAGE_STEPS = "gen_ai.image.steps";
    public static final String GEN_AI_IMAGE_GUIDANCE_SCALE = "gen_ai.image.guidance_scale";
    public static final String GEN_AI_IMAGE_SEED = "gen_ai.image.seed";
    public static final String GEN_AI_IMAGE_FORMAT = "gen_ai.image.format";
    public static final String GEN_AI_IMAGE_COUNT = "gen_ai.image.count";
    public static final String GEN_AI_IMAGE_REVISED_PROMPT = "gen_ai.image.revised_prompt";
    public static final String GEN_AI_IMAGE_OUTPUT_URLS = "gen_ai.image.output_urls";

    // ── GenAI Computer Use ───────────────────────────────────────────
    public static final String GEN_AI_COMPUTER_USE_ACTION = "gen_ai.computer_use.action";
    public static final String GEN_AI_COMPUTER_USE_COORDINATE_X = "gen_ai.computer_use.coordinate_x";
    public static final String GEN_AI_COMPUTER_USE_COORDINATE_Y = "gen_ai.computer_use.coordinate_y";
    public static final String GEN_AI_COMPUTER_USE_TEXT = "gen_ai.computer_use.text";
    public static final String GEN_AI_COMPUTER_USE_KEY = "gen_ai.computer_use.key";
    public static final String GEN_AI_COMPUTER_USE_BUTTON = "gen_ai.computer_use.button";
    public static final String GEN_AI_COMPUTER_USE_SCROLL_DIRECTION = "gen_ai.computer_use.scroll_direction";
    public static final String GEN_AI_COMPUTER_USE_SCROLL_AMOUNT = "gen_ai.computer_use.scroll_amount";
    public static final String GEN_AI_COMPUTER_USE_SCREENSHOT = "gen_ai.computer_use.screenshot";
    public static final String GEN_AI_COMPUTER_USE_ENVIRONMENT = "gen_ai.computer_use.environment";
    public static final String GEN_AI_COMPUTER_USE_VIEWPORT_WIDTH = "gen_ai.computer_use.viewport_width";
    public static final String GEN_AI_COMPUTER_USE_VIEWPORT_HEIGHT = "gen_ai.computer_use.viewport_height";
    public static final String GEN_AI_COMPUTER_USE_CURRENT_URL = "gen_ai.computer_use.current_url";
    public static final String GEN_AI_COMPUTER_USE_ELEMENT_SELECTOR = "gen_ai.computer_use.element_selector";
    public static final String GEN_AI_COMPUTER_USE_RESULT = "gen_ai.computer_use.result";

    // ── GenAI Audio ──────────────────────────────────────────────────
    public static final String GEN_AI_AUDIO_URL = "gen_ai.audio.url";
    public static final String GEN_AI_AUDIO_MIME_TYPE = "gen_ai.audio.mime_type";
    public static final String GEN_AI_AUDIO_TRANSCRIPT = "gen_ai.audio.transcript";
    public static final String GEN_AI_AUDIO_DURATION_SECS = "gen_ai.audio.duration_secs";
    public static final String GEN_AI_AUDIO_LANGUAGE = "gen_ai.audio.language";

    // ── GenAI Simulator ──────────────────────────────────────────────
    public static final String GEN_AI_SIMULATOR_RUN_TEST_ID = "gen_ai.simulator.run_test_id";
    public static final String GEN_AI_SIMULATOR_TEST_EXECUTION_ID = "gen_ai.simulator.test_execution_id";
    public static final String GEN_AI_SIMULATOR_CALL_EXECUTION_ID = "gen_ai.simulator.call_execution_id";
    public static final String GEN_AI_SIMULATOR_IS_SIMULATOR_TRACE = "gen_ai.simulator.is_simulator_trace";

    // ── Input / Output (OpenInference compatible) ────────────────────
    public static final String INPUT_VALUE = "input.value";
    public static final String INPUT_MIME_TYPE = "input.mime_type";
    public static final String OUTPUT_VALUE = "output.value";
    public static final String OUTPUT_MIME_TYPE = "output.mime_type";
    public static final String GEN_AI_INPUT_IMAGES = "gen_ai.input.images";

    // ── Raw Input/Output (FI-specific) ───────────────────────────────
    public static final String RAW_INPUT = "fi.raw_input";
    public static final String RAW_OUTPUT = "fi.raw_output";

    // ── Error ────────────────────────────────────────────────────────
    public static final String ERROR_TYPE = "error.type";
    public static final String ERROR_MESSAGE = "error.message";

    // ── Metadata / Tags / Session ────────────────────────────────────
    public static final String METADATA = "metadata";
    public static final String TAG_TAGS = "tag.tags";
    public static final String SESSION_ID = "session.id";
    public static final String USER_ID = "user.id";

    // ── Embedding (OpenInference) ────────────────────────────────────
    public static final String EMBEDDING_EMBEDDINGS = "embedding.embeddings";
    public static final String EMBEDDING_MODEL_NAME = "embedding.model_name";
    public static final String EMBEDDING_TEXT = "embedding.text";
    public static final String EMBEDDING_VECTOR = "embedding.vector";
    public static final String EMBEDDING_VECTOR_COUNT = "embedding.vector_count";
    public static final String EMBEDDING_DIMENSIONS = "embedding.dimensions";
    public static final String EMBEDDING_INPUT_TEXT = "embedding.input_text";

    // ── Retrieval (OpenInference) ────────────────────────────────────
    public static final String RETRIEVAL_DOCUMENTS = "retrieval.documents";

    // ── Retriever Attributes ─────────────────────────────────────────
    public static final String RETRIEVER_NAME = "retriever.name";
    public static final String RETRIEVER_QUERY = "retriever.query";
    public static final String RETRIEVER_DOCUMENTS = "retriever.documents";
    public static final String RETRIEVER_TOP_K = "retriever.top_k";

    // ── Document (OpenInference) ─────────────────────────────────────
    public static final String DOCUMENT_ID = "document.id";
    public static final String DOCUMENT_CONTENT = "document.content";
    public static final String DOCUMENT_METADATA = "document.metadata";
    public static final String DOCUMENT_SCORE = "document.score";

    // ── Reranker (OpenInference) ─────────────────────────────────────
    public static final String RERANKER_INPUT_DOCUMENTS = "reranker.input_documents";
    public static final String RERANKER_OUTPUT_DOCUMENTS = "reranker.output_documents";
    public static final String RERANKER_QUERY = "reranker.query";
    public static final String RERANKER_MODEL_NAME = "reranker.model_name";
    public static final String RERANKER_TOP_K = "reranker.top_k";

    // ── Message (OpenInference) ──────────────────────────────────────
    public static final String MESSAGE_ROLE = "message.role";
    public static final String MESSAGE_CONTENT = "message.content";
    public static final String MESSAGE_CONTENTS = "message.contents";
    public static final String MESSAGE_NAME = "message.name";
    public static final String MESSAGE_TOOL_CALLS = "message.tool_calls";
    public static final String MESSAGE_FUNCTION_CALL_NAME = "message.function_call_name";
    public static final String MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = "message.function_call_arguments_json";
    public static final String MESSAGE_TOOL_CALL_ID = "message.tool_call_id";

    // ── Message Content (OpenInference) ──────────────────────────────
    public static final String MESSAGE_CONTENT_TYPE = "message_content.type";
    public static final String MESSAGE_CONTENT_TEXT = "message_content.text";
    public static final String MESSAGE_CONTENT_IMAGE = "message_content.image";
    public static final String MESSAGE_CONTENT_AUDIO = "message_content.audio";
    public static final String MESSAGE_AUDIO_TRANSCRIPT = "message_content.audio.transcript";
    public static final String MESSAGE_CONTENT_VIDEO = "message_content.video";

    // ── Image (OpenInference) ────────────────────────────────────────
    public static final String IMAGE_URL = "image.url";

    // ── Audio (OpenInference) ────────────────────────────────────────
    public static final String AUDIO_URL = "audio.url";
    public static final String AUDIO_MIME_TYPE = "audio.mime_type";
    public static final String AUDIO_TRANSCRIPT = "audio.transcript";

    // ── Tool Call (OpenInference) ────────────────────────────────────
    public static final String TOOL_CALL_ID = "tool_call.id";
    public static final String TOOL_CALL_FUNCTION_NAME = "tool_call.function.name";
    public static final String TOOL_CALL_FUNCTION_ARGUMENTS = "tool_call.function.arguments";
    public static final String TOOL_JSON_SCHEMA = "tool.json_schema";

    // ── Tool/Function Attributes (legacy) ────────────────────────────
    public static final String TOOL_NAME = "tool.name";
    public static final String TOOL_DESCRIPTION = "tool.description";
    public static final String TOOL_PARAMETERS = "tool.parameters";
    public static final String TOOL_RESULT = "tool.result";

    // ── Agent Attributes (legacy) ────────────────────────────────────
    public static final String AGENT_NAME = "agent.name";
    public static final String AGENT_TYPE = "agent.type";

    // ── Chain Attributes ─────────────────────────────────────────────
    public static final String CHAIN_NAME = "chain.name";
    public static final String CHAIN_TYPE = "chain.type";

    // ── Graph ────────────────────────────────────────────────────────
    public static final String GRAPH_NODE_ID = "graph.node.id";
    public static final String GRAPH_NODE_NAME = "graph.node.name";
    public static final String GRAPH_NODE_PARENT_ID = "graph.node.parent_id";

    // ── Server / Infrastructure ──────────────────────────────────────
    public static final String SERVER_ADDRESS = "server.address";
    public static final String SERVER_PORT = "server.port";

    // ── VectorDB ─────────────────────────────────────────────────────
    public static final String DB_SYSTEM = "db.system";
    public static final String DB_OPERATION_NAME = "db.operation.name";
    public static final String DB_NAMESPACE = "db.namespace";
    public static final String DB_VECTOR_QUERY_TOP_K = "db.vector.query.top_k";
    public static final String DB_VECTOR_QUERY_FILTER = "db.vector.query.filter";
    public static final String DB_VECTOR_QUERY_INCLUDE_METADATA = "db.vector.query.include_metadata";
    public static final String DB_VECTOR_QUERY_INCLUDE_VECTORS = "db.vector.query.include_vectors";
    public static final String DB_VECTOR_QUERY_SCORE_THRESHOLD = "db.vector.query.score_threshold";
    public static final String DB_VECTOR_QUERY_METRIC = "db.vector.query.metric";
    public static final String DB_VECTOR_RESULTS_COUNT = "db.vector.results.count";
    public static final String DB_VECTOR_RESULTS_SCORES = "db.vector.results.scores";
    public static final String DB_VECTOR_RESULTS_IDS = "db.vector.results.ids";
    public static final String DB_VECTOR_UPSERT_COUNT = "db.vector.upsert.count";
    public static final String DB_VECTOR_UPSERT_DIMENSIONS = "db.vector.upsert.dimensions";
    public static final String DB_VECTOR_DELETE_COUNT = "db.vector.delete.count";
    public static final String DB_VECTOR_DELETE_ALL = "db.vector.delete.all";
    public static final String DB_VECTOR_INDEX_NAME = "db.vector.index.name";
    public static final String DB_VECTOR_COLLECTION_NAME = "db.vector.collection.name";
    public static final String DB_VECTOR_INDEX_METRIC = "db.vector.index.metric";
    public static final String DB_VECTOR_INDEX_DIMENSIONS = "db.vector.index.dimensions";
    public static final String DB_VECTOR_NAMESPACE = "db.vector.namespace";

    // Private constructor to prevent instantiation
    private SemanticConventions() {
        throw new UnsupportedOperationException("Utility class cannot be instantiated");
    }
}
