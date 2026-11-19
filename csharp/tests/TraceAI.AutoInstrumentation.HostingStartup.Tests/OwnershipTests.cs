using System.Diagnostics;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using TraceAI.AutoInstrumentation.HostingStartupSupport;
using Xunit;

namespace TraceAI.AutoInstrumentation.HostingStartup.Tests;

/// <summary>Every type-registered implementation the hook wraps is released exactly once, with the container, and is still traced.</summary>
public class OwnershipTests
{
    private static readonly Settings Everything = new(Chat: true, Agent: true, Sensitive: false);

    public enum Shape { DisposableChat, AsyncOnlyChat, BothChat, DisposableEmbeddings, AsyncOnlyEmbeddings, DisposableAgent, AsyncOnlyAgent }

    public static IEnumerable<object[]> Cases() =>
        from shape in Enum.GetValues<Shape>() from lifetime in new[] { "singleton", "scoped", "transient" } from asyncDispose in new[] { false, true }
        select new object[] { shape, lifetime, asyncDispose };

    private static void Register(IServiceCollection services, Shape shape, string lifetime)
    {
        var life = lifetime switch { "singleton" => ServiceLifetime.Singleton, "scoped" => ServiceLifetime.Scoped, _ => ServiceLifetime.Transient };
        (Type Service, Type Implementation) pair = shape switch
        {
            Shape.DisposableChat => (typeof(IChatClient), typeof(OwnedChat)),
            Shape.AsyncOnlyChat => (typeof(IChatClient), typeof(AsyncOnlyChat)),
            Shape.BothChat => (typeof(IChatClient), typeof(BothChat)),
            Shape.DisposableEmbeddings => (typeof(IEmbeddingGenerator<string, Embedding<float>>), typeof(OwnedEmbeddings)),
            Shape.AsyncOnlyEmbeddings => (typeof(IEmbeddingGenerator<string, Embedding<float>>), typeof(AsyncOnlyEmbeddings)),
            Shape.DisposableAgent => (typeof(AIAgent), typeof(OwnedAgent)),
            _ => (typeof(AIAgent), typeof(AsyncOnlyAgent)),
        };
        services.Add(new ServiceDescriptor(pair.Service, pair.Implementation, life));
    }

    private static Type ServiceOf(Shape shape) => shape switch
    {
        Shape.DisposableChat or Shape.AsyncOnlyChat or Shape.BothChat => typeof(IChatClient),
        Shape.DisposableEmbeddings or Shape.AsyncOnlyEmbeddings => typeof(IEmbeddingGenerator<string, Embedding<float>>),
        _ => typeof(AIAgent),
    };

    [Theory]
    [MemberData(nameof(Cases))]
    public async Task ATypeRegisteredImplementationIsReleasedExactlyOnceAndStillTraced(Shape shape, string lifetime, bool asyncDispose)
    {
        Tally.All.Clear();
        var services = new ServiceCollection();
        Register(services, shape, lifetime);
        Decorator.Apply(services, Everything);
        var provider = services.BuildServiceProvider();
        var scope = provider.CreateScope();
        var resolved = scope.ServiceProvider.GetRequiredService(ServiceOf(shape));
        Assert.True(Traced(resolved, shape), "the registration is not traced");
        if (asyncDispose) { await ((IAsyncDisposable)scope).DisposeAsync(); await ((IAsyncDisposable)provider).DisposeAsync(); }
        else { scope.Dispose(); provider.Dispose(); }
        Assert.NotEmpty(Tally.All);
        Assert.All(Tally.All, item => Assert.Equal(1, item.Releases));
        // Released the way the container was disposed, and never the other way when both exist.
        if (asyncDispose && Tally.All.All(i => i.HasAsync)) Assert.All(Tally.All, i => Assert.True(i.ReleasedAsync));
        if (!asyncDispose && Tally.All.All(i => i.HasSync)) Assert.All(Tally.All, i => Assert.False(i.ReleasedAsync));
    }

