using System.Diagnostics;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;
using TraceAI.AutoInstrumentation.HostingStartupSupport;
using Xunit;

namespace TraceAI.AutoInstrumentation.HostingStartup.Tests;

/// <summary>The decoration the hosting startup applies to a service collection, against the real
/// Microsoft.Extensions.AI and Microsoft.Agents.AI packages.</summary>
public class DecoratorTests
{
    private const string ChatSource = "Experimental.Microsoft.Extensions.AI";
    private const string AgentSource = "Experimental.Microsoft.Agents.AI";
    private static readonly Settings Everything = new(Chat: true, Agent: true, Sensitive: true);

    private static (List<Activity> Spans, IDisposable Listener) Listen()
    {
        var spans = new List<Activity>();
        var listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name.StartsWith("Experimental.Microsoft") || source.Name.StartsWith("Microsoft.Extensions.AI") || source.Name.StartsWith("Microsoft.Agents.AI"),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = activity => { lock (spans) spans.Add(activity); },
        };
        ActivitySource.AddActivityListener(listener);
        return (spans, listener);
    }

    private static string Names(List<Activity> spans) => string.Join(", ", spans.Select(s => s.Source.Name + ":" + s.OperationName));

    [Fact]
    public async Task ARegisteredChatClientIsWrappedAndProducesOneSpanPerCall()
    {
        var (spans, listener) = Listen();
        using var _ = listener;
        var services = new ServiceCollection(); services.AddChatClient(new StubChat());
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        var client = provider.GetRequiredService<IChatClient>();
        Assert.NotNull(client.GetService<OpenTelemetryChatClient>());
        await client.GetResponseAsync("hello");
        Assert.True(spans.Count(s => s.Source.Name == ChatSource && s.OperationName.StartsWith("chat")) == 1, Names(spans));
        Assert.Contains("chat", (string?)AppContext.GetData(TraceHostingStartup.DecoratedKey));
    }

    [Theory]
    [InlineData("instance")]
    [InlineData("factory")]
    [InlineData("type")]
    public void EveryRegistrationShapeIsWrapped(string shape)
    {
        var services = new ServiceCollection();
        switch (shape)
        {
            case "instance": services.AddSingleton<IChatClient>(new StubChat()); break;
            case "factory": services.AddSingleton<IChatClient>(_ => new StubChat()); break;
            default: services.AddSingleton<IChatClient, StubChat>(); break;
        }
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        Assert.NotNull(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
    }

    [Fact]
    public async Task AnApplicationThatAlreadyCallsUseOpenTelemetryGetsExactlyOneSpanPerCall()
    {
        var (spans, listener) = Listen();
        using var _ = listener;
        var services = new ServiceCollection();
        services.AddChatClient(new StubChat()).UseOpenTelemetry(sourceName: "Experimental.Microsoft.Extensions.AI");
        services.AddSingleton<AIAgent>(_ => new StubAgent().AsBuilder().UseOpenTelemetry(sourceName: "Experimental.Microsoft.Agents.AI").Build());
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        await provider.GetRequiredService<IChatClient>().GetResponseAsync("hello");
        await provider.GetRequiredService<AIAgent>().RunAsync("hello");
        Assert.True(spans.Count(s => s.Source.Name == ChatSource && s.OperationName.StartsWith("chat")) == 1, Names(spans));
        Assert.True(spans.Count(s => s.Source.Name == AgentSource) == 1, Names(spans));
    }

    [Fact]
    public async Task EmbeddingGeneratorsAndAgentsAreWrapped()
    {
        var (spans, listener) = Listen();
        using var _ = listener;
        var services = new ServiceCollection();
        services.AddSingleton<IEmbeddingGenerator<string, Embedding<float>>>(new StubEmbeddings());
        services.AddSingleton<AIAgent>(new StubAgent());
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        await provider.GetRequiredService<IEmbeddingGenerator<string, Embedding<float>>>().GenerateAsync(["hello"]);
        await provider.GetRequiredService<AIAgent>().RunAsync("hello");
        Assert.True(spans.Count(s => s.OperationName.StartsWith("embeddings")) == 1, Names(spans));
        Assert.True(spans.Count(s => s.Source.Name == AgentSource) == 1, Names(spans));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ContentCaptureFollowsTheTraceAiHideSettings(bool sensitive)
    {
        var (spans, listener) = Listen();
        using var _ = listener;
        var services = new ServiceCollection(); services.AddChatClient(new StubChat());
        Decorator.Apply(services, Everything with { Sensitive = sensitive });
        using var provider = services.BuildServiceProvider();
        await provider.GetRequiredService<IChatClient>().GetResponseAsync("PROMPT-CANARY");
        var span = Assert.Single(spans, s => s.Source.Name == ChatSource);
        var dump = string.Join(" ", span.TagObjects.Select(t => t.Key + "=" + t.Value));
        Assert.Equal(sensitive, dump.Contains("PROMPT-CANARY"));
    }

    [Fact]
    public void DisabledKindsKeepTheirRegistrationsUntouched()
    {
        var services = new ServiceCollection();
        services.AddChatClient(new StubChat());
        services.AddSingleton<AIAgent>(new StubAgent());
        Decorator.Apply(services, new Settings(Chat: false, Agent: true, Sensitive: true));
        using var provider = services.BuildServiceProvider();
        Assert.Null(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
        Assert.NotNull(provider.GetRequiredService<AIAgent>().GetService<OpenTelemetryAgent>());
    }

    [Fact]
    public void AnOpenGenericEmbeddingRegistrationIsLeftAloneAndTheHostStillBuilds()
    {
        var services = new ServiceCollection();
        services.AddSingleton(typeof(IEmbeddingGenerator<,>), typeof(OpenEmbeddings<,>));
        var before = services.ToList();
        Decorator.Apply(services, Everything);
        Assert.Equal(before, services.ToList());
        using var provider = services.BuildServiceProvider(new ServiceProviderOptions { ValidateOnBuild = true });
        Assert.NotNull(provider.GetService<IEmbeddingGenerator<string, Embedding<float>>>());
    }

    [Fact]
    public void AnInstanceTheApplicationOwnsIsNotDisposedByTheContainer()
    {
        var instance = new StubChat();
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient>(instance);
        Decorator.Apply(services, Everything);
        var provider = services.BuildServiceProvider();
        Assert.NotNull(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
        provider.Dispose();
        Assert.False(instance.Disposed);
    }

    [Fact]
    public void AnInstanceTheContainerCreatesStillBelongsToTheContainer()
    {
        StubChat? created = null;
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient>(_ => created = new StubChat());
        Decorator.Apply(services, Everything);
        var provider = services.BuildServiceProvider();
        provider.GetRequiredService<IChatClient>();
        provider.Dispose();
        Assert.True(created!.Disposed);
    }

    [Fact]
    public void ATypeRegistrationKeepsItsBuildTimeValidation()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient, NeedsMissingDependency>();
        Decorator.Apply(services, Everything);
        Assert.Throws<AggregateException>(() => services.BuildServiceProvider(new ServiceProviderOptions { ValidateOnBuild = true }));
    }

    private static ServiceProviderOptions Strict => new() { ValidateScopes = true, ValidateOnBuild = true };

    [Fact]
    public void AScopedImplementationBehindASingletonServiceKeepsItsOwnLifetime()
    {
        // The application's own scoped registration of the implementation must not be what the singleton resolves:
        // that would throw under scope validation, which development hosts turn on.
        var services = new ServiceCollection();
        services.AddScoped<StubChat>();
        services.AddSingleton<IChatClient, StubChat>();
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider(Strict);
        Assert.NotNull(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
    }

    [Fact]
    public void ASingletonTheApplicationRegisteredIsNotSharedWithOrDisposedByAScopedService()
    {
        var services = new ServiceCollection();
        services.AddSingleton<StubChat>();
        services.AddScoped<IChatClient, StubChat>();
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider(Strict);
        var applications = provider.GetRequiredService<StubChat>();
        using (var scope = provider.CreateScope()) scope.ServiceProvider.GetRequiredService<IChatClient>();
        Assert.False(applications.Disposed);
    }

    [Fact]
    public void ATypeRegistrationDoesNotMakeItsImplementationResolvable()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient, StubChat>();
        Decorator.Apply(services, Everything);
        Assert.DoesNotContain(services, d => !d.IsKeyedService && d.ServiceType == typeof(StubChat));
        using var provider = services.BuildServiceProvider(Strict);
        Assert.Null(provider.GetService<StubChat>());
        Assert.Empty(provider.GetServices<StubChat>());
    }

    [Fact]
    public async Task TheStandardContentVariableSetToFalseIsNotOverriddenToTrue()
    {
        var (spans, listener) = Listen();
        using var _ = listener;
        var previous = Environment.GetEnvironmentVariable("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT");
        try
        {
            Environment.SetEnvironmentVariable("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false");
            var services = new ServiceCollection(); services.AddChatClient(new StubChat());
            Decorator.Apply(services, Everything with { Sensitive = true });
            using var provider = services.BuildServiceProvider();
            await provider.GetRequiredService<IChatClient>().GetResponseAsync("PROMPT-CANARY");
        }
        finally { Environment.SetEnvironmentVariable("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", previous); }
        var span = Assert.Single(spans, s => s.Source.Name == ChatSource);
        Assert.DoesNotContain("PROMPT-CANARY", string.Join(" ", span.TagObjects.Select(t => t.Key + "=" + t.Value)));
    }

    [Fact]
    public void KeyedAndUnrelatedRegistrationsAreLeftAlone()
    {
        var services = new ServiceCollection();
        services.AddKeyedSingleton<IChatClient>("k", new StubChat());
        services.AddSingleton<ICloneable>(new object[0]);
        var before = services.ToList();
        Decorator.Apply(services, Everything);
        Assert.Equal(before, services.ToList());
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("chat=0;agent=0;sensitive=1")]
    public void NoSettingsOrNothingEnabledMeansNoDecoration(string? raw) => Assert.Null(Settings.Read(raw));

    [Fact]
    public void SettingsRoundTrip() => Assert.Equal(new Settings(true, false, true), Settings.Read("chat=1;agent=0;sensitive=1"));
}

sealed class OpenEmbeddings<TInput, TEmbedding> : IEmbeddingGenerator<TInput, TEmbedding> where TEmbedding : Embedding
{
    public Task<GeneratedEmbeddings<TEmbedding>> GenerateAsync(IEnumerable<TInput> values, EmbeddingGenerationOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    public object? GetService(Type serviceType, object? serviceKey = null) => null;
    public void Dispose() { }
}

sealed class NeedsMissingDependency(Uri missing) : IChatClient
{
    public Task<ChatResponse> GetResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    public IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    public object? GetService(Type serviceType, object? serviceKey = null) => null;
    public void Dispose() { }
}

sealed class StubChat : IChatClient
{
    public bool Disposed { get; private set; }
    public Task<ChatResponse> GetResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new ChatResponse(new ChatMessage(ChatRole.Assistant, "answer")) { ModelId = "stub" });
    public IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    public object? GetService(Type serviceType, object? serviceKey = null) => serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;
    public void Dispose() => Disposed = true;
}

sealed class StubEmbeddings : IEmbeddingGenerator<string, Embedding<float>>
{
    public Task<GeneratedEmbeddings<Embedding<float>>> GenerateAsync(IEnumerable<string> values, EmbeddingGenerationOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new GeneratedEmbeddings<Embedding<float>>(values.Select(_ => new Embedding<float>(new float[] { 0.1f })).ToList()));
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
    protected override IAsyncEnumerable<AgentResponseUpdate> RunCoreStreamingAsync(IEnumerable<ChatMessage> messages, AgentSession? session = null, AgentRunOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
}
