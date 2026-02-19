/**
 * Semantic conventions for TraceAI tracing
 */
export declare const SemanticAttributePrefixes: {
    readonly input: "input";
    readonly output: "output";
    readonly llm: "llm";
    readonly retrieval: "retrieval";
    readonly reranker: "reranker";
    readonly messages: "messages";
    readonly message: "message";
    readonly document: "document";
    readonly embedding: "embedding";
    readonly tool: "tool";
    readonly tool_call: "tool_call";
    readonly metadata: "metadata";
    readonly tag: "tag";
    readonly session: "session";
    readonly user: "user";
    readonly traceai: "traceai";
    readonly fi: "fi";
    readonly message_content: "message_content";
    readonly image: "image";
    readonly audio: "audio";
    readonly prompt: "prompt";
};
export declare const LLMAttributePostfixes: {
    readonly provider: "provider";
    readonly system: "system";
    readonly model_name: "model_name";
    readonly token_count: "token_count";
    readonly input_messages: "input_messages";
    readonly output_messages: "output_messages";
    readonly invocation_parameters: "invocation_parameters";
    readonly prompts: "prompts";
    readonly prompt_template: "prompt_template";
    readonly function_call: "function_call";
    readonly tools: "tools";
};
export declare const LLMPromptTemplateAttributePostfixes: {
    readonly variables: "variables";
    readonly template: "template";
};
export declare const RetrievalAttributePostfixes: {
    readonly documents: "documents";
};
export declare const RerankerAttributePostfixes: {
    readonly input_documents: "input_documents";
    readonly output_documents: "output_documents";
    readonly query: "query";
    readonly model_name: "model_name";
    readonly top_k: "top_k";
};
export declare const EmbeddingAttributePostfixes: {
    readonly embeddings: "embeddings";
    readonly text: "text";
    readonly model_name: "model_name";
    readonly vector: "vector";
};
export declare const ToolAttributePostfixes: {
    readonly name: "name";
    readonly description: "description";
    readonly parameters: "parameters";
    readonly json_schema: "json_schema";
};
export declare const MessageAttributePostfixes: {
    readonly role: "role";
    readonly content: "content";
    readonly contents: "contents";
    readonly name: "name";
    readonly function_call_name: "function_call_name";
    readonly function_call_arguments_json: "function_call_arguments_json";
    readonly tool_calls: "tool_calls";
    readonly tool_call_id: "tool_call_id";
};
export declare const MessageContentsAttributePostfixes: {
    readonly type: "type";
    readonly text: "text";
    readonly image: "image";
};
export declare const ImageAttributesPostfixes: {
    readonly url: "url";
};
export declare const ToolCallAttributePostfixes: {
    readonly function_name: "function.name";
    readonly function_arguments_json: "function.arguments";
    readonly id: "id";
};
export declare const DocumentAttributePostfixes: {
    readonly id: "id";
    readonly content: "content";
    readonly score: "score";
    readonly metadata: "metadata";
};
export declare const TagAttributePostfixes: {
    readonly tags: "tags";
};
export declare const SessionAttributePostfixes: {
    readonly id: "id";
};
export declare const UserAttributePostfixes: {
    readonly id: "id";
};
export declare const AudioAttributesPostfixes: {
    readonly url: "url";
    readonly mime_type: "mime_type";
    readonly transcript: "transcript";
};
export declare const PromptAttributePostfixes: {
    readonly vendor: "vendor";
    readonly id: "id";
    readonly url: "url";
};
/**
 * The input to any span
 */
export declare const INPUT_VALUE: "input.value";
export declare const INPUT_MIME_TYPE: "input.mime_type";
/**
 * The output of any span
 */
export declare const OUTPUT_VALUE: "output.value";
export declare const OUTPUT_MIME_TYPE: "output.mime_type";
/**
 * The messages sent to the LLM for completions
 * Typically seen in OpenAI chat completions
 * @see https://beta.openai.com/docs/api-reference/completions/create
 */
