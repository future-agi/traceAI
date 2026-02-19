/**
 * Semantic conventions for TraceAI tracing
 */
export const SemanticAttributePrefixes = {
    input: "input",
    output: "output",
    llm: "llm",
    retrieval: "retrieval",
    reranker: "reranker",
    messages: "messages",
    message: "message",
    document: "document",
    embedding: "embedding",
    tool: "tool",
    tool_call: "tool_call",
    metadata: "metadata",
    tag: "tag",
    session: "session",
    user: "user",
    traceai: "traceai",
    fi: "fi",
    message_content: "message_content",
    image: "image",
    audio: "audio",
    prompt: "prompt",
};
export const LLMAttributePostfixes = {
    provider: "provider",
    system: "system",
    model_name: "model_name",
    token_count: "token_count",
    input_messages: "input_messages",
    output_messages: "output_messages",
    invocation_parameters: "invocation_parameters",
    prompts: "prompts",
    prompt_template: "prompt_template",
    function_call: "function_call",
    tools: "tools",
};
export const LLMPromptTemplateAttributePostfixes = {
    variables: "variables",
    template: "template",
};
export const RetrievalAttributePostfixes = {
    documents: "documents",
};
export const RerankerAttributePostfixes = {
    input_documents: "input_documents",
    output_documents: "output_documents",
    query: "query",
    model_name: "model_name",
    top_k: "top_k",
};
export const EmbeddingAttributePostfixes = {
    embeddings: "embeddings",
    text: "text",
    model_name: "model_name",
    vector: "vector",
};
export const ToolAttributePostfixes = {
    name: "name",
    description: "description",
    parameters: "parameters",
    json_schema: "json_schema",
};
export const MessageAttributePostfixes = {
    role: "role",
    content: "content",
    contents: "contents",
    name: "name",
    function_call_name: "function_call_name",
    function_call_arguments_json: "function_call_arguments_json",
    tool_calls: "tool_calls",
    tool_call_id: "tool_call_id",
};
export const MessageContentsAttributePostfixes = {
    type: "type",
    text: "text",
    image: "image",
};
export const ImageAttributesPostfixes = {
    url: "url",
};
export const ToolCallAttributePostfixes = {
    function_name: "function.name",
    function_arguments_json: "function.arguments",
    id: "id",
};
export const DocumentAttributePostfixes = {
    id: "id",
    content: "content",
    score: "score",
    metadata: "metadata",
};
export const TagAttributePostfixes = {
    tags: "tags",
};
export const SessionAttributePostfixes = {
    id: "id",
};
export const UserAttributePostfixes = {
    id: "id",
};
export const AudioAttributesPostfixes = {
    url: "url",
    mime_type: "mime_type",
    transcript: "transcript",
};
export const PromptAttributePostfixes = {
    vendor: "vendor",
    id: "id",
    url: "url",
};
/**
 * The input to any span
 */
export const INPUT_VALUE = `${SemanticAttributePrefixes.input}.value`;
export const INPUT_MIME_TYPE = `${SemanticAttributePrefixes.input}.mime_type`;
/**
 * The output of any span
 */
export const OUTPUT_VALUE = `${SemanticAttributePrefixes.output}.value`;
export const OUTPUT_MIME_TYPE = `${SemanticAttributePrefixes.output}.mime_type`;
/**
 * The messages sent to the LLM for completions
 * Typically seen in OpenAI chat completions
 * @see https://beta.openai.com/docs/api-reference/completions/create
 */
export const LLM_INPUT_MESSAGES = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.input_messages}`;
/**
 * The prompts sent to the LLM for completions
 * Typically seen in OpenAI legacy completions
 * @see https://beta.openai.com/docs/api-reference/completions/create
 */
export const LLM_PROMPTS = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.prompts}`;
/**
 * The JSON representation of the parameters passed to the LLM
 */
export const LLM_INVOCATION_PARAMETERS = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.invocation_parameters}`;
/**
 * The messages received from the LLM for completions
 * Typically seen in OpenAI chat completions
 * @see https://platform.openai.com/docs/api-reference/chat/object#choices-message
 */
export const LLM_OUTPUT_MESSAGES = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.output_messages}`;
/**
 * The name of the LLM model
 */
export const LLM_MODEL_NAME = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.model_name}`;
/**
 * The provider of the inferences. E.g. the cloud provider
 */
export const LLM_PROVIDER = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.provider}`;
/**
 * The AI product as identified by the client or server
 */
export const LLM_SYSTEM = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.system}`;
/** Token count for the completion by the llm */
export const LLM_TOKEN_COUNT_COMPLETION = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.completion`;
/** Token count for the reasoning steps in the completion */
export const LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.completion_details.reasoning`;
/** Token count for audio input generated by the model */
export const LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.completion_details.audio`;
/** Token count for the prompt to the llm */
export const LLM_TOKEN_COUNT_PROMPT = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.prompt`;
/** Token count for the tokens written to the cache */
export const LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.prompt_details.cache_write`;
/** Token count for the tokens retrieved from the cache */
export const LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.prompt_details.cache_read`;
/** Token count for audio input presented in the prompt */
export const LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.prompt_details.audio`;
/** Token count for the entire transaction with the llm */
export const LLM_TOKEN_COUNT_TOTAL = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.total`;
/**
 * The role that the LLM assumes the message is from
 * during the LLM invocation
 */
export const MESSAGE_ROLE = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.role}`;
/**
 * The name of the message. This is only used for role 'function' where the name
 * of the function is captured in the name field and the parameters are captured in the
 * content.
 */
export const MESSAGE_NAME = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.name}`;
/**
 * The tool calls generated by the model, such as function calls.
 */
export const MESSAGE_TOOL_CALLS = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.tool_calls}`;
/**
 * The id of the tool call on a "tool" role message
 */
export const MESSAGE_TOOL_CALL_ID = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.tool_call_id}`;
/**
 * tool_call.function.name
 */
export const TOOL_CALL_FUNCTION_NAME = `${SemanticAttributePrefixes.tool_call}.${ToolCallAttributePostfixes.function_name}`;
/**
 * tool_call.function.argument (JSON string)
 */
export const TOOL_CALL_FUNCTION_ARGUMENTS_JSON = `${SemanticAttributePrefixes.tool_call}.${ToolCallAttributePostfixes.function_arguments_json}`;
/**
 * The id of the tool call
 */
export const TOOL_CALL_ID = `${SemanticAttributePrefixes.tool_call}.${ToolCallAttributePostfixes.id}`;
/**
 * The LLM function call function name
 */
export const MESSAGE_FUNCTION_CALL_NAME = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.function_call_name}`;
/**
 * The LLM function call function arguments in a json string
 */
export const MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.function_call_arguments_json}`;
/**
 * The content of the message sent to the LLM
 */
export const MESSAGE_CONTENT = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.content}`;
/**
 * The array of contents for the message sent to the LLM. Each element of the array is
 * an `message_content` object.
 */
export const MESSAGE_CONTENTS = `${SemanticAttributePrefixes.message}.${MessageAttributePostfixes.contents}`;
/**
 * The type of content sent to the LLM
 */
export const MESSAGE_CONTENT_TYPE = `${SemanticAttributePrefixes.message_content}.${MessageContentsAttributePostfixes.type}`;
/**
 * The text content of the message sent to the LLM
 */
export const MESSAGE_CONTENT_TEXT = `${SemanticAttributePrefixes.message_content}.${MessageContentsAttributePostfixes.text}`;
/**
 * The image content of the message sent to the LLM
 */
export const MESSAGE_CONTENT_IMAGE = `${SemanticAttributePrefixes.message_content}.${MessageContentsAttributePostfixes.image}`;
/**
 * The http or base64 link to the image
 */
export const IMAGE_URL = `${SemanticAttributePrefixes.image}.${ImageAttributesPostfixes.url}`;
export const DOCUMENT_ID = `${SemanticAttributePrefixes.document}.${DocumentAttributePostfixes.id}`;
export const DOCUMENT_CONTENT = `${SemanticAttributePrefixes.document}.${DocumentAttributePostfixes.content}`;
export const DOCUMENT_SCORE = `${SemanticAttributePrefixes.document}.${DocumentAttributePostfixes.score}`;
export const DOCUMENT_METADATA = `${SemanticAttributePrefixes.document}.${DocumentAttributePostfixes.metadata}`;
/**
 * The text that was embedded to create the vector
 */
export const EMBEDDING_TEXT = `${SemanticAttributePrefixes.embedding}.${EmbeddingAttributePostfixes.text}`;
/**
 * The name of the model that was used to create the vector
 */
export const EMBEDDING_MODEL_NAME = `${SemanticAttributePrefixes.embedding}.${EmbeddingAttributePostfixes.model_name}`;
/**
 * The embedding vector. Typically a high dimensional vector of floats or ints
 */
export const EMBEDDING_VECTOR = `${SemanticAttributePrefixes.embedding}.${EmbeddingAttributePostfixes.vector}`;
/**
 * The embedding list root
 */
export const EMBEDDING_EMBEDDINGS = `${SemanticAttributePrefixes.embedding}.${EmbeddingAttributePostfixes.embeddings}`;
/**
 * The retrieval documents list root
 */
export const RETRIEVAL_DOCUMENTS = `${SemanticAttributePrefixes.retrieval}.${RetrievalAttributePostfixes.documents}`;
const PROMPT_TEMPLATE_PREFIX = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.prompt_template}`;
/**
 * The JSON representation of the variables used in the prompt template
 */
export const PROMPT_TEMPLATE_VARIABLES = `${PROMPT_TEMPLATE_PREFIX}.variables`;
/**
 * A prompt template
 */
export const PROMPT_TEMPLATE_TEMPLATE = `${PROMPT_TEMPLATE_PREFIX}.template`;
/**
 * The JSON representation of a function call of an LLM
 */
export const LLM_FUNCTION_CALL = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.function_call}`;
/**
 * List of tools that are advertised to the LLM to be able to call
 */
export const LLM_TOOLS = `${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.tools}`;
/**
 * The name of a tool
 */
export const TOOL_NAME = `${SemanticAttributePrefixes.tool}.${ToolAttributePostfixes.name}`;
/**
 * The description of a tool
 */
export const TOOL_DESCRIPTION = `${SemanticAttributePrefixes.tool}.${ToolAttributePostfixes.description}`;
/**
 * The parameters of the tool represented as a JSON string
 */
export const TOOL_PARAMETERS = `${SemanticAttributePrefixes.tool}.${ToolAttributePostfixes.parameters}`;
/**
 * The json schema of a tool input, It is RECOMMENDED that this be in the
 * OpenAI tool calling format: https://platform.openai.com/docs/assistants/tools
 */
export const TOOL_JSON_SCHEMA = `${SemanticAttributePrefixes.tool}.${ToolAttributePostfixes.json_schema}`;
/**
 * The session id of a trace. Used to correlate spans in a single session.
 */
export const SESSION_ID = `${SemanticAttributePrefixes.session}.${SessionAttributePostfixes.id}`;
/**
 * The user id of a trace. Used to correlate spans for a single user.
 */
export const USER_ID = `${SemanticAttributePrefixes.user}.${UserAttributePostfixes.id}`;
/**
 * The documents used as input to the reranker
 */
export const RERANKER_INPUT_DOCUMENTS = `${SemanticAttributePrefixes.reranker}.${RerankerAttributePostfixes.input_documents}`;
/**
 * The documents output by the reranker
 */
