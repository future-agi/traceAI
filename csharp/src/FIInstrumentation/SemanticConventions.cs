namespace FIInstrumentation;

/// <summary>
/// Semantic convention attribute keys for GenAI tracing.
/// Mirrors the Python SpanAttributes class from fi_types.py.
/// </summary>
public static class SemanticConventions
{
    // ── FI Span Kind ─────────────────────────────────────────────────
    public const string FiSpanKind = "fi.span.kind";

    // ── GenAI Core ───────────────────────────────────────────────────
    public const string GenAiOperationName = "gen_ai.operation.name";
    public const string GenAiProviderName = "gen_ai.provider.name";
    public const string GenAiSystem = "gen_ai.system";

    // ── GenAI Request ────────────────────────────────────────────────
    public const string GenAiRequestModel = "gen_ai.request.model";
    public const string GenAiRequestTemperature = "gen_ai.request.temperature";
    public const string GenAiRequestTopP = "gen_ai.request.top_p";
    public const string GenAiRequestTopK = "gen_ai.request.top_k";
    public const string GenAiRequestMaxTokens = "gen_ai.request.max_tokens";
    public const string GenAiRequestFrequencyPenalty = "gen_ai.request.frequency_penalty";
    public const string GenAiRequestPresencePenalty = "gen_ai.request.presence_penalty";
    public const string GenAiRequestStopSequences = "gen_ai.request.stop_sequences";
    public const string GenAiRequestSeed = "gen_ai.request.seed";
    public const string GenAiRequestParameters = "gen_ai.request.parameters";

    // ── GenAI Response ───────────────────────────────────────────────
    public const string GenAiResponseModel = "gen_ai.response.model";
    public const string GenAiResponseId = "gen_ai.response.id";
    public const string GenAiResponseFinishReasons = "gen_ai.response.finish_reasons";
    public const string GenAiOutputType = "gen_ai.output.type";

    // ── GenAI Usage / Tokens ─────────────────────────────────────────
    public const string GenAiUsageInputTokens = "gen_ai.usage.input_tokens";
    public const string GenAiUsageOutputTokens = "gen_ai.usage.output_tokens";
    public const string GenAiUsageTotalTokens = "gen_ai.usage.total_tokens";
    public const string GenAiUsageCacheReadTokens = "gen_ai.usage.cache_read_tokens";
    public const string GenAiUsageCacheWriteTokens = "gen_ai.usage.cache_write_tokens";

    // ── Token Detail Breakdowns ──────────────────────────────────────
    public const string GenAiUsageOutputTokensAudio = "gen_ai.usage.output_tokens.audio";
    public const string GenAiUsageOutputTokensReasoning = "gen_ai.usage.output_tokens.reasoning";
    public const string GenAiUsageInputTokensDetails = "gen_ai.usage.input_tokens.details";
    public const string GenAiUsageInputTokensAudio = "gen_ai.usage.input_tokens.audio";
    public const string GenAiUsageInputTokensCacheInput = "gen_ai.usage.input_tokens.cache_input";
    public const string GenAiUsageInputTokensCacheRead = "gen_ai.usage.input_tokens.cache_read";
    public const string GenAiUsageInputTokensCacheWrite = "gen_ai.usage.input_tokens.cache_write";

    // ── GenAI Messages ───────────────────────────────────────────────
    public const string GenAiInputMessages = "gen_ai.input.messages";
    public const string GenAiOutputMessages = "gen_ai.output.messages";
    public const string GenAiSystemInstructions = "gen_ai.system_instructions";

    // ── GenAI Tools ──────────────────────────────────────────────────
    public const string GenAiToolName = "gen_ai.tool.name";
    public const string GenAiToolDescription = "gen_ai.tool.description";
    public const string GenAiToolCallId = "gen_ai.tool.call.id";
    public const string GenAiToolCallArguments = "gen_ai.tool.call.arguments";
    public const string GenAiToolCallResult = "gen_ai.tool.call.result";
    public const string GenAiToolType = "gen_ai.tool.type";
    public const string GenAiToolDefinitions = "gen_ai.tool.definitions";
    public const string GenAiToolCall = "gen_ai.tool.call";
    public const string GenAiToolParameters = "gen_ai.tool.parameters";