export declare const LLM_INPUT_MESSAGES: "gen_ai.input.messages";
export declare const LLM_PROMPTS: "gen_ai.prompts";
export declare const LLM_INVOCATION_PARAMETERS: "gen_ai.request.parameters";
export declare const LLM_OUTPUT_MESSAGES: "gen_ai.output.messages";
export declare const LLM_MODEL_NAME: "gen_ai.request.model";
export declare const LLM_PROVIDER: "gen_ai.provider.name";
export declare const LLM_SYSTEM: "gen_ai.provider.name";
export declare const LLM_TOKEN_COUNT_COMPLETION: "gen_ai.usage.output_tokens";
export declare const LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING: "gen_ai.usage.output_tokens.reasoning";
export declare const LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO: "gen_ai.usage.output_tokens.audio";
export declare const LLM_TOKEN_COUNT_PROMPT: "gen_ai.usage.input_tokens";
export declare const LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE: "gen_ai.usage.cache_write_tokens";
export declare const LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ: "gen_ai.usage.cache_read_tokens";
export declare const LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO: "gen_ai.usage.input_tokens.audio";
export declare const LLM_TOKEN_COUNT_TOTAL: "gen_ai.usage.total_tokens";
/**
 * The role that the LLM assumes the message is from
 * during the LLM invocation
 */
export declare const MESSAGE_ROLE: "message.role";
/**
 * The name of the message. This is only used for role 'function' where the name
 * of the function is captured in the name field and the parameters are captured in the
 * content.
 */
export declare const MESSAGE_NAME: "message.name";
/**
 * The tool calls generated by the model, such as function calls.
 */
export declare const MESSAGE_TOOL_CALLS: "message.tool_calls";
/**
 * The id of the tool call on a "tool" role message
 */
export declare const MESSAGE_TOOL_CALL_ID: "message.tool_call_id";
/**
 * tool_call.function.name
 */
export declare const TOOL_CALL_FUNCTION_NAME: "tool_call.function.name";
/**
 * tool_call.function.argument (JSON string)
 */
export declare const TOOL_CALL_FUNCTION_ARGUMENTS_JSON: "tool_call.function.arguments";
/**
 * The id of the tool call
 */
export declare const TOOL_CALL_ID: "tool_call.id";
/**
 * The LLM function call function name
 */
export declare const MESSAGE_FUNCTION_CALL_NAME: "message.function_call_name";
/**
 * The LLM function call function arguments in a json string
 */
export declare const MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON: "message.function_call_arguments_json";
/**
 * The content of the message sent to the LLM
 */
export declare const MESSAGE_CONTENT: "message.content";
/**
 * The array of contents for the message sent to the LLM. Each element of the array is
 * an `message_content` object.
 */
export declare const MESSAGE_CONTENTS: "message.contents";
/**
 * The type of content sent to the LLM
 */
export declare const MESSAGE_CONTENT_TYPE: "message_content.type";
/**
 * The text content of the message sent to the LLM
 */
export declare const MESSAGE_CONTENT_TEXT: "message_content.text";
/**
 * The image content of the message sent to the LLM
 */
export declare const MESSAGE_CONTENT_IMAGE: "message_content.image";
/**
 * The http or base64 link to the image
 */
export declare const IMAGE_URL: "image.url";
export declare const DOCUMENT_ID: "document.id";
export declare const DOCUMENT_CONTENT: "document.content";
export declare const DOCUMENT_SCORE: "document.score";
export declare const DOCUMENT_METADATA: "document.metadata";
/**
 * The text that was embedded to create the vector
 */
export declare const EMBEDDING_TEXT: "embedding.text";
/**
 * The name of the model that was used to create the vector
 */
export declare const EMBEDDING_MODEL_NAME: "embedding.model_name";
/**
 * The embedding vector. Typically a high dimensional vector of floats or ints
 */
export declare const EMBEDDING_VECTOR: "embedding.vector";
/**
 * The embedding list root
 */
export declare const EMBEDDING_EMBEDDINGS: "embedding.embeddings";
/**
 * The retrieval documents list root
 */
export declare const RETRIEVAL_DOCUMENTS: "retrieval.documents";
/**
 * The JSON representation of the variables used in the prompt template
 */