export const RERANKER_OUTPUT_DOCUMENTS = `${SemanticAttributePrefixes.reranker}.${RerankerAttributePostfixes.output_documents}`;
/**
 * The query string for the reranker
 */
export const RERANKER_QUERY = `${SemanticAttributePrefixes.reranker}.${RerankerAttributePostfixes.query}`;
/**
 * The model name for the reranker
 */
export const RERANKER_MODEL_NAME = `${SemanticAttributePrefixes.reranker}.${RerankerAttributePostfixes.model_name}`;
/**
 * The top k parameter for the reranker
 */
export const RERANKER_TOP_K = `${SemanticAttributePrefixes.reranker}.${RerankerAttributePostfixes.top_k}`;
/**
 * Metadata for a span, used to store user-defined key-value pairs
 */
export const METADATA = "metadata";
/**
 * A prompt template version
 */
export const PROMPT_TEMPLATE_VERSION = `${PROMPT_TEMPLATE_PREFIX}.version`;
/**
 * The tags associated with a span
 */
export const TAG_TAGS = `${SemanticAttributePrefixes.tag}.${TagAttributePostfixes.tags}`;
/**
 * The url of an audio file
 */
export const AUDIO_URL = `${SemanticAttributePrefixes.audio}.${AudioAttributesPostfixes.url}`;
/**
 * The audio mime type
 */
export const AUDIO_MIME_TYPE = `${SemanticAttributePrefixes.audio}.${AudioAttributesPostfixes.mime_type}`;
/**
 * The audio transcript as text
 */
export const AUDIO_TRANSCRIPT = `${SemanticAttributePrefixes.audio}.${AudioAttributesPostfixes.transcript}`;
/**
 * The vendor or origin of the prompt, e.g. a prompt library, a specialized service, etc.
 */
export const PROMPT_VENDOR = `${SemanticAttributePrefixes.prompt}.${PromptAttributePostfixes.vendor}`;
/**
 * A vendor-specific id used to locate the prompt
 */
export const PROMPT_ID = `${SemanticAttributePrefixes.prompt}.${PromptAttributePostfixes.id}`;
/**
 * A vendor-specific URL used to locate the prompt
 */
export const PROMPT_URL = `${SemanticAttributePrefixes.prompt}.${PromptAttributePostfixes.url}`;
export const SemanticConventions = {
    IMAGE_URL,
    INPUT_VALUE,
    INPUT_MIME_TYPE,
    OUTPUT_VALUE,
    OUTPUT_MIME_TYPE,
    LLM_INPUT_MESSAGES,
    LLM_OUTPUT_MESSAGES,
    LLM_MODEL_NAME,
    LLM_PROMPTS,
    LLM_INVOCATION_PARAMETERS,
    LLM_TOKEN_COUNT_COMPLETION,
    LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
    LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO,
    LLM_TOKEN_COUNT_PROMPT,
    LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE,
    LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
    LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO,
    LLM_TOKEN_COUNT_TOTAL,
    LLM_SYSTEM,
    LLM_PROVIDER,
    LLM_TOOLS,
    MESSAGE_ROLE,
    MESSAGE_NAME,
    MESSAGE_TOOL_CALLS,
    MESSAGE_TOOL_CALL_ID,
    TOOL_CALL_ID,
    TOOL_CALL_FUNCTION_NAME,
    TOOL_CALL_FUNCTION_ARGUMENTS_JSON,
    MESSAGE_FUNCTION_CALL_NAME,
    MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
    MESSAGE_CONTENT,
    MESSAGE_CONTENTS,
    MESSAGE_CONTENT_IMAGE,
    MESSAGE_CONTENT_TEXT,
    MESSAGE_CONTENT_TYPE,
    DOCUMENT_ID,
    DOCUMENT_CONTENT,
    DOCUMENT_SCORE,
    DOCUMENT_METADATA,
    EMBEDDING_EMBEDDINGS,
    EMBEDDING_TEXT,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_VECTOR,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_PARAMETERS,
    TOOL_JSON_SCHEMA,
    PROMPT_TEMPLATE_VARIABLES,
    PROMPT_TEMPLATE_TEMPLATE,
    PROMPT_TEMPLATE_VERSION,
    RERANKER_INPUT_DOCUMENTS,
    RERANKER_OUTPUT_DOCUMENTS,
    RERANKER_QUERY,
    RERANKER_MODEL_NAME,
    RERANKER_TOP_K,
    LLM_FUNCTION_CALL,
    RETRIEVAL_DOCUMENTS,
    SESSION_ID,
    USER_ID,
    METADATA,
    TAG_TAGS,
    FI_SPAN_KIND: `${SemanticAttributePrefixes.fi}.span.kind`,
    PROMPT_VENDOR,
    PROMPT_ID,
    PROMPT_URL,
    RAW_INPUT: "raw.input",
    RAW_OUTPUT: "raw.output",
    // Vector Database attributes
    DB_SYSTEM: "db.system",
    DB_OPERATION_NAME: "db.operation.name",
    DB_NAMESPACE: "db.namespace",
    DB_VECTOR_QUERY_TOP_K: "db.vector.query.top_k",
    DB_VECTOR_QUERY_FILTER: "db.vector.query.filter",
    DB_VECTOR_QUERY_INCLUDE_METADATA: "db.vector.query.include_metadata",
    DB_VECTOR_QUERY_INCLUDE_VECTORS: "db.vector.query.include_vectors",
    DB_VECTOR_QUERY_SCORE_THRESHOLD: "db.vector.query.score_threshold",
    DB_VECTOR_QUERY_METRIC: "db.vector.query.metric",
    DB_VECTOR_RESULTS_COUNT: "db.vector.results.count",
    DB_VECTOR_RESULTS_SCORES: "db.vector.results.scores",
    DB_VECTOR_RESULTS_IDS: "db.vector.results.ids",
    DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count",
    DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions",
    DB_VECTOR_DELETE_COUNT: "db.vector.delete.count",
    DB_VECTOR_DELETE_ALL: "db.vector.delete.all",
    DB_VECTOR_INDEX_NAME: "db.vector.index.name",
    DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name",
    DB_VECTOR_INDEX_METRIC: "db.vector.index.metric",
    DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions",
    DB_VECTOR_NAMESPACE: "db.vector.namespace",
};
export var FISpanKind;
(function (FISpanKind) {
    FISpanKind["LLM"] = "LLM";
    FISpanKind["CHAIN"] = "CHAIN";
    FISpanKind["TOOL"] = "TOOL";
    FISpanKind["RETRIEVER"] = "RETRIEVER";
    FISpanKind["RERANKER"] = "RERANKER";
    FISpanKind["EMBEDDING"] = "EMBEDDING";
    FISpanKind["AGENT"] = "AGENT";
    FISpanKind["GUARDRAIL"] = "GUARDRAIL";
    FISpanKind["EVALUATOR"] = "EVALUATOR";
    FISpanKind["VECTOR_DB"] = "VECTOR_DB";
    FISpanKind["UNKNOWN"] = "UNKNOWN";
})(FISpanKind || (FISpanKind = {}));
/**
 * An enum of common mime types. Not exhaustive.
 */
export var MimeType;
(function (MimeType) {
    MimeType["TEXT"] = "text/plain";
    MimeType["JSON"] = "application/json";
    MimeType["AUDIO_WAV"] = "audio/wav";
})(MimeType || (MimeType = {}));
export var LLMSystem;
(function (LLMSystem) {
    LLMSystem["OPENAI"] = "openai";
    LLMSystem["ANTHROPIC"] = "anthropic";
    LLMSystem["MISTRALAI"] = "mistralai";
    LLMSystem["COHERE"] = "cohere";
    LLMSystem["VERTEXAI"] = "vertexai";
    LLMSystem["AI21"] = "ai21";
    LLMSystem["META"] = "meta";
    LLMSystem["AMAZON"] = "amazon";
})(LLMSystem || (LLMSystem = {}));
export var LLMProvider;
(function (LLMProvider) {
    LLMProvider["OPENAI"] = "openai";
    LLMProvider["ANTHROPIC"] = "anthropic";
    LLMProvider["MISTRALAI"] = "mistralai";
    LLMProvider["COHERE"] = "cohere";
    // Cloud Providers of LLM systems
    LLMProvider["GOOGLE"] = "google";
    LLMProvider["AWS"] = "aws";
    LLMProvider["AZURE"] = "azure";
})(LLMProvider || (LLMProvider = {}));
/**
 * Vector Database Semantic Conventions
 *
 * Based on OpenTelemetry database semantic conventions with extensions
 * for vector-specific operations.
 */
// Semantic attribute prefixes for vector databases
export const VectorDBAttributePrefixes = {
    db: "db",
    dbVector: "db.vector",
    dbVectorQuery: "db.vector.query",
    dbVectorResults: "db.vector.results",
    dbVectorUpsert: "db.vector.upsert",
    dbVectorDelete: "db.vector.delete",
    dbVectorIndex: "db.vector.index",
    dbVectorCollection: "db.vector.collection",
};
// Core DB attributes (OTEL standard)
export const DB_SYSTEM = "db.system";
export const DB_OPERATION_NAME = "db.operation.name";
export const DB_NAMESPACE = "db.namespace";
// Query attributes
export const DB_VECTOR_QUERY_TOP_K = "db.vector.query.top_k";
export const DB_VECTOR_QUERY_FILTER = "db.vector.query.filter";
export const DB_VECTOR_QUERY_INCLUDE_METADATA = "db.vector.query.include_metadata";
export const DB_VECTOR_QUERY_INCLUDE_VECTORS = "db.vector.query.include_vectors";
export const DB_VECTOR_QUERY_SCORE_THRESHOLD = "db.vector.query.score_threshold";
export const DB_VECTOR_QUERY_METRIC = "db.vector.query.metric";
// Result attributes
export const DB_VECTOR_RESULTS_COUNT = "db.vector.results.count";
export const DB_VECTOR_RESULTS_SCORES = "db.vector.results.scores";
export const DB_VECTOR_RESULTS_IDS = "db.vector.results.ids";
// Upsert/Insert attributes
export const DB_VECTOR_UPSERT_COUNT = "db.vector.upsert.count";
export const DB_VECTOR_UPSERT_DIMENSIONS = "db.vector.upsert.dimensions";
// Delete attributes
export const DB_VECTOR_DELETE_COUNT = "db.vector.delete.count";
export const DB_VECTOR_DELETE_ALL = "db.vector.delete.all";
// Index/Collection attributes
export const DB_VECTOR_INDEX_NAME = "db.vector.index.name";
export const DB_VECTOR_COLLECTION_NAME = "db.vector.collection.name";
export const DB_VECTOR_INDEX_METRIC = "db.vector.index.metric";
export const DB_VECTOR_INDEX_DIMENSIONS = "db.vector.index.dimensions";
// Namespace
export const DB_VECTOR_NAMESPACE = "db.vector.namespace";
/**
 * Vector Database Semantic Conventions object
 */
