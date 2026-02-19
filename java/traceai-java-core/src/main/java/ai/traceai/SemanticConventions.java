package ai.traceai;

/**
 * GenAI semantic conventions for AI observability.
 * Based on OpenTelemetry GenAI semantic conventions.
 *
 * @see <a href="https://opentelemetry.io/docs/specs/semconv/gen-ai/">GenAI Semantic Conventions</a>
 */
public final class SemanticConventions {

    // Span Kinds
    public static final String FI_SPAN_KIND = "fi.span.kind";

    // LLM System/Provider
    public static final String LLM_SYSTEM = "llm.system";
    public static final String LLM_PROVIDER = "llm.provider";

    // Model Names
    public static final String LLM_MODEL_NAME = "llm.model_name";
    public static final String LLM_REQUEST_MODEL = "llm.request.model";
    public static final String LLM_RESPONSE_MODEL = "llm.response.model";

    // Token Counts
    public static final String LLM_TOKEN_COUNT_PROMPT = "llm.token_count.prompt";
    public static final String LLM_TOKEN_COUNT_COMPLETION = "llm.token_count.completion";
    public static final String LLM_TOKEN_COUNT_TOTAL = "llm.token_count.total";

    // Messages
    public static final String LLM_INPUT_MESSAGES = "llm.input_messages";
    public static final String LLM_OUTPUT_MESSAGES = "llm.output_messages";

    // Request Parameters
    public static final String LLM_REQUEST_TEMPERATURE = "llm.request.temperature";
    public static final String LLM_REQUEST_TOP_P = "llm.request.top_p";
    public static final String LLM_REQUEST_MAX_TOKENS = "llm.request.max_tokens";
    public static final String LLM_REQUEST_STOP_SEQUENCES = "llm.request.stop_sequences";

    // Response
    public static final String LLM_RESPONSE_FINISH_REASON = "llm.response.finish_reason";
    public static final String LLM_RESPONSE_ID = "llm.response.id";

    // Input/Output Values
    public static final String INPUT_VALUE = "input.value";
    public static final String INPUT_MIME_TYPE = "input.mime_type";
    public static final String OUTPUT_VALUE = "output.value";
    public static final String OUTPUT_MIME_TYPE = "output.mime_type";

    // Raw Input/Output (FI-specific)
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
