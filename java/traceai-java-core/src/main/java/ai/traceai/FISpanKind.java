package ai.traceai;

/**
 * Span kinds for AI operations in TraceAI.
 * These values are set as attributes on spans to identify the type of AI operation.
 */
public enum FISpanKind {
    /**
     * Represents a Large Language Model inference operation.
     * Used for chat completions, text generation, etc.
     */
    LLM("LLM"),

    /**
     * Represents a chain of operations.
     * Used for sequential pipelines of AI operations.
     */
    CHAIN("CHAIN"),

    /**
     * Represents an AI agent operation.
     * Used for autonomous agents that make decisions.
     */
    AGENT("AGENT"),

    /**
     * Represents a tool/function call operation.
     * Used when an LLM invokes external tools.
     */
    TOOL("TOOL"),

    /**
     * Represents an embedding generation operation.
     * Used for text-to-vector conversions.
     */
    EMBEDDING("EMBEDDING"),

    /**
     * Represents a document retrieval operation.
     * Used in RAG pipelines for fetching relevant documents.
     */
    RETRIEVER("RETRIEVER"),

    /**
     * Represents a reranking operation.
     * Used to reorder retrieved documents by relevance.
     */
    RERANKER("RERANKER"),

    /**
     * Represents a guardrail check operation.
     * Used for input/output validation and safety checks.
     */
    GUARDRAIL("GUARDRAIL"),

    /**
     * Represents a generic workflow operation.
     * Used for custom pipeline steps.
     */
    WORKFLOW("WORKFLOW"),

    /**
     * Represents an unknown or unspecified operation type.
     */
    UNKNOWN("UNKNOWN"),

    /**
     * Represents an evaluation/assessment operation.
     * Used for quality scoring and assessment of AI outputs.
     */
    EVALUATOR("EVALUATOR"),

    /**
     * Represents a conversation operation.
     * Used for voice and conversational AI sessions.
     */
    CONVERSATION("CONVERSATION"),

    /**
     * Represents a vector database operation.
     * Used for vector store queries, upserts, and deletes.
     */
    VECTOR_DB("VECTOR_DB");

    private final String value;

    FISpanKind(String value) {
        this.value = value;
    }

    /**
     * Returns the string value of this span kind.
     * @return the span kind value
     */
    public String getValue() {
        return value;
    }

    /**
     * Creates a FISpanKind from a string value.
     * @param value the string value
     * @return the corresponding FISpanKind, or null if not found
     */
    public static FISpanKind fromValue(String value) {
        if (value == null) {
            return null;
        }
        for (FISpanKind kind : values()) {
            if (kind.value.equalsIgnoreCase(value)) {
                return kind;
            }
        }
        return null;
    }

    @Override
    public String toString() {
        return value;
    }
}