export const VectorDBSemanticConventions = {
    // Core DB attributes
    DB_SYSTEM,
    DB_OPERATION_NAME,
    DB_NAMESPACE,
    // Query attributes
    DB_VECTOR_QUERY_TOP_K,
    DB_VECTOR_QUERY_FILTER,
    DB_VECTOR_QUERY_INCLUDE_METADATA,
    DB_VECTOR_QUERY_INCLUDE_VECTORS,
    DB_VECTOR_QUERY_SCORE_THRESHOLD,
    DB_VECTOR_QUERY_METRIC,
    // Result attributes
    DB_VECTOR_RESULTS_COUNT,
    DB_VECTOR_RESULTS_SCORES,
    DB_VECTOR_RESULTS_IDS,
    // Upsert/Insert attributes
    DB_VECTOR_UPSERT_COUNT,
    DB_VECTOR_UPSERT_DIMENSIONS,
    // Delete attributes
    DB_VECTOR_DELETE_COUNT,
    DB_VECTOR_DELETE_ALL,
    // Index/Collection attributes
    DB_VECTOR_INDEX_NAME,
    DB_VECTOR_COLLECTION_NAME,
    DB_VECTOR_INDEX_METRIC,
    DB_VECTOR_INDEX_DIMENSIONS,
    // Namespace
    DB_VECTOR_NAMESPACE,
};
/**
 * Supported vector database systems
 */
export var VectorDBSystem;
(function (VectorDBSystem) {
    VectorDBSystem["CHROMADB"] = "chromadb";
    VectorDBSystem["PINECONE"] = "pinecone";
    VectorDBSystem["QDRANT"] = "qdrant";
    VectorDBSystem["WEAVIATE"] = "weaviate";
    VectorDBSystem["MILVUS"] = "milvus";
    VectorDBSystem["PGVECTOR"] = "pgvector";
    VectorDBSystem["REDIS"] = "redis";
    VectorDBSystem["MONGODB"] = "mongodb";
    VectorDBSystem["LANCEDB"] = "lancedb";
})(VectorDBSystem || (VectorDBSystem = {}));
/**
 * Vector distance/similarity metrics
 */
