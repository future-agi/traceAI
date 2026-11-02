namespace FIInstrumentation;

/// <summary>
/// Configuration controlling attribute masking and redaction.
/// </summary>
public class TraceConfig
{
    public const string RedactedValue = "__REDACTED__";

    public bool HideLlmInvocationParameters { get; init; }
    public bool HideInputs { get; init; }
    public bool HideOutputs { get; init; }
    public bool HideInputMessages { get; init; }
    public bool HideOutputMessages { get; init; }
    public bool HideInputImages { get; init; }
    public bool HideInputText { get; init; }
    public bool HideOutputText { get; init; }
    public bool HideEmbeddingVectors { get; init; }
    internal bool? ErrorDetailsOverride { get; init; }

    /// <summary>
    /// Omits error messages, stack traces and status descriptions while preserving error types.
    /// Defaults to true when any input/output content-hiding flag is enabled.
    /// An explicit value takes precedence over that default.
    /// </summary>
    public bool HideErrorDetails
    {
        get => ErrorDetailsOverride ?? (HideInputs || HideOutputs || HideInputMessages ||
            HideOutputMessages || HideInputText || HideOutputText);
        init => ErrorDetailsOverride = value;
    }

    public int Base64ImageMaxLength { get; init; } = 32_000;

    /// <summary>
    /// Determines if a given attribute key should be masked based on config.
    /// Returns null if the attribute should be hidden entirely, or the value/redacted value otherwise.
    /// </summary>
    public string? Mask(string key, string? value)
    {
        return (string?)MaskAttribute(key, value);
    }

    // Keep attribute types unchanged unless the policy redacts or drops the value.
    internal object? MaskAttribute(string key, object? value)
    {
        if (value == null || ShouldOmit(key))
            return null;

        return ShouldHide(key) ? RedactedValue : value;
    }

    /// <summary>
    /// Determines if a given attribute key should be masked (returns redacted value).
    /// </summary>
    public string? Mask(string key, Func<string?> valueFactory)
    {
        if (ShouldOmit(key))
            return null;
        if (ShouldHide(key))
            return RedactedValue;

        return valueFactory();
    }

    private bool ShouldOmit(string key) => HideErrorDetails && (
        key is "error.message" or "exception.message" or "exception.stacktrace" ||
        key.EndsWith(".error.message", StringComparison.Ordinal) ||
        key.EndsWith(".exception.message", StringComparison.Ordinal) ||
        key.EndsWith(".exception.stacktrace", StringComparison.Ordinal) ||
        key.EndsWith(".error_message", StringComparison.Ordinal));

    // Content families, not whole namespaces: model names, tool IDs, token counts,
    // template names and other operational identifiers must remain available.
    private static readonly string[] InputContent =
    [
        SemanticConventions.InputValue, "raw.input", "llm.prompt_template.variables",
        SemanticConventions.GenAiPromptTemplateVariables, SemanticConventions.GenAiSystemInstructions,
        SemanticConventions.GenAiRequestParameters, SemanticConventions.GenAiRequestStopSequences,
        SemanticConventions.GenAiAgentDescription, SemanticConventions.GenAiImagePrompt,
        SemanticConventions.GenAiImageNegativePrompt, SemanticConventions.InputImages,
        SemanticConventions.GenAiPrompts, SemanticConventions.GenAiToolDefinitions,
        SemanticConventions.GenAiToolDescription, SemanticConventions.GenAiToolCallArguments,
        SemanticConventions.GenAiToolParameters, "tool.parameters",
        SemanticConventions.ToolCallFunctionArguments, SemanticConventions.MessageFunctionCallArgumentsJson,
        SemanticConventions.GenAiComputerUseText, SemanticConventions.GenAiComputerUseElementSelector,
        SemanticConventions.ToolJsonSchema, SemanticConventions.GenAiRetrievalQuery,
        SemanticConventions.GenAiRerankerQuery, SemanticConventions.GenAiRerankerInputDocuments,
        SemanticConventions.RerankerQuery, SemanticConventions.RerankerInputDocuments,
    ];

    private static readonly string[] OutputContent =
    [
        SemanticConventions.OutputValue, "raw.output", SemanticConventions.GenAiToolCallResult,
        SemanticConventions.GenAiImageRevisedPrompt, SemanticConventions.GenAiImageOutputUrls,
        SemanticConventions.GenAiGuardrailModifiedOutput, SemanticConventions.GenAiEvaluationExplanation,
        SemanticConventions.GenAiComputerUseResult, SemanticConventions.RetrievalDocuments,
        SemanticConventions.GenAiRetrievalDocuments, SemanticConventions.GenAiRerankerOutputDocuments,
        SemanticConventions.RerankerOutputDocuments,
    ];