    // ── GenAI Cost ───────────────────────────────────────────────────
    public const string GenAiCostTotal = "gen_ai.cost.total";
    public const string GenAiCostInput = "gen_ai.cost.input";
    public const string GenAiCostOutput = "gen_ai.cost.output";
    public const string GenAiCostCacheWrite = "gen_ai.cost.cache_write";

    // ── GenAI Prompts ────────────────────────────────────────────────
    public const string GenAiPrompts = "gen_ai.prompts";
    public const string GenAiPromptName = "gen_ai.prompt.name";
    public const string GenAiPromptVendor = "gen_ai.prompt.vendor";
    public const string GenAiPromptId = "gen_ai.prompt.id";
    public const string GenAiPromptUrl = "gen_ai.prompt.url";
    public const string GenAiPromptTemplateName = "gen_ai.prompt.template.name";
    public const string GenAiPromptTemplateVersion = "gen_ai.prompt.template.version";
    public const string GenAiPromptTemplateLabel = "gen_ai.prompt.template.label";
    public const string GenAiPromptTemplateVariables = "gen_ai.prompt.template.variables";

    // ── GenAI Conversation ───────────────────────────────────────────
    public const string GenAiConversationId = "gen_ai.conversation.id";

    // ── GenAI Agent ──────────────────────────────────────────────────
    public const string GenAiAgentId = "gen_ai.agent.id";
    public const string GenAiAgentName = "gen_ai.agent.name";
    public const string GenAiAgentDescription = "gen_ai.agent.description";
    public const string GenAiAgentGraphNodeId = "gen_ai.agent.graph.node_id";
    public const string GenAiAgentGraphNodeName = "gen_ai.agent.graph.node_name";
    public const string GenAiAgentGraphParentNodeId = "gen_ai.agent.graph.parent_node_id";

    // ── GenAI Evaluation ─────────────────────────────────────────────
    public const string GenAiEvaluationName = "gen_ai.evaluation.name";
    public const string GenAiEvaluationScoreValue = "gen_ai.evaluation.score.value";
    public const string GenAiEvaluationScoreLabel = "gen_ai.evaluation.score.label";
    public const string GenAiEvaluationExplanation = "gen_ai.evaluation.explanation";
    public const string GenAiEvaluationTargetSpanId = "gen_ai.evaluation.target_span_id";

    // ── GenAI Embeddings ─────────────────────────────────────────────
    public const string GenAiEmbeddingsDimensionCount = "gen_ai.embeddings.dimension.count";
    public const string GenAiRequestEncodingFormats = "gen_ai.request.encoding_formats";
    public const string GenAiEmbeddingsVectors = "gen_ai.embeddings.vectors";

    // ── GenAI Retrieval ──────────────────────────────────────────────
    public const string GenAiRetrievalDocuments = "gen_ai.retrieval.documents";
    public const string GenAiRetrievalQuery = "gen_ai.retrieval.query";
    public const string GenAiRetrievalTopK = "gen_ai.retrieval.top_k";

    // ── GenAI Reranker ───────────────────────────────────────────────
    public const string GenAiRerankerModel = "gen_ai.reranker.model";
    public const string GenAiRerankerQuery = "gen_ai.reranker.query";
    public const string GenAiRerankerTopN = "gen_ai.reranker.top_n";
    public const string GenAiRerankerInputDocuments = "gen_ai.reranker.input_documents";
    public const string GenAiRerankerOutputDocuments = "gen_ai.reranker.output_documents";

    // ── GenAI Guardrails ─────────────────────────────────────────────
    public const string GenAiGuardrailName = "gen_ai.guardrail.name";
    public const string GenAiGuardrailType = "gen_ai.guardrail.type";
    public const string GenAiGuardrailResult = "gen_ai.guardrail.result";
    public const string GenAiGuardrailScore = "gen_ai.guardrail.score";
    public const string GenAiGuardrailCategories = "gen_ai.guardrail.categories";
    public const string GenAiGuardrailModifiedOutput = "gen_ai.guardrail.modified_output";

