namespace FIInstrumentation.Context;

/// <summary>
/// Thread/async-local context attributes that automatically apply to spans.
/// C# equivalent of Python's using_session(), using_metadata(), etc.
/// </summary>
public static class ContextAttributes
{
    private static readonly AsyncLocal<string?> s_sessionId = new();
    private static readonly AsyncLocal<string?> s_userId = new();
    private static readonly AsyncLocal<Dictionary<string, object>?> s_metadata = new();
    private static readonly AsyncLocal<List<string>?> s_tags = new();
    private static readonly AsyncLocal<string?> s_promptTemplate = new();
    private static readonly AsyncLocal<string?> s_promptTemplateLabel = new();
    private static readonly AsyncLocal<string?> s_promptTemplateVersion = new();
    private static readonly AsyncLocal<Dictionary<string, object>?> s_promptTemplateVariables = new();

    public static IDisposable UsingSession(string sessionId) =>
        new ContextScope<string?>(s_sessionId, sessionId);

    public static IDisposable UsingUser(string userId) =>
        new ContextScope<string?>(s_userId, userId);

    public static IDisposable UsingMetadata(Dictionary<string, object> metadata) =>
        new ContextScope<Dictionary<string, object>?>(s_metadata, metadata);

    public static IDisposable UsingTags(List<string> tags) =>
        new ContextScope<List<string>?>(s_tags, tags);

    public static IDisposable UsingPromptTemplate(
        string template,
        string? label = null,
        string? version = null,
        Dictionary<string, object>? variables = null)
    {
        var disposables = new CompositeDisposable(
            new ContextScope<string?>(s_promptTemplate, template),
            label != null ? new ContextScope<string?>(s_promptTemplateLabel, label) : null,
            version != null ? new ContextScope<string?>(s_promptTemplateVersion, version) : null,
            variables != null ? new ContextScope<Dictionary<string, object>?>(s_promptTemplateVariables, variables) : null
        );
        return disposables;
    }

    public static IDisposable UsingAttributes(
        string? sessionId = null,
        string? userId = null,
        Dictionary<string, object>? metadata = null,
        List<string>? tags = null,
        string? promptTemplate = null,
        string? promptTemplateLabel = null,
        string? promptTemplateVersion = null,
        Dictionary<string, object>? variables = null)
    {
        var disposables = new CompositeDisposable(
            sessionId != null ? new ContextScope<string?>(s_sessionId, sessionId) : null,
            userId != null ? new ContextScope<string?>(s_userId, userId) : null,
            metadata != null ? new ContextScope<Dictionary<string, object>?>(s_metadata, metadata) : null,
            tags != null ? new ContextScope<List<string>?>(s_tags, tags) : null,
            promptTemplate != null ? new ContextScope<string?>(s_promptTemplate, promptTemplate) : null,
            promptTemplateLabel != null ? new ContextScope<string?>(s_promptTemplateLabel, promptTemplateLabel) : null,
            promptTemplateVersion != null ? new ContextScope<string?>(s_promptTemplateVersion, promptTemplateVersion) : null,
            variables != null ? new ContextScope<Dictionary<string, object>?>(s_promptTemplateVariables, variables) : null
        );
        return disposables;
    }

    /// <summary>
    /// Returns all context attributes currently set, as key-value pairs.
    /// </summary>
    public static IEnumerable<KeyValuePair<string, string>> GetAttributesFromContext()
    {
        if (!string.IsNullOrEmpty(s_sessionId.Value))
            yield return new(SemanticConventions.SessionId, s_sessionId.Value);

        if (!string.IsNullOrEmpty(s_userId.Value))
            yield return new(SemanticConventions.UserId, s_userId.Value);

        if (s_metadata.Value != null)
            yield return new(SemanticConventions.Metadata, FISpan.SafeJsonSerialize(s_metadata.Value));

        if (s_tags.Value != null)
            yield return new(SemanticConventions.TagTags, FISpan.SafeJsonSerialize(s_tags.Value));

        if (!string.IsNullOrEmpty(s_promptTemplate.Value))
            yield return new(SemanticConventions.GenAiPromptTemplateName, s_promptTemplate.Value);

        if (!string.IsNullOrEmpty(s_promptTemplateLabel.Value))
            yield return new(SemanticConventions.GenAiPromptTemplateLabel, s_promptTemplateLabel.Value);

        if (!string.IsNullOrEmpty(s_promptTemplateVersion.Value))
            yield return new(SemanticConventions.GenAiPromptTemplateVersion, s_promptTemplateVersion.Value);

        if (s_promptTemplateVariables.Value != null)
            yield return new(SemanticConventions.GenAiPromptTemplateVariables,
                FISpan.SafeJsonSerialize(s_promptTemplateVariables.Value));
    }
}

/// <summary>
/// Sets an AsyncLocal value and restores the previous value on dispose.
/// </summary>
internal sealed class ContextScope<T> : IDisposable
{
    private readonly AsyncLocal<T> _local;
    private readonly T _previous;

    public ContextScope(AsyncLocal<T> local, T value)
    {
        _local = local;
        _previous = local.Value!;
        _local.Value = value;
    }

    public void Dispose() => _local.Value = _previous;
}

/// <summary>
/// Disposes multiple IDisposable instances together.
/// </summary>
internal sealed class CompositeDisposable : IDisposable
{
    private readonly IDisposable?[] _disposables;

    public CompositeDisposable(params IDisposable?[] disposables) =>
        _disposables = disposables;

    public void Dispose()
    {
        for (int i = _disposables.Length - 1; i >= 0; i--)
            _disposables[i]?.Dispose();
    }
}