    // These fields can contain either direction; the aggregate tool call is also
    // mixed, but its individual arguments/result/ID fields remain directional.
    private static readonly string[] MixedContent =
    [
        SemanticConventions.GenAiVoiceTranscript, SemanticConventions.GenAiVoiceRecordingUrl,
        SemanticConventions.GenAiVoiceRecordingStereoUrl, SemanticConventions.GenAiVoiceRecordingCustomerUrl,
        SemanticConventions.GenAiVoiceRecordingAssistantUrl, SemanticConventions.GenAiAudioUrl,
        SemanticConventions.GenAiAudioTranscript, SemanticConventions.GenAiComputerUseScreenshot,
        SemanticConventions.GenAiComputerUseCurrentUrl,
    ];

    private static readonly string[] InvocationParameters =
    [
        SemanticConventions.GenAiRequestParameters, SemanticConventions.GenAiRequestTemperature,
        SemanticConventions.GenAiRequestTopP, SemanticConventions.GenAiRequestTopK,
        SemanticConventions.GenAiRequestMaxTokens, SemanticConventions.GenAiRequestFrequencyPenalty,
        SemanticConventions.GenAiRequestPresencePenalty, SemanticConventions.GenAiRequestStopSequences,
        SemanticConventions.GenAiRequestSeed,
    ];

    private bool ShouldHide(string key)
    {
        if (HideInputs && (key == SemanticConventions.InputMimeType || HasPrefix(key, InputContent)))
            return true;
        if (HideOutputs && (key == SemanticConventions.OutputMimeType || HasPrefix(key, OutputContent)))
            return true;
        if ((HideInputs || HideOutputs) &&
            (key == SemanticConventions.GenAiToolCall || HasPrefix(key, MixedContent)))
            return true;

        if (IsFamily(key, SemanticConventions.GenAiInputMessages) &&
            (HideInputs || HideInputMessages ||
             (HideInputText && IsMessageText(key, SemanticConventions.GenAiInputMessages)) ||
             (HideInputImages && (IsMessageContainer(key, SemanticConventions.GenAiInputMessages) ||
                                 HasField(key, SemanticConventions.MessageContentImage)))))
            return true;
        if (IsFamily(key, SemanticConventions.GenAiOutputMessages) &&
            (HideOutputs || HideOutputMessages ||
             (HideOutputText && IsMessageText(key, SemanticConventions.GenAiOutputMessages))))
            return true;

        if (HideInputImages && (IsFamily(key, SemanticConventions.InputImages) ||
                               IsFamily(key, SemanticConventions.GenAiComputerUseScreenshot)))
            return true;
        if (HideEmbeddingVectors && (IsFamily(key, SemanticConventions.GenAiEmbeddingsVectors) ||
                                    IsFamily(key, SemanticConventions.EmbeddingVector)))
            return true;
        if (IsFamily(key, SemanticConventions.EmbeddingEmbeddings))
        {
            bool hideText = HideInputs || HideInputMessages || HideInputText;
            bool container = IsIndexedContainer(key, SemanticConventions.EmbeddingEmbeddings) ||
                key.EndsWith(".embedding", StringComparison.Ordinal);
            if ((HideEmbeddingVectors && (container || HasField(key, SemanticConventions.EmbeddingVector))) ||
                (hideText && (container || HasField(key, SemanticConventions.EmbeddingText))))
                return true;
        }

        return HideLlmInvocationParameters && HasPrefix(key, InvocationParameters);
    }

    private static bool IsFamily(string key, string prefix) =>
        key.StartsWith(prefix, StringComparison.Ordinal) &&
        (key.Length == prefix.Length || key[prefix.Length] == '.');

    private static bool HasPrefix(string key, string[] prefixes)
    {
        foreach (var prefix in prefixes)
            if (IsFamily(key, prefix)) return true;
        return false;
    }

    // Match complete semantic fields inside flattened indexed attributes, not
    // similarly named custom fields such as message.content_type or vector_size.
    private static bool HasField(string key, string field) => IsFamily(key, field) ||
        key.EndsWith("." + field, StringComparison.Ordinal) ||
        key.Contains("." + field + ".", StringComparison.Ordinal);

    private static bool IsMessageText(string key, string root) =>
        IsMessageContainer(key, root) || HasField(key, SemanticConventions.MessageContent) ||
        HasField(key, SemanticConventions.MessageContentText);

    private static bool IsIndexedContainer(string key, string root) =>
        key == root || (key.StartsWith(root + ".", StringComparison.Ordinal) &&
                        IsIndex(key.AsSpan(root.Length + 1)));

    private static bool IsIndex(ReadOnlySpan<char> value)
    {
        if (value.IsEmpty) return false;
        foreach (char c in value)
            if (!char.IsAsciiDigit(c)) return false;
        return true;
    }