    // ── GenAI Performance / Streaming ────────────────────────────────
    public const string GenAiClientOperationDuration = "gen_ai.client.operation.duration";
    public const string GenAiServerTimeToFirstToken = "gen_ai.server.time_to_first_token";
    public const string GenAiServerTimePerOutputToken = "gen_ai.server.time_per_output_token";
    public const string GenAiServerQueueTime = "gen_ai.server.queue_time";

    // ── GenAI Voice / Conversation ───────────────────────────────────
    public const string GenAiVoiceCallId = "gen_ai.voice.call_id";
    public const string GenAiVoiceProvider = "gen_ai.voice.provider";
    public const string GenAiVoiceCallDurationSecs = "gen_ai.voice.call_duration_secs";
    public const string GenAiVoiceEndedReason = "gen_ai.voice.ended_reason";
    public const string GenAiVoiceFromNumber = "gen_ai.voice.from_number";
    public const string GenAiVoiceToNumber = "gen_ai.voice.to_number";
    public const string GenAiVoiceChannelType = "gen_ai.voice.channel_type";
    public const string GenAiVoiceTranscript = "gen_ai.voice.transcript";
    public const string GenAiVoiceRecordingUrl = "gen_ai.voice.recording.url";
    public const string GenAiVoiceRecordingStereoUrl = "gen_ai.voice.recording.stereo_url";
    public const string GenAiVoiceRecordingCustomerUrl = "gen_ai.voice.recording.customer_url";
    public const string GenAiVoiceRecordingAssistantUrl = "gen_ai.voice.recording.assistant_url";
    public const string GenAiVoiceSttModel = "gen_ai.voice.stt.model";
    public const string GenAiVoiceSttProvider = "gen_ai.voice.stt.provider";
    public const string GenAiVoiceSttLanguage = "gen_ai.voice.stt.language";
    public const string GenAiVoiceTtsModel = "gen_ai.voice.tts.model";
    public const string GenAiVoiceTtsProvider = "gen_ai.voice.tts.provider";
    public const string GenAiVoiceTtsVoiceId = "gen_ai.voice.tts.voice_id";
    public const string GenAiVoiceLatencyModelAvgMs = "gen_ai.voice.latency.model_avg_ms";
    public const string GenAiVoiceLatencyVoiceAvgMs = "gen_ai.voice.latency.voice_avg_ms";
    public const string GenAiVoiceLatencyTranscriberAvgMs = "gen_ai.voice.latency.transcriber_avg_ms";
    public const string GenAiVoiceLatencyTurnAvgMs = "gen_ai.voice.latency.turn_avg_ms";
    public const string GenAiVoiceLatencyTtfbMs = "gen_ai.voice.latency.ttfb_ms";
    public const string GenAiVoiceInterruptionsUserCount = "gen_ai.voice.interruptions.user_count";
    public const string GenAiVoiceInterruptionsAssistantCount = "gen_ai.voice.interruptions.assistant_count";
    public const string GenAiVoiceCostTotal = "gen_ai.voice.cost.total";
    public const string GenAiVoiceCostStt = "gen_ai.voice.cost.stt";
    public const string GenAiVoiceCostTts = "gen_ai.voice.cost.tts";
    public const string GenAiVoiceCostLlm = "gen_ai.voice.cost.llm";
    public const string GenAiVoiceCostTelephony = "gen_ai.voice.cost.telephony";