export declare const PROMPT_TEMPLATE_VARIABLES: "llm.prompt_template.variables";
/**
 * A prompt template
 */
export declare const PROMPT_TEMPLATE_TEMPLATE: "llm.prompt_template.template";
/**
 * The JSON representation of a function call of an LLM
 */
export declare const LLM_FUNCTION_CALL: "gen_ai.tool.call";
export declare const LLM_TOOLS: "gen_ai.tool.definitions";
/**
 * The name of a tool
 */
export declare const TOOL_NAME: "tool.name";
/**
 * The description of a tool
 */
export declare const TOOL_DESCRIPTION: "tool.description";
/**
 * The parameters of the tool represented as a JSON string
 */
export declare const TOOL_PARAMETERS: "tool.parameters";
/**
 * The json schema of a tool input, It is RECOMMENDED that this be in the
 * OpenAI tool calling format: https://platform.openai.com/docs/assistants/tools
 */
export declare const TOOL_JSON_SCHEMA: "tool.json_schema";
/**
 * The session id of a trace. Used to correlate spans in a single session.
 */
export declare const SESSION_ID: "session.id";
/**
 * The user id of a trace. Used to correlate spans for a single user.
 */
export declare const USER_ID: "user.id";
/**
 * The documents used as input to the reranker
 */
export declare const RERANKER_INPUT_DOCUMENTS: "reranker.input_documents";
/**
 * The documents output by the reranker
 */
export declare const RERANKER_OUTPUT_DOCUMENTS: "reranker.output_documents";
/**
 * The query string for the reranker
 */
export declare const RERANKER_QUERY: "reranker.query";
/**
 * The model name for the reranker
 */
export declare const RERANKER_MODEL_NAME: "reranker.model_name";
/**
 * The top k parameter for the reranker
 */
export declare const RERANKER_TOP_K: "reranker.top_k";
/**
 * Metadata for a span, used to store user-defined key-value pairs
 */
export declare const METADATA: "metadata";
/**
 * A prompt template version
 */
export declare const PROMPT_TEMPLATE_VERSION: "llm.prompt_template.version";
/**
 * The tags associated with a span
 */
export declare const TAG_TAGS: "tag.tags";
/**
 * The url of an audio file
 */
export declare const AUDIO_URL: "audio.url";
/**
 * The audio mime type
 */
export declare const AUDIO_MIME_TYPE: "audio.mime_type";
/**
 * The audio transcript as text
 */
export declare const AUDIO_TRANSCRIPT: "audio.transcript";
/**
 * The vendor or origin of the prompt, e.g. a prompt library, a specialized service, etc.
 */
export declare const PROMPT_VENDOR: "prompt.vendor";
/**
 * A vendor-specific id used to locate the prompt
 */
export declare const PROMPT_ID: "prompt.id";
/**
 * A vendor-specific URL used to locate the prompt
 */
