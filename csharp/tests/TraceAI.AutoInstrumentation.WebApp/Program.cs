using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text.Json;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;

// Usage: WebApp <mode>. The application has no tracing code unless the mode adds it. It starts a web host,
// resolves its registered AI services from the container, calls each once, and prints a JSON summary.
// An application listener of its own on the libraries' sources: it records whether any prompt or response text reaches it.
var otherListenerTags = new List<string>();
using var otherListener = new System.Diagnostics.ActivityListener
{
    ShouldListenTo = source => source.Name.StartsWith("Experimental.Microsoft.Extensions.AI") || source.Name.StartsWith("Experimental.Microsoft.Agents.AI"),
    Sample = (ref System.Diagnostics.ActivityCreationOptions<System.Diagnostics.ActivityContext> _) => System.Diagnostics.ActivitySamplingResult.AllDataAndRecorded,
    ActivityStopped = activity => { lock (otherListenerTags) otherListenerTags.AddRange(activity.TagObjects.Select(t => t.Key + "=" + t.Value)); },
};
System.Diagnostics.ActivitySource.AddActivityListener(otherListener);
var mode = args.Length > 0 ? args[0] : "plain";
var builder = WebApplication.CreateBuilder([]);
builder.WebHost.UseUrls("http://127.0.0.1:0");
builder.Logging.ClearProviders();

switch (mode)
{
    case "own_otel":
        builder.Services.AddChatClient(new StubChatClient()).UseOpenTelemetry(sourceName: "Experimental.Microsoft.Extensions.AI");
        builder.Services.AddSingleton<AIAgent>(sp => new StubAgent().AsBuilder().UseOpenTelemetry(sourceName: "Experimental.Microsoft.Agents.AI").Build());
        break;
    case "direct":
        break;
    default:
        builder.Services.AddChatClient(new StubChatClient());
        builder.Services.AddSingleton<IEmbeddingGenerator<string, Embedding<float>>>(new StubEmbeddings());
        builder.Services.AddSingleton<AIAgent>(new StubAgent());
        break;
}

var app = builder.Build();
await app.StartAsync();
var result = new Dictionary<string, object?>();
try
{
    if (mode == "direct")
    {
        // Not registered with the host: the application builds its own pipeline and gets no wrapper.
        using var client = new ChatClientBuilder(new StubChatClient()).Build();
        result["chat"] = (await client.GetResponseAsync("hello direct")).Text;
    }
    else
    {
        var chat = app.Services.GetRequiredService<IChatClient>();
        // A test may seed a secret into the prompt through FI_SECRET_KEY, which the agent knows and must scrub.
        result["chat"] = (await chat.GetResponseAsync("hello chat " + Environment.GetEnvironmentVariable("FI_SECRET_KEY"))).Text;
        result["wrappedChat"] = chat.GetService<OpenTelemetryChatClient>() != null;
        var agent = app.Services.GetService<AIAgent>();
        if (agent != null) result["agent"] = (await agent.RunAsync("hello agent")).Text;
        var embeddings = app.Services.GetService<IEmbeddingGenerator<string, Embedding<float>>>();
        if (embeddings != null) result["embeddings"] = (await embeddings.GenerateAsync(["hello embed"])).Count;
    }
    string[] seen; lock (otherListenerTags) seen = otherListenerTags.ToArray();
    result["otherListenerSawContent"] = seen.Any(t => t.Contains("hello") || t.Contains("stub answer") || t.Contains("agent answer"));
    result["otherListenerSpanTags"] = seen.Length;
    result["hostingStartupVariable"] = Environment.GetEnvironmentVariable("ASPNETCORE_HOSTINGSTARTUPASSEMBLIES");
    result["integrations"] = StatusOf();
}
finally { await app.StopAsync(); }
Console.WriteLine(JsonSerializer.Serialize(result));

static object? StatusOf()
{
    try
    {
        var context = AssemblyLoadContext.All.FirstOrDefault(c => c.Name == "traceai-auto");
        var type = context?.Assemblies.FirstOrDefault(a => a.GetName().Name == "TraceAI.AutoInstrumentation.Runtime")?.GetType("TraceAI.AutoInstrumentation.TraceAIAuto");
        var statuses = (System.Collections.IEnumerable?)type?.GetMethod("GetStatus", BindingFlags.Static | BindingFlags.Public)?.Invoke(null, null);
        return statuses?.Cast<object>().Where(s => (string)s.GetType().GetProperty("Name")!.GetValue(s)! is "extensions_ai" or "agents")
            .ToDictionary(s => (string)s.GetType().GetProperty("Name")!.GetValue(s)!, s => (string)s.GetType().GetProperty("Status")!.GetValue(s)! + "|" + (string)s.GetType().GetProperty("Reason")!.GetValue(s)!);
    }
    catch (Exception error) { return error.GetType().Name; }
}

sealed class StubChatClient : IChatClient
{
    public Task<ChatResponse> GetResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new ChatResponse(new ChatMessage(ChatRole.Assistant, "stub answer")) { ModelId = "stub-model", Usage = new UsageDetails { InputTokenCount = 3, OutputTokenCount = 2, TotalTokenCount = 5 } });
    public async IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, [EnumeratorCancellation] CancellationToken cancellationToken = default)
    { yield return new ChatResponseUpdate(ChatRole.Assistant, "stub answer"); await Task.CompletedTask; }
    public object? GetService(Type serviceType, object? serviceKey = null) => serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;
    public void Dispose() { }
}

sealed class StubEmbeddings : IEmbeddingGenerator<string, Embedding<float>>
{
    public Task<GeneratedEmbeddings<Embedding<float>>> GenerateAsync(IEnumerable<string> values, EmbeddingGenerationOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new GeneratedEmbeddings<Embedding<float>>(values.Select(_ => new Embedding<float>(new float[] { 0.1f, 0.2f })).ToList()) { Usage = new UsageDetails { InputTokenCount = 2, TotalTokenCount = 2 } });
    public object? GetService(Type serviceType, object? serviceKey = null) => serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;
    public void Dispose() { }
}

sealed class StubAgent : AIAgent
{
    protected override ValueTask<AgentSession> CreateSessionCoreAsync(CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override ValueTask<System.Text.Json.JsonElement> SerializeSessionCoreAsync(AgentSession session, System.Text.Json.JsonSerializerOptions? jsonSerializerOptions = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override ValueTask<AgentSession> DeserializeSessionCoreAsync(System.Text.Json.JsonElement serializedState, System.Text.Json.JsonSerializerOptions? jsonSerializerOptions = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override Task<AgentResponse> RunCoreAsync(IEnumerable<ChatMessage> messages, AgentSession? session = null, AgentRunOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new AgentResponse(new ChatMessage(ChatRole.Assistant, "agent answer")));
    protected override async IAsyncEnumerable<AgentResponseUpdate> RunCoreStreamingAsync(IEnumerable<ChatMessage> messages, AgentSession? session = null, AgentRunOptions? options = null, [EnumeratorCancellation] CancellationToken cancellationToken = default)
    { yield return new AgentResponseUpdate(ChatRole.Assistant, "agent answer"); await Task.CompletedTask; }
}