    // ── GenAI Image Generation ───────────────────────────────────────
    public const string GenAiImagePrompt = "gen_ai.image.prompt";
    public const string GenAiImageNegativePrompt = "gen_ai.image.negative_prompt";
    public const string GenAiImageWidth = "gen_ai.image.width";
    public const string GenAiImageHeight = "gen_ai.image.height";
    public const string GenAiImageSize = "gen_ai.image.size";
    public const string GenAiImageQuality = "gen_ai.image.quality";
    public const string GenAiImageStyle = "gen_ai.image.style";
    public const string GenAiImageSteps = "gen_ai.image.steps";
    public const string GenAiImageGuidanceScale = "gen_ai.image.guidance_scale";
    public const string GenAiImageSeed = "gen_ai.image.seed";
    public const string GenAiImageFormat = "gen_ai.image.format";
    public const string GenAiImageCount = "gen_ai.image.count";
    public const string GenAiImageRevisedPrompt = "gen_ai.image.revised_prompt";
    public const string GenAiImageOutputUrls = "gen_ai.image.output_urls";

    // ── GenAI Computer Use ───────────────────────────────────────────
    public const string GenAiComputerUseAction = "gen_ai.computer_use.action";
    public const string GenAiComputerUseCoordinateX = "gen_ai.computer_use.coordinate_x";
    public const string GenAiComputerUseCoordinateY = "gen_ai.computer_use.coordinate_y";
    public const string GenAiComputerUseText = "gen_ai.computer_use.text";
    public const string GenAiComputerUseKey = "gen_ai.computer_use.key";
    public const string GenAiComputerUseButton = "gen_ai.computer_use.button";
    public const string GenAiComputerUseScrollDirection = "gen_ai.computer_use.scroll_direction";
    public const string GenAiComputerUseScrollAmount = "gen_ai.computer_use.scroll_amount";
    public const string GenAiComputerUseScreenshot = "gen_ai.computer_use.screenshot";
    public const string GenAiComputerUseEnvironment = "gen_ai.computer_use.environment";
    public const string GenAiComputerUseViewportWidth = "gen_ai.computer_use.viewport_width";
    public const string GenAiComputerUseViewportHeight = "gen_ai.computer_use.viewport_height";
    public const string GenAiComputerUseCurrentUrl = "gen_ai.computer_use.current_url";
    public const string GenAiComputerUseElementSelector = "gen_ai.computer_use.element_selector";
    public const string GenAiComputerUseResult = "gen_ai.computer_use.result";

    // ── GenAI Audio ──────────────────────────────────────────────────
    public const string GenAiAudioUrl = "gen_ai.audio.url";
    public const string GenAiAudioMimeType = "gen_ai.audio.mime_type";
    public const string GenAiAudioTranscript = "gen_ai.audio.transcript";
    public const string GenAiAudioDurationSecs = "gen_ai.audio.duration_secs";
    public const string GenAiAudioLanguage = "gen_ai.audio.language";

    // ── GenAI Simulator ──────────────────────────────────────────────
    public const string GenAiSimulatorRunTestId = "gen_ai.simulator.run_test_id";
    public const string GenAiSimulatorTestExecutionId = "gen_ai.simulator.test_execution_id";
    public const string GenAiSimulatorCallExecutionId = "gen_ai.simulator.call_execution_id";
    public const string GenAiSimulatorIsSimulatorTrace = "gen_ai.simulator.is_simulator_trace";

    // ── Input / Output (OpenInference compatible) ────────────────────
    public const string InputValue = "input.value";
    public const string InputMimeType = "input.mime_type";
    public const string OutputValue = "output.value";
    public const string OutputMimeType = "output.mime_type";
    public const string InputImages = "gen_ai.input.images";

    // ── Error ────────────────────────────────────────────────────────
    public const string ErrorType = "error.type";
    public const string ErrorMessage = "error.message";

    // ── Metadata / Tags ──────────────────────────────────────────────
    public const string Metadata = "metadata";
    public const string TagTags = "tag.tags";
    public const string SessionId = "session.id";
    public const string UserId = "user.id";

    // ── Embedding (OpenInference) ────────────────────────────────────
    public const string EmbeddingEmbeddings = "embedding.embeddings";
    public const string EmbeddingModelName = "embedding.model_name";
    public const string EmbeddingText = "embedding.text";
    public const string EmbeddingVector = "embedding.vector";

    // ── Retrieval (OpenInference) ────────────────────────────────────
    public const string RetrievalDocuments = "retrieval.documents";