    private static bool Traced(object resolved, Shape shape) => shape switch
    {
        Shape.DisposableChat or Shape.AsyncOnlyChat or Shape.BothChat => ((IChatClient)resolved).GetService<OpenTelemetryChatClient>() != null,
        Shape.DisposableEmbeddings or Shape.AsyncOnlyEmbeddings => ((IEmbeddingGenerator<string, Embedding<float>>)resolved).GetService<OpenTelemetryEmbeddingGenerator<string, Embedding<float>>>() != null,
        _ => ((AIAgent)resolved).GetService<OpenTelemetryAgent>() != null,
    };

    [Fact]
    public async Task ATypeRegisteredAgentAndChatClientStillProduceOneSpanEach()
    {
        var spans = new List<Activity>();
        using var listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name.StartsWith("Experimental.Microsoft"),
            Sample = (ref ActivityCreationOptions<ActivityContext> _) => ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = activity => { lock (spans) spans.Add(activity); },
        };
        ActivitySource.AddActivityListener(listener);
        var services = new ServiceCollection();
        services.AddSingleton<AIAgent, OwnedAgent>();
        services.AddSingleton<IChatClient, OwnedChat>();
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        await provider.GetRequiredService<AIAgent>().RunAsync("hello");
        await provider.GetRequiredService<IChatClient>().GetResponseAsync("hello");
        Assert.Equal(1, spans.Count(s => s.Source.Name == "Experimental.Microsoft.Agents.AI"));
        Assert.Equal(1, spans.Count(s => s.Source.Name == "Experimental.Microsoft.Extensions.AI" && s.OperationName.StartsWith("chat")));
    }

    [Fact]
    public async Task AHostWithAProviderThatOnlyHasGetServiceResolvesWrappedServices()
    {
        Tally.All.Clear();
        var builder = Host.CreateApplicationBuilder();
        builder.ConfigureContainer(new MinimalFactory());
        builder.Services.AddSingleton<IChatClient, OwnedChat>();
        builder.Services.AddSingleton<AIAgent, OwnedAgent>();
        Decorator.Apply(builder.Services, Everything);
        using (var host = builder.Build())
        {
            Assert.IsType<MinimalProvider>(host.Services);
            Assert.NotNull(host.Services.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
            Assert.NotNull(host.Services.GetRequiredService<AIAgent>().GetService<OpenTelemetryAgent>());
            await host.StartAsync();
            await host.StopAsync();
        }
        Assert.Equal(2, Tally.All.Count);
        Assert.All(Tally.All, item => Assert.Equal(1, item.Releases));
    }

    [Fact]
    public void AnImplementationBuiltWithTheIntendedConstructorIsUsed()
    {
        var services = new ServiceCollection();
        services.AddSingleton<IChatClient, TwoConstructors>();
        Decorator.Apply(services, Everything);
        using var provider = services.BuildServiceProvider();
        Assert.NotNull(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>());
        Assert.True(TwoConstructors.UsedMarked);
    }
}

/// <summary>Counts how the container released one implementation.</summary>
sealed class Tally(bool hasSync, bool hasAsync)
{
    public static readonly List<Tally> All = new();
    private int _releases;
    public int Releases => _releases;
    public bool ReleasedAsync { get; private set; }
    public bool HasSync { get; } = hasSync;
    public bool HasAsync { get; } = hasAsync;
    public static Tally New(bool hasSync, bool hasAsync) { var t = new Tally(hasSync, hasAsync); lock (All) All.Add(t); return t; }
    public void Sync() => Interlocked.Increment(ref _releases);
    public ValueTask Async() { ReleasedAsync = true; Interlocked.Increment(ref _releases); return ValueTask.CompletedTask; }
}

abstract class ChatBase(Tally tally) : IChatClient
{
    protected Tally Tally { get; } = tally;
    public Task<ChatResponse> GetResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new ChatResponse(new ChatMessage(ChatRole.Assistant, "answer")) { ModelId = "stub" });
    public IAsyncEnumerable<ChatResponseUpdate> GetStreamingResponseAsync(IEnumerable<ChatMessage> messages, ChatOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    public object? GetService(Type serviceType, object? serviceKey = null) => serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;
    public abstract void Dispose();
}
sealed class OwnedChat() : ChatBase(Tally.New(true, false)) { public override void Dispose() => Tally.Sync(); }
// IChatClient and IEmbeddingGenerator are IDisposable, so these are "async-first": the real release is DisposeAsync and Dispose is the synchronous fallback.
sealed class AsyncOnlyChat() : ChatBase(Tally.New(true, true)), IAsyncDisposable
{ public override void Dispose() => Tally.Sync(); public ValueTask DisposeAsync() => Tally.Async(); }
sealed class BothChat() : ChatBase(Tally.New(true, true)), IAsyncDisposable
{ public override void Dispose() => Tally.Sync(); public ValueTask DisposeAsync() => Tally.Async(); }