export declare const PROMPT_URL: "prompt.url";
export declare const GEN_AI_OPERATION_NAME: "gen_ai.operation.name";
export declare const GEN_AI_RESPONSE_MODEL: "gen_ai.response.model";
export declare const GEN_AI_RESPONSE_ID: "gen_ai.response.id";
export declare const GEN_AI_RESPONSE_FINISH_REASONS: "gen_ai.response.finish_reasons";
export declare const GEN_AI_CONVERSATION_ID: "gen_ai.conversation.id";
export declare const SemanticConventions: {
    readonly IMAGE_URL: "image.url";
    readonly INPUT_VALUE: "input.value";
    readonly INPUT_MIME_TYPE: "input.mime_type";
    readonly OUTPUT_VALUE: "output.value";
    readonly OUTPUT_MIME_TYPE: "output.mime_type";
    readonly LLM_INPUT_MESSAGES: "gen_ai.input.messages";
    readonly LLM_OUTPUT_MESSAGES: "gen_ai.output.messages";
    readonly LLM_MODEL_NAME: "gen_ai.request.model";
    readonly LLM_PROMPTS: "gen_ai.prompts";
    readonly LLM_INVOCATION_PARAMETERS: "gen_ai.request.parameters";
    readonly LLM_TOKEN_COUNT_COMPLETION: "gen_ai.usage.output_tokens";
    readonly LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING: "gen_ai.usage.output_tokens.reasoning";
    readonly LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO: "gen_ai.usage.output_tokens.audio";
    readonly LLM_TOKEN_COUNT_PROMPT: "gen_ai.usage.input_tokens";
    readonly LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE: "gen_ai.usage.cache_write_tokens";
    readonly LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ: "gen_ai.usage.cache_read_tokens";
    readonly LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO: "gen_ai.usage.input_tokens.audio";
    readonly LLM_TOKEN_COUNT_TOTAL: "gen_ai.usage.total_tokens";
    readonly LLM_SYSTEM: "gen_ai.provider.name";
    readonly LLM_PROVIDER: "gen_ai.provider.name";
    readonly LLM_TOOLS: "gen_ai.tool.definitions";
    readonly MESSAGE_ROLE: "message.role";
    readonly MESSAGE_NAME: "message.name";
    readonly MESSAGE_TOOL_CALLS: "message.tool_calls";
    readonly MESSAGE_TOOL_CALL_ID: "message.tool_call_id";
    readonly TOOL_CALL_ID: "tool_call.id";
    readonly TOOL_CALL_FUNCTION_NAME: "tool_call.function.name";
    readonly TOOL_CALL_FUNCTION_ARGUMENTS_JSON: "tool_call.function.arguments";
    readonly MESSAGE_FUNCTION_CALL_NAME: "message.function_call_name";
    readonly MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON: "message.function_call_arguments_json";
    readonly MESSAGE_CONTENT: "message.content";
    readonly MESSAGE_CONTENTS: "message.contents";
    readonly MESSAGE_CONTENT_IMAGE: "message_content.image";
    readonly MESSAGE_CONTENT_TEXT: "message_content.text";
    readonly MESSAGE_CONTENT_TYPE: "message_content.type";
    readonly DOCUMENT_ID: "document.id";
    readonly DOCUMENT_CONTENT: "document.content";
    readonly DOCUMENT_SCORE: "document.score";
    readonly DOCUMENT_METADATA: "document.metadata";
    readonly EMBEDDING_EMBEDDINGS: "embedding.embeddings";
    readonly EMBEDDING_TEXT: "embedding.text";
    readonly EMBEDDING_MODEL_NAME: "embedding.model_name";
    readonly EMBEDDING_VECTOR: "embedding.vector";
    readonly TOOL_DESCRIPTION: "tool.description";
    readonly TOOL_NAME: "tool.name";
    readonly TOOL_PARAMETERS: "tool.parameters";
    readonly TOOL_JSON_SCHEMA: "tool.json_schema";
    readonly PROMPT_TEMPLATE_VARIABLES: "llm.prompt_template.variables";
    readonly PROMPT_TEMPLATE_TEMPLATE: "llm.prompt_template.template";
    readonly PROMPT_TEMPLATE_VERSION: "llm.prompt_template.version";
    readonly RERANKER_INPUT_DOCUMENTS: "reranker.input_documents";
    readonly RERANKER_OUTPUT_DOCUMENTS: "reranker.output_documents";
    readonly RERANKER_QUERY: "reranker.query";
    readonly RERANKER_MODEL_NAME: "reranker.model_name";
    readonly RERANKER_TOP_K: "reranker.top_k";
    readonly LLM_FUNCTION_CALL: "gen_ai.tool.call";
    readonly RETRIEVAL_DOCUMENTS: "retrieval.documents";
    readonly SESSION_ID: "session.id";
    readonly USER_ID: "user.id";
    readonly METADATA: "metadata";
    readonly TAG_TAGS: "tag.tags";
    readonly FI_SPAN_KIND: "fi.span.kind";
    readonly GEN_AI_OPERATION_NAME: "gen_ai.operation.name";
    readonly GEN_AI_RESPONSE_MODEL: "gen_ai.response.model";
    readonly GEN_AI_RESPONSE_ID: "gen_ai.response.id";
    readonly GEN_AI_RESPONSE_FINISH_REASONS: "gen_ai.response.finish_reasons";
    readonly GEN_AI_CONVERSATION_ID: "gen_ai.conversation.id";
    readonly PROMPT_VENDOR: "prompt.vendor";
    readonly PROMPT_ID: "prompt.id";
    readonly PROMPT_URL: "prompt.url";
    readonly RAW_INPUT: "raw.input";
    readonly RAW_OUTPUT: "raw.output";
    readonly DB_SYSTEM: "db.system";
    readonly DB_OPERATION_NAME: "db.operation.name";
    readonly DB_NAMESPACE: "db.namespace";
    readonly DB_VECTOR_QUERY_TOP_K: "db.vector.query.top_k";
    readonly DB_VECTOR_QUERY_FILTER: "db.vector.query.filter";
    readonly DB_VECTOR_QUERY_INCLUDE_METADATA: "db.vector.query.include_metadata";
    readonly DB_VECTOR_QUERY_INCLUDE_VECTORS: "db.vector.query.include_vectors";
    readonly DB_VECTOR_QUERY_SCORE_THRESHOLD: "db.vector.query.score_threshold";
    readonly DB_VECTOR_QUERY_METRIC: "db.vector.query.metric";
    readonly DB_VECTOR_RESULTS_COUNT: "db.vector.results.count";
    readonly DB_VECTOR_RESULTS_SCORES: "db.vector.results.scores";
    readonly DB_VECTOR_RESULTS_IDS: "db.vector.results.ids";
    readonly DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count";
    readonly DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions";
    readonly DB_VECTOR_DELETE_COUNT: "db.vector.delete.count";
    readonly DB_VECTOR_DELETE_ALL: "db.vector.delete.all";
    readonly DB_VECTOR_INDEX_NAME: "db.vector.index.name";
    readonly DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name";
    readonly DB_VECTOR_INDEX_METRIC: "db.vector.index.metric";
    readonly DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions";
    readonly DB_VECTOR_NAMESPACE: "db.vector.namespace";
};
export declare enum FISpanKind {
    LLM = "LLM",
    CHAIN = "CHAIN",
    TOOL = "TOOL",
    RETRIEVER = "RETRIEVER",
    RERANKER = "RERANKER",
    EMBEDDING = "EMBEDDING",
    AGENT = "AGENT",
    GUARDRAIL = "GUARDRAIL",
    EVALUATOR = "EVALUATOR",
    VECTOR_DB = "VECTOR_DB",
    UNKNOWN = "UNKNOWN"
}
/**
 * An enum of common mime types. Not exhaustive.
 */
