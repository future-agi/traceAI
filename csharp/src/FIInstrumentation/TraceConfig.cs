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
    public int Base64ImageMaxLength { get; init; } = 32_000;

    /// <summary>
    /// Determines if a given attribute key should be masked based on config.
    /// Returns null if the attribute should be hidden entirely, or the value/redacted value otherwise.
    /// </summary>
    public string? Mask(string key, string? value)
    {
        if (value == null)
            return null;

        if (ShouldHide(key))
            return RedactedValue;

        return value;
    }

    /// <summary>
    /// Determines if a given attribute key should be masked (returns redacted value).
    /// </summary>
    public string? Mask(string key, Func<string?> valueFactory)
    {
        if (ShouldHide(key))
            return RedactedValue;

        return valueFactory();
    }

    private bool ShouldHide(string key) => key switch
    {
        SemanticConventions.InputValue or
        SemanticConventions.InputMimeType
            => HideInputs,

        SemanticConventions.OutputValue or
        SemanticConventions.OutputMimeType
            => HideOutputs,

        SemanticConventions.GenAiInputMessages
            => HideInputs || HideInputMessages,

        SemanticConventions.GenAiOutputMessages
            => HideOutputs || HideOutputMessages,

        SemanticConventions.InputImages or
        SemanticConventions.GenAiComputerUseScreenshot
            => HideInputImages,

        SemanticConventions.GenAiEmbeddingsVectors or
        SemanticConventions.EmbeddingVector
            => HideEmbeddingVectors,

        SemanticConventions.GenAiRequestParameters or
        SemanticConventions.GenAiRequestTemperature or
        SemanticConventions.GenAiRequestTopP or
        SemanticConventions.GenAiRequestTopK or
        SemanticConventions.GenAiRequestMaxTokens or
        SemanticConventions.GenAiRequestFrequencyPenalty or
        SemanticConventions.GenAiRequestPresencePenalty or
        SemanticConventions.GenAiRequestStopSequences or
        SemanticConventions.GenAiRequestSeed
            => HideLlmInvocationParameters,

        _ => false,
    };

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
    };

    private static bool ParseBoolEnv(string envVar)
    {
        var value = Environment.GetEnvironmentVariable(envVar);
        return value?.ToLowerInvariant() is "true" or "1" or "yes";
    }
}
