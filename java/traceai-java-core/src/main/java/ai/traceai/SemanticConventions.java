package ai.traceai;

/**
 * GenAI semantic conventions for AI observability.
 * Based on OpenTelemetry GenAI semantic conventions.
 *
 * @see <a href="https://opentelemetry.io/docs/specs/semconv/gen-ai/">GenAI Semantic Conventions</a>
 */
public final class SemanticConventions {

    // Span Kinds (FutureAGI extension — stays as fi.*)
    public static final String FI_SPAN_KIND = "fi.span.kind";

    // LLM System/Provider — both resolve to gen_ai.provider.name
    public static final String LLM_SYSTEM = "gen_ai.provider.name";
    public static final String LLM_PROVIDER = "gen_ai.provider.name";

    // Model Names
    public static final String LLM_MODEL_NAME = "gen_ai.request.model";
    public static final String LLM_REQUEST_MODEL = "gen_ai.request.model";
    public static final String LLM_RESPONSE_MODEL = "gen_ai.response.model";

    // Token Counts
    public static final String LLM_TOKEN_COUNT_PROMPT = "gen_ai.usage.input_tokens";
    public static final String LLM_TOKEN_COUNT_COMPLETION = "gen_ai.usage.output_tokens";
    public static final String LLM_TOKEN_COUNT_TOTAL = "gen_ai.usage.total_tokens";

    // Messages (JSON blob format)
    public static final String LLM_INPUT_MESSAGES = "gen_ai.input.messages";
    public static final String LLM_OUTPUT_MESSAGES = "gen_ai.output.messages";

    // Request Parameters
    public static final String LLM_REQUEST_TEMPERATURE = "gen_ai.request.temperature";
    public static final String LLM_REQUEST_TOP_P = "gen_ai.request.top_p";
    public static final String LLM_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens";
    public static final String LLM_REQUEST_STOP_SEQUENCES = "gen_ai.request.stop_sequences";

    // Response
    public static final String LLM_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reasons";
    public static final String LLM_RESPONSE_ID = "gen_ai.response.id";

    // New GenAI constants
    public static final String GEN_AI_OPERATION_NAME = "gen_ai.operation.name";
    public static final String GEN_AI_REQUEST_PARAMETERS = "gen_ai.request.parameters";
    public static final String GEN_AI_TOOL_DEFINITIONS = "gen_ai.tool.definitions";

    // Input/Output Values (generic OTEL — stays as-is)
    public static final String INPUT_VALUE = "input.value";
    public static final String INPUT_MIME_TYPE = "input.mime_type";
    public static final String OUTPUT_VALUE = "output.value";
    public static final String OUTPUT_MIME_TYPE = "output.mime_type";

    // Raw Input/Output (FI-specific — stays as-is)
    public static final String RAW_INPUT = "fi.raw_input";
    public static final String RAW_OUTPUT = "fi.raw_output";

    // Embedding Attributes
    public static final String EMBEDDING_MODEL_NAME = "embedding.model_name";
    public static final String EMBEDDING_VECTOR_COUNT = "embedding.vector_count";
    public static final String EMBEDDING_DIMENSIONS = "embedding.dimensions";
    public static final String EMBEDDING_INPUT_TEXT = "embedding.input_text";

    // Tool/Function Attributes
    public static final String TOOL_NAME = "tool.name";
    public static final String TOOL_DESCRIPTION = "tool.description";
    public static final String TOOL_PARAMETERS = "tool.parameters";
    public static final String TOOL_RESULT = "tool.result";

    // Agent Attributes
    public static final String AGENT_NAME = "agent.name";
    public static final String AGENT_TYPE = "agent.type";

    // Retriever Attributes
    public static final String RETRIEVER_NAME = "retriever.name";
    public static final String RETRIEVER_QUERY = "retriever.query";
    public static final String RETRIEVER_DOCUMENTS = "retriever.documents";
    public static final String RETRIEVER_TOP_K = "retriever.top_k";

    // Document Attributes
    public static final String DOCUMENT_ID = "document.id";
    public static final String DOCUMENT_CONTENT = "document.content";
    public static final String DOCUMENT_METADATA = "document.metadata";
    public static final String DOCUMENT_SCORE = "document.score";

    // Chain Attributes
    public static final String CHAIN_NAME = "chain.name";
    public static final String CHAIN_TYPE = "chain.type";

    // Error Attributes
    public static final String ERROR_TYPE = "error.type";
    public static final String ERROR_MESSAGE = "error.message";

    // Private constructor to prevent instantiation
    private SemanticConventions() {
        throw new UnsupportedOperationException("Utility class cannot be instantiated");
    }
}