export declare enum MimeType {
    TEXT = "text/plain",
    JSON = "application/json",
    AUDIO_WAV = "audio/wav"
}
export declare enum LLMSystem {
    OPENAI = "openai",
    ANTHROPIC = "anthropic",
    MISTRALAI = "mistralai",
    COHERE = "cohere",
    VERTEXAI = "vertexai",
    AI21 = "ai21",
    META = "meta",
    AMAZON = "amazon"
}
export declare enum LLMProvider {
    OPENAI = "openai",
    ANTHROPIC = "anthropic",
    MISTRALAI = "mistralai",
    COHERE = "cohere",
    GOOGLE = "google",
    AWS = "aws",
    AZURE = "azure"
}
/**
 * Vector Database Semantic Conventions
 *
 * Based on OpenTelemetry database semantic conventions with extensions
 * for vector-specific operations.
 */
export declare const VectorDBAttributePrefixes: {
    readonly db: "db";
    readonly dbVector: "db.vector";
    readonly dbVectorQuery: "db.vector.query";
    readonly dbVectorResults: "db.vector.results";
    readonly dbVectorUpsert: "db.vector.upsert";
    readonly dbVectorDelete: "db.vector.delete";
    readonly dbVectorIndex: "db.vector.index";
    readonly dbVectorCollection: "db.vector.collection";
};
export declare const DB_SYSTEM: "db.system";
export declare const DB_OPERATION_NAME: "db.operation.name";
export declare const DB_NAMESPACE: "db.namespace";
export declare const DB_VECTOR_QUERY_TOP_K: "db.vector.query.top_k";
export declare const DB_VECTOR_QUERY_FILTER: "db.vector.query.filter";
export declare const DB_VECTOR_QUERY_INCLUDE_METADATA: "db.vector.query.include_metadata";
export declare const DB_VECTOR_QUERY_INCLUDE_VECTORS: "db.vector.query.include_vectors";
export declare const DB_VECTOR_QUERY_SCORE_THRESHOLD: "db.vector.query.score_threshold";
export declare const DB_VECTOR_QUERY_METRIC: "db.vector.query.metric";
export declare const DB_VECTOR_RESULTS_COUNT: "db.vector.results.count";
export declare const DB_VECTOR_RESULTS_SCORES: "db.vector.results.scores";
export declare const DB_VECTOR_RESULTS_IDS: "db.vector.results.ids";
export declare const DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count";
export declare const DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions";
export declare const DB_VECTOR_DELETE_COUNT: "db.vector.delete.count";
export declare const DB_VECTOR_DELETE_ALL: "db.vector.delete.all";
export declare const DB_VECTOR_INDEX_NAME: "db.vector.index.name";
export declare const DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name";
export declare const DB_VECTOR_INDEX_METRIC: "db.vector.index.metric";
export declare const DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions";
export declare const DB_VECTOR_NAMESPACE: "db.vector.namespace";
/**
 * Vector Database Semantic Conventions object
 */