    // Opaque JSON containers can carry hidden text/images too. Redact them as a
    // whole; when flattened, preserve sibling role/name/type and other modalities.
    private static bool IsMessageContainer(string key, string root)
    {
        if (IsIndexedContainer(key, root)) return true;
        if (!key.StartsWith(root + ".", StringComparison.Ordinal)) return false;
        var path = key[(root.Length + 1)..];
        int separator = path.IndexOf('.');
        if (separator < 0 || !IsIndex(path.AsSpan(0, separator))) return false;
        path = path[(separator + 1)..];
        if (path == "message" || IsIndexedContainer(path, SemanticConventions.MessageContents)) return true;
        if (path.EndsWith(".message_content", StringComparison.Ordinal))
            return IsIndexedContainer(path[..^".message_content".Length], SemanticConventions.MessageContents);
        return false;
    }

    public static TraceConfigBuilder Builder() => new();
}

public class TraceConfigBuilder
{
    private bool _hideLlmInvocationParameters;
    private bool _hideInputs;
    private bool _hideOutputs;
    private bool _hideInputMessages;
    private bool _hideOutputMessages;
    private bool _hideInputImages;
    private bool _hideInputText;
    private bool _hideOutputText;
    private bool _hideEmbeddingVectors;
    private bool? _hideErrorDetails;
    private int _base64ImageMaxLength = 32_000;

    public TraceConfigBuilder()
    {
        // Read from environment variables as defaults
        _hideLlmInvocationParameters = ParseBoolEnv("FI_HIDE_LLM_INVOCATION_PARAMETERS");
        _hideInputs = ParseBoolEnv("FI_HIDE_INPUTS");
        _hideOutputs = ParseBoolEnv("FI_HIDE_OUTPUTS");
        _hideInputMessages = ParseBoolEnv("FI_HIDE_INPUT_MESSAGES");
        _hideOutputMessages = ParseBoolEnv("FI_HIDE_OUTPUT_MESSAGES");
        _hideInputImages = ParseBoolEnv("FI_HIDE_INPUT_IMAGES");
        _hideInputText = ParseBoolEnv("FI_HIDE_INPUT_TEXT");
        _hideOutputText = ParseBoolEnv("FI_HIDE_OUTPUT_TEXT");
        _hideEmbeddingVectors = ParseBoolEnv("FI_HIDE_EMBEDDING_VECTORS");

        _hideErrorDetails = ParseOptionalBoolEnv("FI_HIDE_ERROR_DETAILS");

        var maxLen = Environment.GetEnvironmentVariable("FI_BASE64_IMAGE_MAX_LENGTH");
        if (int.TryParse(maxLen, out var parsed))
            _base64ImageMaxLength = parsed;
    }

    public TraceConfigBuilder HideLlmInvocationParameters(bool value = true) { _hideLlmInvocationParameters = value; return this; }
    public TraceConfigBuilder HideInputs(bool value = true) { _hideInputs = value; return this; }
    public TraceConfigBuilder HideOutputs(bool value = true) { _hideOutputs = value; return this; }
    public TraceConfigBuilder HideInputMessages(bool value = true) { _hideInputMessages = value; return this; }
    public TraceConfigBuilder HideOutputMessages(bool value = true) { _hideOutputMessages = value; return this; }
    public TraceConfigBuilder HideInputImages(bool value = true) { _hideInputImages = value; return this; }
    public TraceConfigBuilder HideInputText(bool value = true) { _hideInputText = value; return this; }
    public TraceConfigBuilder HideOutputText(bool value = true) { _hideOutputText = value; return this; }
    public TraceConfigBuilder HideEmbeddingVectors(bool value = true) { _hideEmbeddingVectors = value; return this; }
    public TraceConfigBuilder HideErrorDetails(bool value = true) { _hideErrorDetails = value; return this; }
    public TraceConfigBuilder Base64ImageMaxLength(int value) { _base64ImageMaxLength = value; return this; }

    public TraceConfig Build() => new()
    {
        HideLlmInvocationParameters = _hideLlmInvocationParameters,
        HideInputs = _hideInputs,
        HideOutputs = _hideOutputs,
        HideInputMessages = _hideInputMessages,
        HideOutputMessages = _hideOutputMessages,
        HideInputImages = _hideInputImages,
        HideInputText = _hideInputText,
        HideOutputText = _hideOutputText,
        HideEmbeddingVectors = _hideEmbeddingVectors,
        Base64ImageMaxLength = _base64ImageMaxLength,
        ErrorDetailsOverride = _hideErrorDetails,
    };

    private static bool? ParseOptionalBoolEnv(string envVar) =>
        Environment.GetEnvironmentVariable(envVar)?.Trim().ToLowerInvariant() switch
        {
            "true" or "1" or "yes" => true,
            "false" or "0" or "no" => false,
            _ => null,
        };

    private static bool ParseBoolEnv(string envVar)
    {
        var value = Environment.GetEnvironmentVariable(envVar);
        return value?.ToLowerInvariant() is "true" or "1" or "yes";
    }
}