    // ── Document (OpenInference) ─────────────────────────────────────
    public const string DocumentId = "document.id";
    public const string DocumentScore = "document.score";
    public const string DocumentContent = "document.content";
    public const string DocumentMetadata = "document.metadata";

    // ── Reranker (OpenInference) ─────────────────────────────────────
    public const string RerankerInputDocuments = "reranker.input_documents";
    public const string RerankerOutputDocuments = "reranker.output_documents";
    public const string RerankerQuery = "reranker.query";
    public const string RerankerModelName = "reranker.model_name";
    public const string RerankerTopK = "reranker.top_k";

    // ── Message (OpenInference) ──────────────────────────────────────
    public const string MessageRole = "message.role";
    public const string MessageContent = "message.content";
    public const string MessageContents = "message.contents";
    public const string MessageName = "message.name";
    public const string MessageToolCalls = "message.tool_calls";
    public const string MessageFunctionCallName = "message.function_call_name";
    public const string MessageFunctionCallArgumentsJson = "message.function_call_arguments_json";
    public const string MessageToolCallId = "message.tool_call_id";

    // ── Message Content (OpenInference) ──────────────────────────────
    public const string MessageContentType = "message_content.type";
    public const string MessageContentText = "message_content.text";
    public const string MessageContentImage = "message_content.image";
    public const string MessageContentAudio = "message_content.audio";
    public const string MessageAudioTranscript = "message_content.audio.transcript";
    public const string MessageContentVideo = "message_content.video";

    // ── Image (OpenInference) ────────────────────────────────────────
    public const string ImageUrl = "image.url";

    // ── Audio (OpenInference) ────────────────────────────────────────
    public const string AudioUrl = "audio.url";
    public const string AudioMimeTypeOI = "audio.mime_type";
    public const string AudioTranscriptOI = "audio.transcript";

    // ── Tool Call (OpenInference) ────────────────────────────────────
    public const string ToolCallId = "tool_call.id";
    public const string ToolCallFunctionName = "tool_call.function.name";
    public const string ToolCallFunctionArguments = "tool_call.function.arguments";
    public const string ToolJsonSchema = "tool.json_schema";

    // ── Graph ────────────────────────────────────────────────────────
    public const string GraphNodeId = "graph.node.id";
    public const string GraphNodeName = "graph.node.name";
    public const string GraphNodeParentId = "graph.node.parent_id";

    // ── Server / Infrastructure ──────────────────────────────────────
    public const string ServerAddress = "server.address";
    public const string ServerPort = "server.port";

    // ── VectorDB ─────────────────────────────────────────────────────
    public const string DbSystem = "db.system";
    public const string DbOperationName = "db.operation.name";
    public const string DbNamespace = "db.namespace";
    public const string DbVectorQueryTopK = "db.vector.query.top_k";
    public const string DbVectorQueryFilter = "db.vector.query.filter";
    public const string DbVectorQueryIncludeMetadata = "db.vector.query.include_metadata";
    public const string DbVectorQueryIncludeVectors = "db.vector.query.include_vectors";
    public const string DbVectorQueryScoreThreshold = "db.vector.query.score_threshold";
    public const string DbVectorQueryMetric = "db.vector.query.metric";
    public const string DbVectorResultsCount = "db.vector.results.count";
    public const string DbVectorResultsScores = "db.vector.results.scores";
    public const string DbVectorResultsIds = "db.vector.results.ids";
    public const string DbVectorUpsertCount = "db.vector.upsert.count";
    public const string DbVectorUpsertDimensions = "db.vector.upsert.dimensions";
    public const string DbVectorDeleteCount = "db.vector.delete.count";
    public const string DbVectorDeleteAll = "db.vector.delete.all";
    public const string DbVectorIndexName = "db.vector.index.name";
    public const string DbVectorCollectionName = "db.vector.collection.name";
    public const string DbVectorIndexMetric = "db.vector.index.metric";
    public const string DbVectorIndexDimensions = "db.vector.index.dimensions";
    public const string DbVectorNamespace = "db.vector.namespace";
}