export declare const VectorDBSemanticConventions: {
    readonly DB_SYSTEM: "db.system";
    readonly DB_OPERATION_NAME: "db.operation.name";
    readonly DB_NAMESPACE: "db.namespace";
    readonly DB_VECTOR_QUERY_TOP_K: "db.vector.query.top_k";
    readonly DB_VECTOR_QUERY_FILTER: "db.vector.query.filter";
    readonly DB_VECTOR_QUERY_INCLUDE_METADATA: "db.vector.query.include_metadata";
    readonly DB_VECTOR_QUERY_INCLUDE_VECTORS: "db.vector.query.include_vectors";
    readonly DB_VECTOR_QUERY_SCORE_THRESHOLD: "db.vector.query.score_threshold";
    readonly DB_VECTOR_QUERY_METRIC: "db.vector.query.metric";
    readonly DB_VECTOR_RESULTS_COUNT: "db.vector.results.count";
    readonly DB_VECTOR_RESULTS_SCORES: "db.vector.results.scores";
    readonly DB_VECTOR_RESULTS_IDS: "db.vector.results.ids";
    readonly DB_VECTOR_UPSERT_COUNT: "db.vector.upsert.count";
    readonly DB_VECTOR_UPSERT_DIMENSIONS: "db.vector.upsert.dimensions";
    readonly DB_VECTOR_DELETE_COUNT: "db.vector.delete.count";
    readonly DB_VECTOR_DELETE_ALL: "db.vector.delete.all";
    readonly DB_VECTOR_INDEX_NAME: "db.vector.index.name";
    readonly DB_VECTOR_COLLECTION_NAME: "db.vector.collection.name";
    readonly DB_VECTOR_INDEX_METRIC: "db.vector.index.metric";
    readonly DB_VECTOR_INDEX_DIMENSIONS: "db.vector.index.dimensions";
    readonly DB_VECTOR_NAMESPACE: "db.vector.namespace";
};
/**
 * Supported vector database systems
 */
export declare enum VectorDBSystem {
    CHROMADB = "chromadb",
    PINECONE = "pinecone",
    QDRANT = "qdrant",
    WEAVIATE = "weaviate",
    MILVUS = "milvus",
    PGVECTOR = "pgvector",
    REDIS = "redis",
    MONGODB = "mongodb",
    LANCEDB = "lancedb"
}
/**
 * Vector distance/similarity metrics
 */
export declare enum VectorMetric {
    COSINE = "cosine",
    EUCLIDEAN = "euclidean",
    DOT_PRODUCT = "dot_product",
    L2 = "l2",
    IP = "ip",
    HAMMING = "hamming"
}