sealed class TwoConstructors : ChatBase
{
    public static bool UsedMarked;
    [ActivatorUtilitiesConstructor] public TwoConstructors(IServiceProvider services) : base(Tally.New(true, false)) { UsedMarked = true; }
    public TwoConstructors() : base(Tally.New(true, false)) { }
    public override void Dispose() => Tally.Sync();
}

abstract class EmbeddingsBase(Tally tally) : IEmbeddingGenerator<string, Embedding<float>>
{
    protected Tally Tally { get; } = tally;
    public Task<GeneratedEmbeddings<Embedding<float>>> GenerateAsync(IEnumerable<string> values, EmbeddingGenerationOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new GeneratedEmbeddings<Embedding<float>>(values.Select(_ => new Embedding<float>(new float[] { 0.1f })).ToList()));
    public object? GetService(Type serviceType, object? serviceKey = null) => serviceKey == null && serviceType.IsInstanceOfType(this) ? this : null;
    public abstract void Dispose();
}
sealed class OwnedEmbeddings() : EmbeddingsBase(Tally.New(true, false)) { public override void Dispose() => Tally.Sync(); }
sealed class AsyncOnlyEmbeddings() : EmbeddingsBase(Tally.New(true, true)), IAsyncDisposable
{ public override void Dispose() => Tally.Sync(); public ValueTask DisposeAsync() => Tally.Async(); }

abstract class AgentBase(Tally tally) : AIAgent
{
    protected Tally Tally { get; } = tally;
    protected override ValueTask<AgentSession> CreateSessionCoreAsync(CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override ValueTask<System.Text.Json.JsonElement> SerializeSessionCoreAsync(AgentSession session, System.Text.Json.JsonSerializerOptions? jsonSerializerOptions = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override ValueTask<AgentSession> DeserializeSessionCoreAsync(System.Text.Json.JsonElement serializedState, System.Text.Json.JsonSerializerOptions? jsonSerializerOptions = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
    protected override Task<AgentResponse> RunCoreAsync(IEnumerable<ChatMessage> messages, AgentSession? session = null, AgentRunOptions? options = null, CancellationToken cancellationToken = default) =>
        Task.FromResult(new AgentResponse(new ChatMessage(ChatRole.Assistant, "agent answer")));
    protected override IAsyncEnumerable<AgentResponseUpdate> RunCoreStreamingAsync(IEnumerable<ChatMessage> messages, AgentSession? session = null, AgentRunOptions? options = null, CancellationToken cancellationToken = default) => throw new NotSupportedException();
}
sealed class OwnedAgent() : AgentBase(Tally.New(true, false)), IDisposable { public void Dispose() => Tally.Sync(); }
sealed class AsyncOnlyAgent() : AgentBase(Tally.New(false, true)), IAsyncDisposable { public ValueTask DisposeAsync() => Tally.Async(); }

/// <summary>A container that, like some third-party ones, exposes only GetService: no keyed services, no IServiceProviderIsService.</summary>
sealed class MinimalFactory : IServiceProviderFactory<IServiceCollection>
{
    public IServiceCollection CreateBuilder(IServiceCollection services) => services;
    public IServiceProvider CreateServiceProvider(IServiceCollection services) => new MinimalProvider(services.BuildServiceProvider());
}
sealed class MinimalProvider(ServiceProvider inner) : IServiceProvider, IDisposable, IAsyncDisposable
{
    public object? GetService(Type serviceType) => serviceType == typeof(IServiceProviderIsService) || serviceType == typeof(IKeyedServiceProvider) ? null : inner.GetService(serviceType);
    public void Dispose() => inner.Dispose();
    public ValueTask DisposeAsync() => inner.DisposeAsync();
}