export var VectorMetric;
(function (VectorMetric) {
    VectorMetric["COSINE"] = "cosine";
    VectorMetric["EUCLIDEAN"] = "euclidean";
    VectorMetric["DOT_PRODUCT"] = "dot_product";
    VectorMetric["L2"] = "l2";
    VectorMetric["IP"] = "ip";
    VectorMetric["HAMMING"] = "hamming";
})(VectorMetric || (VectorMetric = {}));
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiU2VtYW50aWNDb252ZW50aW9ucy5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbIlNlbWFudGljQ29udmVudGlvbnMudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6IkFBQUE7O0dBRUc7QUFFSCxNQUFNLENBQUMsTUFBTSx5QkFBeUIsR0FBRztJQUNyQyxLQUFLLEVBQUUsT0FBTztJQUNkLE1BQU0sRUFBRSxRQUFRO0lBQ2hCLEdBQUcsRUFBRSxLQUFLO0lBQ1YsU0FBUyxFQUFFLFdBQVc7SUFDdEIsUUFBUSxFQUFFLFVBQVU7SUFDcEIsUUFBUSxFQUFFLFVBQVU7SUFDcEIsT0FBTyxFQUFFLFNBQVM7SUFDbEIsUUFBUSxFQUFFLFVBQVU7SUFDcEIsU0FBUyxFQUFFLFdBQVc7SUFDdEIsSUFBSSxFQUFFLE1BQU07SUFDWixTQUFTLEVBQUUsV0FBVztJQUN0QixRQUFRLEVBQUUsVUFBVTtJQUNwQixHQUFHLEVBQUUsS0FBSztJQUNWLE9BQU8sRUFBRSxTQUFTO0lBQ2xCLElBQUksRUFBRSxNQUFNO0lBQ1osT0FBTyxFQUFFLFNBQVM7SUFDbEIsRUFBRSxFQUFFLElBQUk7SUFDUixlQUFlLEVBQUUsaUJBQWlCO0lBQ2xDLEtBQUssRUFBRSxPQUFPO0lBQ2QsS0FBSyxFQUFFLE9BQU87SUFDZCxNQUFNLEVBQUUsUUFBUTtDQUNSLENBQUM7QUFFWCxNQUFNLENBQUMsTUFBTSxxQkFBcUIsR0FBRztJQUNuQyxRQUFRLEVBQUUsVUFBVTtJQUNwQixNQUFNLEVBQUUsUUFBUTtJQUNoQixVQUFVLEVBQUUsWUFBWTtJQUN4QixXQUFXLEVBQUUsYUFBYTtJQUMxQixjQUFjLEVBQUUsZ0JBQWdCO0lBQ2hDLGVBQWUsRUFBRSxpQkFBaUI7SUFDbEMscUJBQXFCLEVBQUUsdUJBQXVCO0lBQzlDLE9BQU8sRUFBRSxTQUFTO0lBQ2xCLGVBQWUsRUFBRSxpQkFBaUI7SUFDbEMsYUFBYSxFQUFFLGVBQWU7SUFDOUIsS0FBSyxFQUFFLE9BQU87Q0FDTixDQUFDO0FBRVgsTUFB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dUI7SUFDdkIsd0JBQXdCO0lBQ3hCLHFCQUFxQjtJQUVyQiwyQkFBMkI7SUFDM0Isc0JBQXNCO0lBQ3RCLDJCQUEyQjtJQUUzQixvQkFBb0I7SUFDcEIsc0JBQXNCO0lBQ3RCLG9CQUFvQjtJQUVwQiw4QkFBOEI7SUFDOUIsb0JBQW9CO0lBQ3BCLHlCQUF5QjtJQUN6QixzQkFBc0I7SUFDdEIsMEJBQTBCO0lBRTFCLFlBQVk7SUFDWixtQkFBbUI7Q0FDWCxDQUFDO0FBRVg7O0dBRUc7QUFDSCxNQUFNLENBQU4sSUFBWSxjQVVYO0FBVkQsV0FBWSxjQUFjO0lBQ3hCLHVDQUFxQixDQUFBO0lBQ3JCLHVDQUFxQixDQUFBO0lBQ3JCLG1DQUFpQixDQUFBO0lBQ2pCLHVDQUFxQixDQUFBO0lBQ3JCLG1DQUFpQixDQUFBO0lBQ2pCLHVDQUFxQixDQUFBO0lBQ3JCLGlDQUFlLENBQUE7SUFDZixxQ0FBbUIsQ0FBQTtJQUNuQixxQ0FBbUIsQ0FBQTtBQUNyQixDQUFDLEVBVlcsY0FBYyxLQUFkLGNBQWMsUUFVekI7QUFFRDs7R0FFRztBQUNILE1BQU0sQ0FBTixJQUFZLFlBT1g7QUFQRCxXQUFZLFlBQVk7SUFDdEIsaUNBQWlCLENBQUE7SUFDakIsdUNBQXVCLENBQUE7SUFDdkIsMkNBQTJCLENBQUE7SUFDM0IseUJBQVMsQ0FBQTtJQUNULHlCQUFTLENBQUE7SUFDVCxtQ0FBbUIsQ0FBQTtBQUNyQixDQUFDLEVBUFcsWUFBWSxLQUFaLFlBQVksUUFPdkIiLCJzb3VyY2VzQ29udGVudCI6WyIvKipcbiAqIFNlbWFudGljIGNvbnZlbnRpb25zIGZvciBUcmFjZUFJIHRyYWNpbmdcbiAqL1xuXG5leHBvcnQgY29uc3QgU2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcyA9IHtcbiAgICBpbnB1dDogXCJpbnB1dFwiLFxuICAgIG91dHB1dDogXCJvdXRwdXRcIixcbiAgICBsbG06IFwibGxtXCIsXG4gICAgcmV0cmlldmFsOiBcInJldHJpZXZhbFwiLFxuICAgIHJlcmFua2VyOiBcInJlcmFua2VyXCIsXG4gICAgbWVzc2FnZXM6IFwibWVzc2FnZXNcIixcbiAgICBtZXNzYWdlOiBcIm1lc3NhZ2VcIixcbiAgICBkb2N1bWVudDogXCJkb2N1bWVudFwiLFxuICAgIGVtYmVkZGluZzogXCJlbWJlZGRpbmdcIixcbiAgICB0b29sOiBcInRvb2xcIixcbiAgICB0b29sX2NhbGw6IFwidG9vbF9jYWxsXCIsXG4gICAgbWV0YWRhdGE6IFwibWV0YWRhdGFcIixcbiAgICB0YWc6IFwidGFnXCIsXG4gICAgc2Vzc2lvbjogXCJzZXNzaW9uXCIsXG4gICAgdXNlcjogXCJ1c2VyXCIsXG4gICAgdHJhY2VhaTogXCJ0cmFjZWFpXCIsXG4gICAgZmk6IFwiZmlcIixcbiAgICBtZXNzYWdlX2NvbnRlbnQ6IFwibWVzc2FnZV9jb250ZW50XCIsXG4gICAgaW1hZ2U6IFwiaW1hZ2VcIixcbiAgICBhdWRpbzogXCJhdWRpb1wiLFxuICAgIHByb21wdDogXCJwcm9tcHRcIixcbiAgfSBhcyBjb25zdDtcbiAgXG4gIGV4cG9ydCBjb25zdCBMTE1BdHRyaWJ1dGVQb3N0Zml4ZXMgPSB7XG4gICAgcHJvdmlkZXI6IFwicHJvdmlkZXJcIixcbiAgICBzeXN0ZW06IFwic3lzdGVtXCIsXG4gICAgbW9kZWxfbmFtZTogXCJtb2RlbF9uYW1lXCIsXG4gICAgdG9rZW5fY291bnQ6IFwidG9rZW5fY291bnRcIixcbiAgICBpbnB1dF9tZXNzYWdlczogXCJpbnB1dF9tZXNzYWdlc1wiLFxuICAgIG91dHB1dF9tZXNzYWdlczogXCJvdXRwdXRfbWVzc2FnZXNcIixcbiAgICBpbnZvY2F0aW9uX3BhcmFtZXRlcnM6IFwiaW52b2NhdGlvbl9wYXJhbWV0ZXJzXCIsXG4gICAgcHJvbXB0czogXCJwcm9tcHRzXCIsXG4gICAgcHJvbXB0X3RlbXBsYXRlOiBcInByb21wdF90ZW1wbGF0ZVwiLFxuICAgIGZ1bmN0aW9uX2NhbGw6IFwiZnVuY3Rpb25fY2FsbFwiLFxuICAgIHRvb2xzOiBcInRvb2xzXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgTExNUHJvbXB0VGVtcGxhdGVBdHRyaWJ1dGVQb3N0Zml4ZXMgPSB7XG4gICAgdmFyaWFibGVzOiBcInZhcmlhYmxlc1wiLFxuICAgIHRlbXBsYXRlOiBcInRlbXBsYXRlXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgUmV0cmlldmFsQXR0cmlidXRlUG9zdGZpeGVzID0ge1xuICAgIGRvY3VtZW50czogXCJkb2N1bWVudHNcIixcbiAgfSBhcyBjb25zdDtcbiAgXG4gIGV4cG9ydCBjb25zdCBSZXJhbmtlckF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICBpbnB1dF9kb2N1bWVudHM6IFwiaW5wdXRfZG9jdW1lbnRzXCIsXG4gICAgb3V0cHV0X2RvY3VtZW50czogXCJvdXRwdXRfZG9jdW1lbnRzXCIsXG4gICAgcXVlcnk6IFwicXVlcnlcIixcbiAgICBtb2RlbF9uYW1lOiBcIm1vZGVsX25hbWVcIixcbiAgICB0b3BfazogXCJ0b3Bfa1wiLFxuICB9IGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IEVtYmVkZGluZ0F0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICBlbWJlZGRpbmdzOiBcImVtYmVkZGluZ3NcIixcbiAgICB0ZXh0OiBcInRleHRcIixcbiAgICBtb2RlbF9uYW1lOiBcIm1vZGVsX25hbWVcIixcbiAgICB2ZWN0b3I6IFwidmVjdG9yXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgVG9vbEF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICBuYW1lOiBcIm5hbWVcIixcbiAgICBkZXNjcmlwdGlvbjogXCJkZXNjcmlwdGlvblwiLFxuICAgIHBhcmFtZXRlcnM6IFwicGFyYW1ldGVyc1wiLFxuICAgIGpzb25fc2NoZW1hOiBcImpzb25fc2NoZW1hXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgTWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICByb2xlOiBcInJvbGVcIixcbiAgICBjb250ZW50OiBcImNvbnRlbnRcIixcbiAgICBjb250ZW50czogXCJjb250ZW50c1wiLFxuICAgIG5hbWU6IFwibmFtZVwiLFxuICAgIGZ1bmN0aW9uX2NhbGxfbmFtZTogXCJmdW5jdGlvbl9jYWxsX25hbWVcIixcbiAgICBmdW5jdGlvbl9jYWxsX2FyZ3VtZW50c19qc29uOiBcImZ1bmN0aW9uX2NhbGxfYXJndW1lbnRzX2pzb25cIixcbiAgICB0b29sX2NhbGxzOiBcInRvb2xfY2FsbHNcIixcbiAgICB0b29sX2NhbGxfaWQ6IFwidG9vbF9jYWxsX2lkXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgTWVzc2FnZUNvbnRlbnRzQXR0cmlidXRlUG9zdGZpeGVzID0ge1xuICAgIHR5cGU6IFwidHlwZVwiLFxuICAgIHRleHQ6IFwidGV4dFwiLFxuICAgIGltYWdlOiBcImltYWdlXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgSW1hZ2VBdHRyaWJ1dGVzUG9zdGZpeGVzID0ge1xuICAgIHVybDogXCJ1cmxcIixcbiAgfSBhcyBjb25zdDtcbiAgXG4gIGV4cG9ydCBjb25zdCBUb29sQ2FsbEF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICBmdW5jdGlvbl9uYW1lOiBcImZ1bmN0aW9uLm5hbWVcIixcbiAgICBmdW5jdGlvbl9hcmd1bWVudHNfanNvbjogXCJmdW5jdGlvbi5hcmd1bWVudHNcIixcbiAgICBpZDogXCJpZFwiLFxuICB9IGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IERvY3VtZW50QXR0cmlidXRlUG9zdGZpeGVzID0ge1xuICAgIGlkOiBcImlkXCIsXG4gICAgY29udGVudDogXCJjb250ZW50XCIsXG4gICAgc2NvcmU6IFwic2NvcmVcIixcbiAgICBtZXRhZGF0YTogXCJtZXRhZGF0YVwiLFxuICB9IGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IFRhZ0F0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICB0YWdzOiBcInRhZ3NcIixcbiAgfSBhcyBjb25zdDtcbiAgXG4gIGV4cG9ydCBjb25zdCBTZXNzaW9uQXR0cmlidXRlUG9zdGZpeGVzID0ge1xuICAgIGlkOiBcImlkXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgVXNlckF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICBpZDogXCJpZFwiLFxuICB9IGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IEF1ZGlvQXR0cmlidXRlc1Bvc3RmaXhlcyA9IHtcbiAgICB1cmw6IFwidXJsXCIsXG4gICAgbWltZV90eXBlOiBcIm1pbWVfdHlwZVwiLFxuICAgIHRyYW5zY3JpcHQ6IFwidHJhbnNjcmlwdFwiLFxuICB9IGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IFByb21wdEF0dHJpYnV0ZVBvc3RmaXhlcyA9IHtcbiAgICB2ZW5kb3I6IFwidmVuZG9yXCIsXG4gICAgaWQ6IFwiaWRcIixcbiAgICB1cmw6IFwidXJsXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIGlucHV0IHRvIGFueSBzcGFuXG4gICAqL1xuICBleHBvcnQgY29uc3QgSU5QVVRfVkFMVUUgPSBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmlucHV0fS52YWx1ZWAgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBJTlBVVF9NSU1FX1RZUEUgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuaW5wdXR9Lm1pbWVfdHlwZWAgYXMgY29uc3Q7XG4gIC8qKlxuICAgKiBUaGUgb3V0cHV0IG9mIGFueSBzcGFuXG4gICAqL1xuICBleHBvcnQgY29uc3QgT1VUUFVUX1ZBTFVFID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm91dHB1dH0udmFsdWVgIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgT1VUUFVUX01JTUVfVFlQRSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5vdXRwdXR9Lm1pbWVfdHlwZWAgYXMgY29uc3Q7XG4gIC8qKlxuICAgKiBUaGUgbWVzc2FnZXMgc2VudCB0byB0aGUgTExNIGZvciBjb21wbGV0aW9uc1xuICAgKiBUeXBpY2FsbHkgc2VlbiBpbiBPcGVuQUkgY2hhdCBjb21wbGV0aW9uc1xuICAgKiBAc2VlIGh0dHBzOi8vYmV0YS5vcGVuYWkuY29tL2RvY3MvYXBpLXJlZmVyZW5jZS9jb21wbGV0aW9ucy9jcmVhdGVcbiAgICovXG4gIGV4cG9ydCBjb25zdCBMTE1fSU5QVVRfTUVTU0FHRVMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy5pbnB1dF9tZXNzYWdlc31gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBwcm9tcHRzIHNlbnQgdG8gdGhlIExMTSBmb3IgY29tcGxldGlvbnNcbiAgICogVHlwaWNhbGx5IHNlZW4gaW4gT3BlbkFJIGxlZ2FjeSBjb21wbGV0aW9uc1xuICAgKiBAc2VlIGh0dHBzOi8vYmV0YS5vcGVuYWkuY29tL2RvY3MvYXBpLXJlZmVyZW5jZS9jb21wbGV0aW9ucy9jcmVhdGVcbiAgICovXG4gIGV4cG9ydCBjb25zdCBMTE1fUFJPTVBUUyA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnByb21wdHN9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgSlNPTiByZXByZXNlbnRhdGlvbiBvZiB0aGUgcGFyYW1ldGVycyBwYXNzZWQgdG8gdGhlIExMTVxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IExMTV9JTlZPQ0FUSU9OX1BBUkFNRVRFUlMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy5pbnZvY2F0aW9uX3BhcmFtZXRlcnN9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgbWVzc2FnZXMgcmVjZWl2ZWQgZnJvbSB0aGUgTExNIGZvciBjb21wbGV0aW9uc1xuICAgKiBUeXBpY2FsbHkgc2VlbiBpbiBPcGVuQUkgY2hhdCBjb21wbGV0aW9uc1xuICAgKiBAc2VlIGh0dHBzOi8vcGxhdGZvcm0ub3BlbmFpLmNvbS9kb2NzL2FwaS1yZWZlcmVuY2UvY2hhdC9vYmplY3QjY2hvaWNlcy1tZXNzYWdlXG4gICAqL1xuICBleHBvcnQgY29uc3QgTExNX09VVFBVVF9NRVNTQUdFUyA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLm91dHB1dF9tZXNzYWdlc31gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBuYW1lIG9mIHRoZSBMTE0gbW9kZWxcbiAgICovXG4gIGV4cG9ydCBjb25zdCBMTE1fTU9ERUxfTkFNRSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLm1vZGVsX25hbWV9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgcHJvdmlkZXIgb2YgdGhlIGluZmVyZW5jZXMuIEUuZy4gdGhlIGNsb3VkIHByb3ZpZGVyXG4gICAqL1xuICBleHBvcnQgY29uc3QgTExNX1BST1ZJREVSID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmxsbX0uJHtMTE1BdHRyaWJ1dGVQb3N0Zml4ZXMucHJvdmlkZXJ9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgQUkgcHJvZHVjdCBhcyBpZGVudGlmaWVkIGJ5IHRoZSBjbGllbnQgb3Igc2VydmVyXG4gICAqL1xuICBleHBvcnQgY29uc3QgTExNX1NZU1RFTSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnN5c3RlbX1gIGFzIGNvbnN0O1xuICBcbiAgLyoqIFRva2VuIGNvdW50IGZvciB0aGUgY29tcGxldGlvbiBieSB0aGUgbGxtICovXG4gIGV4cG9ydCBjb25zdCBMTE1fVE9LRU5fQ09VTlRfQ09NUExFVElPTiA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnRva2VuX2NvdW50fS5jb21wbGV0aW9uYCBhcyBjb25zdDtcbiAgXG4gIC8qKiBUb2tlbiBjb3VudCBmb3IgdGhlIHJlYXNvbmluZyBzdGVwcyBpbiB0aGUgY29tcGxldGlvbiAqL1xuICBleHBvcnQgY29uc3QgTExNX1RPS0VOX0NPVU5UX0NPTVBMRVRJT05fREVUQUlMU19SRUFTT05JTkcgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy50b2tlbl9jb3VudH0uY29tcGxldGlvbl9kZXRhaWxzLnJlYXNvbmluZ2AgYXMgY29uc3Q7XG4gIFxuICAvKiogVG9rZW4gY291bnQgZm9yIGF1ZGlvIGlucHV0IGdlbmVyYXRlZCBieSB0aGUgbW9kZWwgKi9cbiAgZXhwb3J0IGNvbnN0IExMTV9UT0tFTl9DT1VOVF9DT01QTEVUSU9OX0RFVEFJTFNfQVVESU8gPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy50b2tlbl9jb3VudH0uY29tcGxldGlvbl9kZXRhaWxzLmF1ZGlvYCBhcyBjb25zdDtcbiAgXG4gIC8qKiBUb2tlbiBjb3VudCBmb3IgdGhlIHByb21wdCB0byB0aGUgbGxtICovXG4gIGV4cG9ydCBjb25zdCBMTE1fVE9LRU5fQ09VTlRfUFJPTVBUID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmxsbX0uJHtMTE1BdHRyaWJ1dGVQb3N0Zml4ZXMudG9rZW5fY291bnR9LnByb21wdGAgYXMgY29uc3Q7XG4gIFxuICAvKiogVG9rZW4gY291bnQgZm9yIHRoZSB0b2tlbnMgd3JpdHRlbiB0byB0aGUgY2FjaGUgKi9cbiAgZXhwb3J0IGNvbnN0IExMTV9UT0tFTl9DT1VOVF9QUk9NUFRfREVUQUlMU19DQUNIRV9XUklURSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnRva2VuX2NvdW50fS5wcm9tcHRfZGV0YWlscy5jYWNoZV93cml0ZWAgYXMgY29uc3Q7XG4gIFxuICAvKiogVG9rZW4gY291bnQgZm9yIHRoZSB0b2tlbnMgcmV0cmlldmVkIGZyb20gdGhlIGNhY2hlICovXG4gIGV4cG9ydCBjb25zdCBMTE1fVE9LRU5fQ09VTlRfUFJPTVBUX0RFVEFJTFNfQ0FDSEVfUkVBRCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnRva2VuX2NvdW50fS5wcm9tcHRfZGV0YWlscy5jYWNoZV9yZWFkYCBhcyBjb25zdDtcbiAgXG4gIC8qKiBUb2tlbiBjb3VudCBmb3IgYXVkaW8gaW5wdXQgcHJlc2VudGVkIGluIHRoZSBwcm9tcHQgKi9cbiAgZXhwb3J0IGNvbnN0IExMTV9UT0tFTl9DT1VOVF9QUk9NUFRfREVUQUlMU19BVURJTyA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnRva2VuX2NvdW50fS5wcm9tcHRfZGV0YWlscy5hdWRpb2AgYXMgY29uc3Q7XG4gIFxuICAvKiogVG9rZW4gY291bnQgZm9yIHRoZSBlbnRpcmUgdHJhbnNhY3Rpb24gd2l0aCB0aGUgbGxtICovXG4gIGV4cG9ydCBjb25zdCBMTE1fVE9LRU5fQ09VTlRfVE9UQUwgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy50b2tlbl9jb3VudH0udG90YWxgIGFzIGNvbnN0O1xuICAvKipcbiAgICogVGhlIHJvbGUgdGhhdCB0aGUgTExNIGFzc3VtZXMgdGhlIG1lc3NhZ2UgaXMgZnJvbVxuICAgKiBkdXJpbmcgdGhlIExMTSBpbnZvY2F0aW9uXG4gICAqL1xuICBleHBvcnQgY29uc3QgTUVTU0FHRV9ST0xFID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm1lc3NhZ2V9LiR7TWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcy5yb2xlfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIG5hbWUgb2YgdGhlIG1lc3NhZ2UuIFRoaXMgaXMgb25seSB1c2VkIGZvciByb2xlICdmdW5jdGlvbicgd2hlcmUgdGhlIG5hbWVcbiAgICogb2YgdGhlIGZ1bmN0aW9uIGlzIGNhcHR1cmVkIGluIHRoZSBuYW1lIGZpZWxkIGFuZCB0aGUgcGFyYW1ldGVycyBhcmUgY2FwdHVyZWQgaW4gdGhlXG4gICAqIGNvbnRlbnQuXG4gICAqL1xuICBleHBvcnQgY29uc3QgTUVTU0FHRV9OQU1FID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm1lc3NhZ2V9LiR7TWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcy5uYW1lfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIHRvb2wgY2FsbHMgZ2VuZXJhdGVkIGJ5IHRoZSBtb2RlbCwgc3VjaCBhcyBmdW5jdGlvbiBjYWxscy5cbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX1RPT0xfQ0FMTFMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubWVzc2FnZX0uJHtNZXNzYWdlQXR0cmlidXRlUG9zdGZpeGVzLnRvb2xfY2FsbHN9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgaWQgb2YgdGhlIHRvb2wgY2FsbCBvbiBhIFwidG9vbFwiIHJvbGUgbWVzc2FnZVxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IE1FU1NBR0VfVE9PTF9DQUxMX0lEID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm1lc3NhZ2V9LiR7TWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcy50b29sX2NhbGxfaWR9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiB0b29sX2NhbGwuZnVuY3Rpb24ubmFtZVxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFRPT0xfQ0FMTF9GVU5DVElPTl9OQU1FID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLnRvb2xfY2FsbH0uJHtUb29sQ2FsbEF0dHJpYnV0ZVBvc3RmaXhlcy5mdW5jdGlvbl9uYW1lfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogdG9vbF9jYWxsLmZ1bmN0aW9uLmFyZ3VtZW50IChKU09OIHN0cmluZylcbiAgICovXG4gIGV4cG9ydCBjb25zdCBUT09MX0NBTExfRlVOQ1RJT05fQVJHVU1FTlRTX0pTT04gPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMudG9vbF9jYWxsfS4ke1Rvb2xDYWxsQXR0cmlidXRlUG9zdGZpeGVzLmZ1bmN0aW9uX2FyZ3VtZW50c19qc29ufWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIGlkIG9mIHRoZSB0b29sIGNhbGxcbiAgICovXG4gIGV4cG9ydCBjb25zdCBUT09MX0NBTExfSUQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMudG9vbF9jYWxsfS4ke1Rvb2xDYWxsQXR0cmlidXRlUG9zdGZpeGVzLmlkfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIExMTSBmdW5jdGlvbiBjYWxsIGZ1bmN0aW9uIG5hbWVcbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX0ZVTkNUSU9OX0NBTExfTkFNRSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5tZXNzYWdlfS4ke01lc3NhZ2VBdHRyaWJ1dGVQb3N0Zml4ZXMuZnVuY3Rpb25fY2FsbF9uYW1lfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIExMTSBmdW5jdGlvbiBjYWxsIGZ1bmN0aW9uIGFyZ3VtZW50cyBpbiBhIGpzb24gc3RyaW5nXG4gICAqL1xuICBleHBvcnQgY29uc3QgTUVTU0FHRV9GVU5DVElPTl9DQUxMX0FSR1VNRU5UU19KU09OID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm1lc3NhZ2V9LiR7TWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcy5mdW5jdGlvbl9jYWxsX2FyZ3VtZW50c19qc29ufWAgYXMgY29uc3Q7XG4gIC8qKlxuICAgKiBUaGUgY29udGVudCBvZiB0aGUgbWVzc2FnZSBzZW50IHRvIHRoZSBMTE1cbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX0NPTlRFTlQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubWVzc2FnZX0uJHtNZXNzYWdlQXR0cmlidXRlUG9zdGZpeGVzLmNvbnRlbnR9YCBhcyBjb25zdDtcbiAgLyoqXG4gICAqIFRoZSBhcnJheSBvZiBjb250ZW50cyBmb3IgdGhlIG1lc3NhZ2Ugc2VudCB0byB0aGUgTExNLiBFYWNoIGVsZW1lbnQgb2YgdGhlIGFycmF5IGlzXG4gICAqIGFuIGBtZXNzYWdlX2NvbnRlbnRgIG9iamVjdC5cbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX0NPTlRFTlRTID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLm1lc3NhZ2V9LiR7TWVzc2FnZUF0dHJpYnV0ZVBvc3RmaXhlcy5jb250ZW50c31gIGFzIGNvbnN0O1xuICAvKipcbiAgICogVGhlIHR5cGUgb2YgY29udGVudCBzZW50IHRvIHRoZSBMTE1cbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX0NPTlRFTlRfVFlQRSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5tZXNzYWdlX2NvbnRlbnR9LiR7TWVzc2FnZUNvbnRlbnRzQXR0cmlidXRlUG9zdGZpeGVzLnR5cGV9YCBhcyBjb25zdDtcbiAgLyoqXG4gICAqIFRoZSB0ZXh0IGNvbnRlbnQgb2YgdGhlIG1lc3NhZ2Ugc2VudCB0byB0aGUgTExNXG4gICAqL1xuICBleHBvcnQgY29uc3QgTUVTU0FHRV9DT05URU5UX1RFWFQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubWVzc2FnZV9jb250ZW50fS4ke01lc3NhZ2VDb250ZW50c0F0dHJpYnV0ZVBvc3RmaXhlcy50ZXh0fWAgYXMgY29uc3Q7XG4gIC8qKlxuICAgKiBUaGUgaW1hZ2UgY29udGVudCBvZiB0aGUgbWVzc2FnZSBzZW50IHRvIHRoZSBMTE1cbiAgICovXG4gIGV4cG9ydCBjb25zdCBNRVNTQUdFX0NPTlRFTlRfSU1BR0UgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubWVzc2FnZV9jb250ZW50fS4ke01lc3NhZ2VDb250ZW50c0F0dHJpYnV0ZVBvc3RmaXhlcy5pbWFnZX1gIGFzIGNvbnN0O1xuICAvKipcbiAgICogVGhlIGh0dHAgb3IgYmFzZTY0IGxpbmsgdG8gdGhlIGltYWdlXG4gICAqL1xuICBleHBvcnQgY29uc3QgSU1BR0VfVVJMID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmltYWdlfS4ke0ltYWdlQXR0cmlidXRlc1Bvc3RmaXhlcy51cmx9YCBhcyBjb25zdDtcbiAgXG4gIGV4cG9ydCBjb25zdCBET0NVTUVOVF9JRCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5kb2N1bWVudH0uJHtEb2N1bWVudEF0dHJpYnV0ZVBvc3RmaXhlcy5pZH1gIGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IERPQ1VNRU5UX0NPTlRFTlQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuZG9jdW1lbnR9LiR7RG9jdW1lbnRBdHRyaWJ1dGVQb3N0Zml4ZXMuY29udGVudH1gIGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IERPQ1VNRU5UX1NDT1JFID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmRvY3VtZW50fS4ke0RvY3VtZW50QXR0cmlidXRlUG9zdGZpeGVzLnNjb3JlfWAgYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgY29uc3QgRE9DVU1FTlRfTUVUQURBVEEgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuZG9jdW1lbnR9LiR7RG9jdW1lbnRBdHRyaWJ1dGVQb3N0Zml4ZXMubWV0YWRhdGF9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgdGV4dCB0aGF0IHdhcyBlbWJlZGRlZCB0byBjcmVhdGUgdGhlIHZlY3RvclxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IEVNQkVERElOR19URVhUID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmVtYmVkZGluZ30uJHtFbWJlZGRpbmdBdHRyaWJ1dGVQb3N0Zml4ZXMudGV4dH1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBuYW1lIG9mIHRoZSBtb2RlbCB0aGF0IHdhcyB1c2VkIHRvIGNyZWF0ZSB0aGUgdmVjdG9yXG4gICAqL1xuICBleHBvcnQgY29uc3QgRU1CRURESU5HX01PREVMX05BTUUgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuZW1iZWRkaW5nfS4ke0VtYmVkZGluZ0F0dHJpYnV0ZVBvc3RmaXhlcy5tb2RlbF9uYW1lfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIGVtYmVkZGluZyB2ZWN0b3IuIFR5cGljYWxseSBhIGhpZ2ggZGltZW5zaW9uYWwgdmVjdG9yIG9mIGZsb2F0cyBvciBpbnRzXG4gICAqL1xuICBleHBvcnQgY29uc3QgRU1CRURESU5HX1ZFQ1RPUiA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5lbWJlZGRpbmd9LiR7RW1iZWRkaW5nQXR0cmlidXRlUG9zdGZpeGVzLnZlY3Rvcn1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBlbWJlZGRpbmcgbGlzdCByb290XG4gICAqL1xuICBleHBvcnQgY29uc3QgRU1CRURESU5HX0VNQkVERElOR1MgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuZW1iZWRkaW5nfS4ke0VtYmVkZGluZ0F0dHJpYnV0ZVBvc3RmaXhlcy5lbWJlZGRpbmdzfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIHJldHJpZXZhbCBkb2N1bWVudHMgbGlzdCByb290XG4gICAqL1xuICBleHBvcnQgY29uc3QgUkVUUklFVkFMX0RPQ1VNRU5UUyA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5yZXRyaWV2YWx9LiR7UmV0cmlldmFsQXR0cmlidXRlUG9zdGZpeGVzLmRvY3VtZW50c31gIGFzIGNvbnN0O1xuICBcbiAgY29uc3QgUFJPTVBUX1RFTVBMQVRFX1BSRUZJWCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLnByb21wdF90ZW1wbGF0ZX1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBKU09OIHJlcHJlc2VudGF0aW9uIG9mIHRoZSB2YXJpYWJsZXMgdXNlZCBpbiB0aGUgcHJvbXB0IHRlbXBsYXRlXG4gICAqL1xuICBleHBvcnQgY29uc3QgUFJPTVBUX1RFTVBMQVRFX1ZBUklBQkxFUyA9XG4gICAgYCR7UFJPTVBUX1RFTVBMQVRFX1BSRUZJWH0udmFyaWFibGVzYCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBBIHByb21wdCB0ZW1wbGF0ZVxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFBST01QVF9URU1QTEFURV9URU1QTEFURSA9XG4gICAgYCR7UFJPTVBUX1RFTVBMQVRFX1BSRUZJWH0udGVtcGxhdGVgIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBKU09OIHJlcHJlc2VudGF0aW9uIG9mIGEgZnVuY3Rpb24gY2FsbCBvZiBhbiBMTE1cbiAgICovXG4gIGV4cG9ydCBjb25zdCBMTE1fRlVOQ1RJT05fQ0FMTCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5sbG19LiR7TExNQXR0cmlidXRlUG9zdGZpeGVzLmZ1bmN0aW9uX2NhbGx9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBMaXN0IG9mIHRvb2xzIHRoYXQgYXJlIGFkdmVydGlzZWQgdG8gdGhlIExMTSB0byBiZSBhYmxlIHRvIGNhbGxcbiAgICovXG4gIGV4cG9ydCBjb25zdCBMTE1fVE9PTFMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMubGxtfS4ke0xMTUF0dHJpYnV0ZVBvc3RmaXhlcy50b29sc31gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBuYW1lIG9mIGEgdG9vbFxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFRPT0xfTkFNRSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy50b29sfS4ke1Rvb2xBdHRyaWJ1dGVQb3N0Zml4ZXMubmFtZX1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBkZXNjcmlwdGlvbiBvZiBhIHRvb2xcbiAgICovXG4gIGV4cG9ydCBjb25zdCBUT09MX0RFU0NSSVBUSU9OID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLnRvb2x9LiR7VG9vbEF0dHJpYnV0ZVBvc3RmaXhlcy5kZXNjcmlwdGlvbn1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBwYXJhbWV0ZXJzIG9mIHRoZSB0b29sIHJlcHJlc2VudGVkIGFzIGEgSlNPTiBzdHJpbmdcbiAgICovXG4gIGV4cG9ydCBjb25zdCBUT09MX1BBUkFNRVRFUlMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMudG9vbH0uJHtUb29sQXR0cmlidXRlUG9zdGZpeGVzLnBhcmFtZXRlcnN9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUganNvbiBzY2hlbWEgb2YgYSB0b29sIGlucHV0LCBJdCBpcyBSRUNPTU1FTkRFRCB0aGF0IHRoaXMgYmUgaW4gdGhlXG4gICAqIE9wZW5BSSB0b29sIGNhbGxpbmcgZm9ybWF0OiBodHRwczovL3BsYXRmb3JtLm9wZW5haS5jb20vZG9jcy9hc3Npc3RhbnRzL3Rvb2xzXG4gICAqL1xuICBleHBvcnQgY29uc3QgVE9PTF9KU09OX1NDSEVNQSA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy50b29sfS4ke1Rvb2xBdHRyaWJ1dGVQb3N0Zml4ZXMuanNvbl9zY2hlbWF9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgc2Vzc2lvbiBpZCBvZiBhIHRyYWNlLiBVc2VkIHRvIGNvcnJlbGF0ZSBzcGFucyBpbiBhIHNpbmdsZSBzZXNzaW9uLlxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFNFU1NJT05fSUQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMuc2Vzc2lvbn0uJHtTZXNzaW9uQXR0cmlidXRlUG9zdGZpeGVzLmlkfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIHVzZXIgaWQgb2YgYSB0cmFjZS4gVXNlZCB0byBjb3JyZWxhdGUgc3BhbnMgZm9yIGEgc2luZ2xlIHVzZXIuXG4gICAqL1xuICBleHBvcnQgY29uc3QgVVNFUl9JRCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy51c2VyfS4ke1VzZXJBdHRyaWJ1dGVQb3N0Zml4ZXMuaWR9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgZG9jdW1lbnRzIHVzZWQgYXMgaW5wdXQgdG8gdGhlIHJlcmFua2VyXG4gICAqL1xuICBleHBvcnQgY29uc3QgUkVSQU5LRVJfSU5QVVRfRE9DVU1FTlRTID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLnJlcmFua2VyfS4ke1JlcmFua2VyQXR0cmlidXRlUG9zdGZpeGVzLmlucHV0X2RvY3VtZW50c31gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBkb2N1bWVudHMgb3V0cHV0IGJ5IHRoZSByZXJhbmtlclxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFJFUkFOS0VSX09VVFBVVF9ET0NVTUVOVFMgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMucmVyYW5rZXJ9LiR7UmVyYW5rZXJBdHRyaWJ1dGVQb3N0Zml4ZXMub3V0cHV0X2RvY3VtZW50c31gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSBxdWVyeSBzdHJpbmcgZm9yIHRoZSByZXJhbmtlclxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFJFUkFOS0VSX1FVRVJZID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLnJlcmFua2VyfS4ke1JlcmFua2VyQXR0cmlidXRlUG9zdGZpeGVzLnF1ZXJ5fWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIG1vZGVsIG5hbWUgZm9yIHRoZSByZXJhbmtlclxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFJFUkFOS0VSX01PREVMX05BTUUgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMucmVyYW5rZXJ9LiR7UmVyYW5rZXJBdHRyaWJ1dGVQb3N0Zml4ZXMubW9kZWxfbmFtZX1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSB0b3AgayBwYXJhbWV0ZXIgZm9yIHRoZSByZXJhbmtlclxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFJFUkFOS0VSX1RPUF9LID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLnJlcmFua2VyfS4ke1JlcmFua2VyQXR0cmlidXRlUG9zdGZpeGVzLnRvcF9rfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogTWV0YWRhdGEgZm9yIGEgc3BhbiwgdXNlZCB0byBzdG9yZSB1c2VyLWRlZmluZWQga2V5LXZhbHVlIHBhaXJzXG4gICAqL1xuICBleHBvcnQgY29uc3QgTUVUQURBVEEgPSBcIm1ldGFkYXRhXCIgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogQSBwcm9tcHQgdGVtcGxhdGUgdmVyc2lvblxuICAgKi9cbiAgZXhwb3J0IGNvbnN0IFBST01QVF9URU1QTEFURV9WRVJTSU9OID1cbiAgICBgJHtQUk9NUFRfVEVNUExBVEVfUFJFRklYfS52ZXJzaW9uYCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgdGFncyBhc3NvY2lhdGVkIHdpdGggYSBzcGFuXG4gICAqL1xuICBleHBvcnQgY29uc3QgVEFHX1RBR1MgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMudGFnfS4ke1RhZ0F0dHJpYnV0ZVBvc3RmaXhlcy50YWdzfWAgYXMgY29uc3Q7XG4gIFxuICAvKipcbiAgICogVGhlIHVybCBvZiBhbiBhdWRpbyBmaWxlXG4gICAqL1xuICBleHBvcnQgY29uc3QgQVVESU9fVVJMID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmF1ZGlvfS4ke0F1ZGlvQXR0cmlidXRlc1Bvc3RmaXhlcy51cmx9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgYXVkaW8gbWltZSB0eXBlXG4gICAqL1xuICBleHBvcnQgY29uc3QgQVVESU9fTUlNRV9UWVBFID1cbiAgICBgJHtTZW1hbnRpY0F0dHJpYnV0ZVByZWZpeGVzLmF1ZGlvfS4ke0F1ZGlvQXR0cmlidXRlc1Bvc3RmaXhlcy5taW1lX3R5cGV9YCBhcyBjb25zdDtcbiAgXG4gIC8qKlxuICAgKiBUaGUgYXVkaW8gdHJhbnNjcmlwdCBhcyB0ZXh0XG4gICAqL1xuICBleHBvcnQgY29uc3QgQVVESU9fVFJBTlNDUklQVCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5hdWRpb30uJHtBdWRpb0F0dHJpYnV0ZXNQb3N0Zml4ZXMudHJhbnNjcmlwdH1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIFRoZSB2ZW5kb3Igb3Igb3JpZ2luIG9mIHRoZSBwcm9tcHQsIGUuZy4gYSBwcm9tcHQgbGlicmFyeSwgYSBzcGVjaWFsaXplZCBzZXJ2aWNlLCBldGMuXG4gICAqL1xuICBleHBvcnQgY29uc3QgUFJPTVBUX1ZFTkRPUiA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5wcm9tcHR9LiR7UHJvbXB0QXR0cmlidXRlUG9zdGZpeGVzLnZlbmRvcn1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIEEgdmVuZG9yLXNwZWNpZmljIGlkIHVzZWQgdG8gbG9jYXRlIHRoZSBwcm9tcHRcbiAgICovXG4gIGV4cG9ydCBjb25zdCBQUk9NUFRfSUQgPVxuICAgIGAke1NlbWFudGljQXR0cmlidXRlUHJlZml4ZXMucHJvbXB0fS4ke1Byb21wdEF0dHJpYnV0ZVBvc3RmaXhlcy5pZH1gIGFzIGNvbnN0O1xuICBcbiAgLyoqXG4gICAqIEEgdmVuZG9yLXNwZWNpZmljIFVSTCB1c2VkIHRvIGxvY2F0ZSB0aGUgcHJvbXB0XG4gICAqL1xuICBleHBvcnQgY29uc3QgUFJPTVBUX1VSTCA9XG4gICAgYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5wcm9tcHR9LiR7UHJvbXB0QXR0cmlidXRlUG9zdGZpeGVzLnVybH1gIGFzIGNvbnN0O1xuICBcbiAgZXhwb3J0IGNvbnN0IFNlbWFudGljQ29udmVudGlvbnMgPSB7XG4gICAgSU1BR0VfVVJMLFxuICAgIElOUFVUX1ZBTFVFLFxuICAgIElOUFVUX01JTUVfVFlQRSxcbiAgICBPVVRQVVRfVkFMVUUsXG4gICAgT1VUUFVUX01JTUVfVFlQRSxcbiAgICBMTE1fSU5QVVRfTUVTU0FHRVMsXG4gICAgTExNX09VVFBVVF9NRVNTQUdFUyxcbiAgICBMTE1fTU9ERUxfTkFNRSxcbiAgICBMTE1fUFJPTVBUUyxcbiAgICBMTE1fSU5WT0NBVElPTl9QQVJBTUVURVJTLFxuICAgIExMTV9UT0tFTl9DT1VOVF9DT01QTEVUSU9OLFxuICAgIExMTV9UT0tFTl9DT1VOVF9DT01QTEVUSU9OX0RFVEFJTFNfUkVBU09OSU5HLFxuICAgIExMTV9UT0tFTl9DT1VOVF9DT01QTEVUSU9OX0RFVEFJTFNfQVVESU8sXG4gICAgTExNX1RPS0VOX0NPVU5UX1BST01QVCxcbiAgICBMTE1fVE9LRU5fQ09VTlRfUFJPTVBUX0RFVEFJTFNfQ0FDSEVfV1JJVEUsXG4gICAgTExNX1RPS0VOX0NPVU5UX1BST01QVF9ERVRBSUxTX0NBQ0hFX1JFQUQsXG4gICAgTExNX1RPS0VOX0NPVU5UX1BST01QVF9ERVRBSUxTX0FVRElPLFxuICAgIExMTV9UT0tFTl9DT1VOVF9UT1RBTCxcbiAgICBMTE1fU1lTVEVNLFxuICAgIExMTV9QUk9WSURFUixcbiAgICBMTE1fVE9PTFMsXG4gICAgTUVTU0FHRV9ST0xFLFxuICAgIE1FU1NBR0VfTkFNRSxcbiAgICBNRVNTQUdFX1RPT0xfQ0FMTFMsXG4gICAgTUVTU0FHRV9UT09MX0NBTExfSUQsXG4gICAgVE9PTF9DQUxMX0lELFxuICAgIFRPT0xfQ0FMTF9GVU5DVElPTl9OQU1FLFxuICAgIFRPT0xfQ0FMTF9GVU5DVElPTl9BUkdVTUVOVFNfSlNPTixcbiAgICBNRVNTQUdFX0ZVTkNUSU9OX0NBTExfTkFNRSxcbiAgICBNRVNTQUdFX0ZVTkNUSU9OX0NBTExfQVJHVU1FTlRTX0pTT04sXG4gICAgTUVTU0FHRV9DT05URU5ULFxuICAgIE1FU1NBR0VfQ09OVEVOVFMsXG4gICAgTUVTU0FHRV9DT05URU5UX0lNQUdFLFxuICAgIE1FU1NBR0VfQ09OVEVOVF9URVhULFxuICAgIE1FU1NBR0VfQ09OVEVOVF9UWVBFLFxuICAgIERPQ1VNRU5UX0lELFxuICAgIERPQ1VNRU5UX0NPTlRFTlQsXG4gICAgRE9DVU1FTlRfU0NPUkUsXG4gICAgRE9DVU1FTlRfTUVUQURBVEEsXG4gICAgRU1CRURESU5HX0VNQkVERElOR1MsXG4gICAgRU1CRURESU5HX1RFWFQsXG4gICAgRU1CRURESU5HX01PREVMX05BTUUsXG4gICAgRU1CRURESU5HX1ZFQ1RPUixcbiAgICBUT09MX0RFU0NSSVBUSU9OLFxuICAgIFRPT0xfTkFNRSxcbiAgICBUT09MX1BBUkFNRVRFUlMsXG4gICAgVE9PTF9KU09OX1NDSEVNQSxcbiAgICBQUk9NUFRfVEVNUExBVEVfVkFSSUFCTEVTLFxuICAgIFBST01QVF9URU1QTEFURV9URU1QTEFURSxcbiAgICBQUk9NUFRfVEVNUExBVEVfVkVSU0lPTixcbiAgICBSRVJBTktFUl9JTlBVVF9ET0NVTUVOVFMsXG4gICAgUkVSQU5LRVJfT1VUUFVUX0RPQ1VNRU5UUyxcbiAgICBSRVJBTktFUl9RVUVSWSxcbiAgICBSRVJBTktFUl9NT0RFTF9OQU1FLFxuICAgIFJFUkFOS0VSX1RPUF9LLFxuICAgIExMTV9GVU5DVElPTl9DQUxMLFxuICAgIFJFVFJJRVZBTF9ET0NVTUVOVFMsXG4gICAgU0VTU0lPTl9JRCxcbiAgICBVU0VSX0lELFxuICAgIE1FVEFEQVRBLFxuICAgIFRBR19UQUdTLFxuICAgIEZJX1NQQU5fS0lORDogYCR7U2VtYW50aWNBdHRyaWJ1dGVQcmVmaXhlcy5maX0uc3Bhbi5raW5kYCxcbiAgICBQUk9NUFRfVkVORE9SLFxuICAgIFBST01QVF9JRCxcbiAgICBQUk9NUFRfVVJMLFxuICAgIFJBV19JTlBVVDogXCJyYXcuaW5wdXRcIixcbiAgICBSQVdfT1VUUFVUOiBcInJhdy5vdXRwdXRcIixcblxuICAgIC8vIFZlY3RvciBEYXRhYmFzZSBhdHRyaWJ1dGVzXG4gICAgREJfU1lTVEVNOiBcImRiLnN5c3RlbVwiLFxuICAgIERCX09QRVJBVElPTl9OQU1FOiBcImRiLm9wZXJhdGlvbi5uYW1lXCIsXG4gICAgREJfTkFNRVNQQUNFOiBcImRiLm5hbWVzcGFjZVwiLFxuICAgIERCX1ZFQ1RPUl9RVUVSWV9UT1BfSzogXCJkYi52ZWN0b3IucXVlcnkudG9wX2tcIixcbiAgICBEQl9WRUNUT1JfUVVFUllfRklMVEVSOiBcImRiLnZlY3Rvci5xdWVyeS5maWx0ZXJcIixcbiAgICBEQl9WRUNUT1JfUVVFUllfSU5DTFVERV9NRVRBREFUQTogXCJkYi52ZWN0b3IucXVlcnkuaW5jbHVkZV9tZXRhZGF0YVwiLFxuICAgIERCX1ZFQ1RPUl9RVUVSWV9JTkNMVURFX1ZFQ1RPUlM6IFwiZGIudmVjdG9yLnF1ZXJ5LmluY2x1ZGVfdmVjdG9yc1wiLFxuICAgIERCX1ZFQ1RPUl9RVUVSWV9TQ09SRV9USFJFU0hPTEQ6IFwiZGIudmVjdG9yLnF1ZXJ5LnNjb3JlX3RocmVzaG9sZFwiLFxuICAgIERCX1ZFQ1RPUl9RVUVSWV9NRVRSSUM6IFwiZGIudmVjdG9yLnF1ZXJ5Lm1ldHJpY1wiLFxuICAgIERCX1ZFQ1RPUl9SRVNVTFRTX0NPVU5UOiBcImRiLnZlY3Rvci5yZXN1bHRzLmNvdW50XCIsXG4gICAgREJfVkVDVE9SX1JFU1VMVFNfU0NPUkVTOiBcImRiLnZlY3Rvci5yZXN1bHRzLnNjb3Jlc1wiLFxuICAgIERCX1ZFQ1RPUl9SRVNVTFRTX0lEUzogXCJkYi52ZWN0b3IucmVzdWx0cy5pZHNcIixcbiAgICBEQl9WRUNUT1JfVVBTRVJUX0NPVU5UOiBcImRiLnZlY3Rvci51cHNlcnQuY291bnRcIixcbiAgICBEQl9WRUNUT1JfVVBTRVJUX0RJTUVOU0lPTlM6IFwiZGIudmVjdG9yLnVwc2VydC5kaW1lbnNpb25zXCIsXG4gICAgREJfVkVDVE9SX0RFTEVURV9DT1VOVDogXCJkYi52ZWN0b3IuZGVsZXRlLmNvdW50XCIsXG4gICAgREJfVkVDVE9SX0RFTEVURV9BTEw6IFwiZGIudmVjdG9yLmRlbGV0ZS5hbGxcIixcbiAgICBEQl9WRUNUT1JfSU5ERVhfTkFNRTogXCJkYi52ZWN0b3IuaW5kZXgubmFtZVwiLFxuICAgIERCX1ZFQ1RPUl9DT0xMRUNUSU9OX05BTUU6IFwiZGIudmVjdG9yLmNvbGxlY3Rpb24ubmFtZVwiLFxuICAgIERCX1ZFQ1RPUl9JTkRFWF9NRVRSSUM6IFwiZGIudmVjdG9yLmluZGV4Lm1ldHJpY1wiLFxuICAgIERCX1ZFQ1RPUl9JTkRFWF9ESU1FTlNJT05TOiBcImRiLnZlY3Rvci5pbmRleC5kaW1lbnNpb25zXCIsXG4gICAgREJfVkVDVE9SX05BTUVTUEFDRTogXCJkYi52ZWN0b3IubmFtZXNwYWNlXCIsXG4gIH0gYXMgY29uc3Q7XG4gIFxuICBleHBvcnQgZW51bSBGSVNwYW5LaW5kIHtcbiAgICBMTE0gPSBcIkxMTVwiLFxuICAgIENIQUlOID0gXCJDSEFJTlwiLFxuICAgIFRPT0wgPSBcIlRPT0xcIixcbiAgICBSRVRSSUVWRVIgPSBcIlJFVFJJRVZFUlwiLFxuICAgIFJFUkFOS0VSID0gXCJSRVJBTktFUlwiLFxuICAgIEVNQkVERElORyA9IFwiRU1CRURESU5HXCIsXG4gICAgQUdFTlQgPSBcIkFHRU5UXCIsXG4gICAgR1VBUkRSQUlMID0gXCJHVUFSRFJBSUxcIixcbiAgICBFVkFMVUFUT1IgPSBcIkVWQUxVQVRPUlwiLFxuICAgIFZFQ1RPUl9EQiA9IFwiVkVDVE9SX0RCXCIsXG4gICAgVU5LTk9XTiA9IFwiVU5LTk9XTlwiLFxuICB9XG4gIFxuICAvKipcbiAgICogQW4gZW51bSBvZiBjb21tb24gbWltZSB0eXBlcy4gTm90IGV4aGF1c3RpdmUuXG4gICAqL1xuICBleHBvcnQgZW51bSBNaW1lVHlwZSB7XG4gICAgVEVYVCA9IFwidGV4dC9wbGFpblwiLFxuICAgIEpTT04gPSBcImFwcGxpY2F0aW9uL2pzb25cIixcbiAgICBBVURJT19XQVYgPSBcImF1ZGlvL3dhdlwiLFxuICB9XG4gIFxuICBleHBvcnQgZW51bSBMTE1TeXN0ZW0ge1xuICAgIE9QRU5BSSA9IFwib3BlbmFpXCIsXG4gICAgQU5USFJPUElDID0gXCJhbnRocm9waWNcIixcbiAgICBNSVNUUkFMQUkgPSBcIm1pc3RyYWxhaVwiLFxuICAgIENPSEVSRSA9IFwiY29oZXJlXCIsXG4gICAgVkVSVEVYQUkgPSBcInZlcnRleGFpXCIsXG4gICAgQUkyMSA9IFwiYWkyMVwiLFxuICAgIE1FVEEgPSBcIm1ldGFcIixcbiAgICBBTUFaT04gPSBcImFtYXpvblwiLFxuICB9XG4gIFxuICBleHBvcnQgZW51bSBMTE1Qcm92aWRlciB7XG4gICAgT1BFTkFJID0gXCJvcGVuYWlcIixcbiAgICBBTlRIUk9QSUMgPSBcImFudGhyb3BpY1wiLFxuICAgIE1JU1RSQUxBSSA9IFwibWlzdHJhbGFpXCIsXG4gICAgQ09IRVJFID0gXCJjb2hlcmVcIixcbiAgICAvLyBDbG91ZCBQcm92aWRlcnMgb2YgTExNIHN5c3RlbXNcbiAgICBHT09HTEUgPSBcImdvb2dsZVwiLFxuICAgIEFXUyA9IFwiYXdzXCIsXG4gICAgQVpVUkUgPSBcImF6dXJlXCIsXG4gIH1cblxuICAvKipcbiAgICogVmVjdG9yIERhdGFiYXNlIFNlbWFudGljIENvbnZlbnRpb25zXG4gICAqXG4gICAqIEJhc2VkIG9uIE9wZW5UZWxlbWV0cnkgZGF0YWJhc2Ugc2VtYW50aWMgY29udmVudGlvbnMgd2l0aCBleHRlbnNpb25zXG4gICAqIGZvciB2ZWN0b3Itc3BlY2lmaWMgb3BlcmF0aW9ucy5cbiAgICovXG5cbiAgLy8gU2VtYW50aWMgYXR0cmlidXRlIHByZWZpeGVzIGZvciB2ZWN0b3IgZGF0YWJhc2VzXG4gIGV4cG9ydCBjb25zdCBWZWN0b3JEQkF0dHJpYnV0ZVByZWZpeGVzID0ge1xuICAgIGRiOiBcImRiXCIsXG4gICAgZGJWZWN0b3I6IFwiZGIudmVjdG9yXCIsXG4gICAgZGJWZWN0b3JRdWVyeTogXCJkYi52ZWN0b3IucXVlcnlcIixcbiAgICBkYlZlY3RvclJlc3VsdHM6IFwiZGIudmVjdG9yLnJlc3VsdHNcIixcbiAgICBkYlZlY3RvclVwc2VydDogXCJkYi52ZWN0b3IudXBzZXJ0XCIsXG4gICAgZGJWZWN0b3JEZWxldGU6IFwiZGIudmVjdG9yLmRlbGV0ZVwiLFxuICAgIGRiVmVjdG9ySW5kZXg6IFwiZGIudmVjdG9yLmluZGV4XCIsXG4gICAgZGJWZWN0b3JDb2xsZWN0aW9uOiBcImRiLnZlY3Rvci5jb2xsZWN0aW9uXCIsXG4gIH0gYXMgY29uc3Q7XG5cbiAgLy8gQ29yZSBEQiBhdHRyaWJ1dGVzIChPVEVMIHN0YW5kYXJkKVxuICBleHBvcnQgY29uc3QgREJfU1lTVEVNID0gXCJkYi5zeXN0ZW1cIiBhcyBjb25zdDtcbiAgZXhwb3J0IGNvbnN0IERCX09QRVJBVElPTl9OQU1FID0gXCJkYi5vcGVyYXRpb24ubmFtZVwiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfTkFNRVNQQUNFID0gXCJkYi5uYW1lc3BhY2VcIiBhcyBjb25zdDtcblxuICAvLyBRdWVyeSBhdHRyaWJ1dGVzXG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfUVVFUllfVE9QX0sgPSBcImRiLnZlY3Rvci5xdWVyeS50b3Bfa1wiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX1FVRVJZX0ZJTFRFUiA9IFwiZGIudmVjdG9yLnF1ZXJ5LmZpbHRlclwiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX1FVRVJZX0lOQ0xVREVfTUVUQURBVEEgPSBcImRiLnZlY3Rvci5xdWVyeS5pbmNsdWRlX21ldGFkYXRhXCIgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfUVVFUllfSU5DTFVERV9WRUNUT1JTID0gXCJkYi52ZWN0b3IucXVlcnkuaW5jbHVkZV92ZWN0b3JzXCIgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfUVVFUllfU0NPUkVfVEhSRVNIT0xEID0gXCJkYi52ZWN0b3IucXVlcnkuc2NvcmVfdGhyZXNob2xkXCIgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfUVVFUllfTUVUUklDID0gXCJkYi52ZWN0b3IucXVlcnkubWV0cmljXCIgYXMgY29uc3Q7XG5cbiAgLy8gUmVzdWx0IGF0dHJpYnV0ZXNcbiAgZXhwb3J0IGNvbnN0IERCX1ZFQ1RPUl9SRVNVTFRTX0NPVU5UID0gXCJkYi52ZWN0b3IucmVzdWx0cy5jb3VudFwiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX1JFU1VMVFNfU0NPUkVTID0gXCJkYi52ZWN0b3IucmVzdWx0cy5zY29yZXNcIiBhcyBjb25zdDtcbiAgZXhwb3J0IGNvbnN0IERCX1ZFQ1RPUl9SRVNVTFRTX0lEUyA9IFwiZGIudmVjdG9yLnJlc3VsdHMuaWRzXCIgYXMgY29uc3Q7XG5cbiAgLy8gVXBzZXJ0L0luc2VydCBhdHRyaWJ1dGVzXG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfVVBTRVJUX0NPVU5UID0gXCJkYi52ZWN0b3IudXBzZXJ0LmNvdW50XCIgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfVVBTRVJUX0RJTUVOU0lPTlMgPSBcImRiLnZlY3Rvci51cHNlcnQuZGltZW5zaW9uc1wiIGFzIGNvbnN0O1xuXG4gIC8vIERlbGV0ZSBhdHRyaWJ1dGVzXG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfREVMRVRFX0NPVU5UID0gXCJkYi52ZWN0b3IuZGVsZXRlLmNvdW50XCIgYXMgY29uc3Q7XG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfREVMRVRFX0FMTCA9IFwiZGIudmVjdG9yLmRlbGV0ZS5hbGxcIiBhcyBjb25zdDtcblxuICAvLyBJbmRleC9Db2xsZWN0aW9uIGF0dHJpYnV0ZXNcbiAgZXhwb3J0IGNvbnN0IERCX1ZFQ1RPUl9JTkRFWF9OQU1FID0gXCJkYi52ZWN0b3IuaW5kZXgubmFtZVwiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX0NPTExFQ1RJT05fTkFNRSA9IFwiZGIudmVjdG9yLmNvbGxlY3Rpb24ubmFtZVwiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX0lOREVYX01FVFJJQyA9IFwiZGIudmVjdG9yLmluZGV4Lm1ldHJpY1wiIGFzIGNvbnN0O1xuICBleHBvcnQgY29uc3QgREJfVkVDVE9SX0lOREVYX0RJTUVOU0lPTlMgPSBcImRiLnZlY3Rvci5pbmRleC5kaW1lbnNpb25zXCIgYXMgY29uc3Q7XG5cbiAgLy8gTmFtZXNwYWNlXG4gIGV4cG9ydCBjb25zdCBEQl9WRUNUT1JfTkFNRVNQQUNFID0gXCJkYi52ZWN0b3IubmFtZXNwYWNlXCIgYXMgY29uc3Q7XG5cbiAgLyoqXG4gICAqIFZlY3RvciBEYXRhYmFzZSBTZW1hbnRpYyBDb252ZW50aW9ucyBvYmplY3RcbiAgICovXG4gIGV4cG9ydCBjb25zdCBWZWN0b3JEQlNlbWFudGljQ29udmVudGlvbnMgPSB7XG4gICAgLy8gQ29yZSBEQiBhdHRyaWJ1dGVzXG4gICAgREJfU1lTVEVNLFxuICAgIERCX09QRVJBVElPTl9OQU1FLFxuICAgIERCX05BTUVTUEFDRSxcblxuICAgIC8vIFF1ZXJ5IGF0dHJpYnV0ZXNcbiAgICBEQl9WRUNUT1JfUVVFUllfVE9QX0ssXG4gICAgREJfVkVDVE9SX1FVRVJZX0ZJTFRFUixcbiAgICBEQl9WRUNUT1JfUVVFUllfSU5DTFVERV9NRVRBREFUQSxcbiAgICBEQl9WRUNUT1JfUVVFUllfSU5DTFVERV9WRUNUT1JTLFxuICAgIERCX1ZFQ1RPUl9RVUVSWV9TQ09SRV9USFJFU0hPTEQsXG4gICAgREJfVkVDVE9SX1FVRVJZX01FVFJJQyxcblxuICAgIC8vIFJlc3VsdCBhdHRyaWJ1dGVzXG4gICAgREJfVkVDVE9SX1JFU1VMVFNfQ09VTlQsXG4gICAgREJfVkVDVE9SX1JFU1VMVFNfU0NPUkVTLFxuICAgIERCX1ZFQ1RPUl9SRVNVTFRTX0lEUyxcblxuICAgIC8vIFVwc2VydC9JbnNlcnQgYXR0cmlidXRlc1xuICAgIERCX1ZFQ1RPUl9VUFNFUlRfQ09VTlQsXG4gICAgREJfVkVDVE9SX1VQU0VSVF9ESU1FTlNJT05TLFxuXG4gICAgLy8gRGVsZXRlIGF0dHJpYnV0ZXNcbiAgICBEQl9WRUNUT1JfREVMRVRFX0NPVU5ULFxuICAgIERCX1ZFQ1RPUl9ERUxFVEVfQUxMLFxuXG4gICAgLy8gSW5kZXgvQ29sbGVjdGlvbiBhdHRyaWJ1dGVzXG4gICAgREJfVkVDVE9SX0lOREVYX05BTUUsXG4gICAgREJfVkVDVE9SX0NPTExFQ1RJT05fTkFNRSxcbiAgICBEQl9WRUNUT1JfSU5ERVhfTUVUUklDLFxuICAgIERCX1ZFQ1RPUl9JTkRFWF9ESU1FTlNJT05TLFxuXG4gICAgLy8gTmFtZXNwYWNlXG4gICAgREJfVkVDVE9SX05BTUVTUEFDRSxcbiAgfSBhcyBjb25zdDtcblxuICAvKipcbiAgICogU3VwcG9ydGVkIHZlY3RvciBkYXRhYmFzZSBzeXN0ZW1zXG4gICAqL1xuICBleHBvcnQgZW51bSBWZWN0b3JEQlN5c3RlbSB7XG4gICAgQ0hST01BREIgPSBcImNocm9tYWRiXCIsXG4gICAgUElORUNPTkUgPSBcInBpbmVjb25lXCIsXG4gICAgUURSQU5UID0gXCJxZHJhbnRcIixcbiAgICBXRUFWSUFURSA9IFwid2VhdmlhdGVcIixcbiAgICBNSUxWVVMgPSBcIm1pbHZ1c1wiLFxuICAgIFBHVkVDVE9SID0gXCJwZ3ZlY3RvclwiLFxuICAgIFJFRElTID0gXCJyZWRpc1wiLFxuICAgIE1PTkdPREIgPSBcIm1vbmdvZGJcIixcbiAgICBMQU5DRURCID0gXCJsYW5jZWRiXCIsXG4gIH1cblxuICAvKipcbiAgICogVmVjdG9yIGRpc3RhbmNlL3NpbWlsYXJpdHkgbWV0cmljc1xuICAgKi9cbiAgZXhwb3J0IGVudW0gVmVjdG9yTWV0cmljIHtcbiAgICBDT1NJTkUgPSBcImNvc2luZVwiLFxuICAgIEVVQ0xJREVBTiA9IFwiZXVjbGlkZWFuXCIsXG4gICAgRE9UX1BST0RVQ1QgPSBcImRvdF9wcm9kdWN0XCIsXG4gICAgTDIgPSBcImwyXCIsXG4gICAgSVAgPSBcImlwXCIsXG4gICAgSEFNTUlORyA9IFwiaGFtbWluZ1wiLFxuICB9Il19