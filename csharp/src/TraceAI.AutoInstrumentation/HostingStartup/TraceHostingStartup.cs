using System.Diagnostics;
using System.Reflection;
using Microsoft.Extensions.Hosting;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;

[assembly: HostingStartup(typeof(TraceAI.AutoInstrumentation.HostingStartupSupport.TraceHostingStartup))]

namespace TraceAI.AutoInstrumentation.HostingStartupSupport;

/// <summary>Wraps the application's registered chat clients, embedding generators and agents in the
/// libraries' own OpenTelemetry wrappers, so an ASP.NET Core host is traced with no application code.
/// The startup hook activates it only when the agent is running and the integration is enabled, and
/// hands it the settings through <see cref="SettingsKey"/>. Every step is fail-open.</summary>
public sealed class TraceHostingStartup : IHostingStartup
{
    /// <summary>AppContext data written by the startup hook: "chat=1;agent=1;sensitive=1".</summary>
    public const string SettingsKey = "TraceAI.AutoInstrumentation.HostingStartup";
    /// <summary>AppContext data written here: the kinds that were wrapped, e.g. "chat,agent", recorded as each is wrapped.</summary>
    public const string DecoratedKey = "TraceAI.AutoInstrumentation.HostingStartup.Decorated";

    private static int s_subscribed;

    /// <summary>The host's service collection is complete only just before the container is built, and a
    /// hosting startup runs long before that, while the application is still registering services. The host
    /// announces that moment on the "Microsoft.Extensions.Hosting" DiagnosticListener ("HostBuilding", carrying
    /// the IHostBuilder), so the decoration runs then, over every registration the application made.</summary>
    public void Configure(IWebHostBuilder builder)
    {
        try
        {
            var settings = Settings.Read(AppContext.GetData(SettingsKey) as string);
            Report("configure, settings: " + (settings == null ? "none" : settings.ToString()));
            if (settings == null || Interlocked.Exchange(ref s_subscribed, 1) == 1) return;
            DiagnosticListener.AllListeners.Subscribe(new ListenerObserver(settings));
        }
        catch (Exception error) { Report("could not start: " + error.GetType().Name); }
    }

    private sealed class ListenerObserver(Settings settings) : IObserver<DiagnosticListener>
    {
        public void OnNext(DiagnosticListener listener)
        {
            try { if (listener.Name == "Microsoft.Extensions.Hosting") listener.Subscribe(new HostingObserver(settings)); }
            catch (Exception error) { Report("could not subscribe: " + error.GetType().Name); }
        }
        public void OnError(Exception error) { }
        public void OnCompleted() { }
    }

    private sealed class HostingObserver(Settings settings) : IObserver<KeyValuePair<string, object?>>
    {
        public void OnNext(KeyValuePair<string, object?> value)
        {
            try
            {
                if (value.Key != "HostBuilding" || value.Value is not IHostBuilder hostBuilder) return;
                hostBuilder.ConfigureServices((_, services) =>
                {
                    try { Decorator.Apply(services, settings); }
                    catch (Exception error) { Report("decoration failed: " + error.GetType().Name); }
                });
            }
            catch (Exception error) { Report("could not decorate: " + error.GetType().Name); }
        }
        public void OnError(Exception error) { }
        public void OnCompleted() { }
    }

    private static readonly object WrappedGate = new();

    /// <summary>Records a kind the startup actually wrapped (not merely a registration it replaced), for the status report.</summary>
    internal static void RecordWrapped(string kind)
    {
        try
        {
            lock (WrappedGate)
            {
                var kinds = new SortedSet<string>(((AppContext.GetData(DecoratedKey) as string) ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries)) { kind };
                AppContext.SetData(DecoratedKey, string.Join(',', kinds));
            }
        }
        catch { }
    }

    private static int s_contentNoted;

    /// <summary>One value-free line per process, through the agent's warning path (standard error, "[traceAI auto]" prefix): the
    /// libraries' switch is on, so other listeners and exporters in the application receive content unscrubbed. Names only the setting.</summary>
    internal static void NoteContentOn()
    {
        try
        {
            if (Interlocked.Exchange(ref s_contentNoted, 1) == 1) return;
            Console.Error.WriteLine("[traceAI auto] Prompt and response content capture is on, so other OpenTelemetry listeners and exporters in this application receive that content unscrubbed. Set FI_AUTO_CAPTURE_CONTENT=false to turn it off.");
        }
        catch { }
    }

    internal static void Report(string message)
    {
        try { if (Environment.GetEnvironmentVariable("FI_AUTO_DEBUG") == "true") Console.Error.WriteLine("[traceAI auto] hosting startup " + message); }
        catch { }
    }
}

internal sealed record Settings(bool Chat, bool Agent, bool Sensitive)
{
    internal static Settings? Read(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        var map = raw.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(part => part.Split('=', 2)).Where(p => p.Length == 2).ToDictionary(p => p[0], p => p[1] == "1");
        bool Flag(string key) => map.GetValueOrDefault(key);
        var settings = new Settings(Flag("chat"), Flag("agent"), Flag("sensitive"));
        return settings.Chat || settings.Agent ? settings : null;
    }
}

internal static class Decorator
{
    private const string ChatClient = "Microsoft.Extensions.AI.IChatClient";
    private const string EmbeddingGenerator = "Microsoft.Extensions.AI.IEmbeddingGenerator`2";
    private const string Agent = "Microsoft.Agents.AI.AIAgent";

    internal static void Apply(IServiceCollection services, Settings settings)
    {
        var snapshot = services.ToList();
        for (var i = 0; i < snapshot.Count; i++)
        {
            var descriptor = snapshot[i];
            // Keyed services and open generics are left exactly as registered: a factory cannot stand in for an
            // open-generic registration, and the container would refuse to build.
            if (descriptor.IsKeyedService || descriptor.ServiceType.ContainsGenericParameters) continue;
            var kind = Classify(descriptor.ServiceType, settings);
            if (kind == null) continue;
            var wrapper = Wrapper.For(kind, descriptor.ServiceType);
            if (wrapper == null) continue;
            var index = services.IndexOf(descriptor);
            if (index < 0) continue;
            services[index] = Replace(descriptor, kind, wrapper, settings.Sensitive);
        }
    }

    private static ServiceDescriptor Replace(ServiceDescriptor descriptor, string kind, Wrapper wrapper, bool sensitive)
    {
        // An instance the application registered stays the application's to dispose, as before: the wrapper is built
        // now and registered as an instance too, which the container never disposes.
        if (descriptor.ImplementationInstance != null)
            return new ServiceDescriptor(descriptor.ServiceType, wrapper.Wrap(descriptor.ImplementationInstance, sensitive, kind));
        var serviceType = descriptor.ServiceType;
        var factory = descriptor.ImplementationFactory;
        var implementation = descriptor.ImplementationType!;
        // Factory and type registrations are created, and so owned, by the container, and take the same path. The container
        // disposes only what the factory returns, and the library's wrappers do not all dispose what they wrap (the agent
        // wrapper never does, and no wrapper releases an IAsyncDisposable), so the factory returns a small object that releases
        // the wrapper and the implementation exactly once, with DisposeAsync when the implementation is IAsyncDisposable and
        // the container is disposed asynchronously. A type registration is built with ActivatorUtilities, adds no descriptor and
        // uses no keyed service, so it works on any container; the application's own registrations of the implementation type
        // are neither used nor changed, and the type does not become resolvable.
        return new ServiceDescriptor(serviceType, provider =>
            Own(kind, serviceType, wrapper, sensitive, factory != null ? factory(provider) : ActivatorUtilities.CreateInstance(provider, implementation)),
            descriptor.Lifetime);
    }

    /// <summary>Wraps a registration the container created and returns the object the container will own; fails open to
    /// <paramref name="created"/> itself (untraced, released by the container as before).</summary>
    private static object Own(string kind, Type serviceType, Wrapper wrapper, bool sensitive, object created)
    {
        object? wrapped = null;
        try
        {
            // Stops the library's wrapper from disposing what it wraps (chat and embedding), so the release below is the only one.
            var guarded = Ownership.Shield(kind, serviceType, created);
            if (guarded == null) { TraceHostingStartup.Report("left a registration untraced: no ownership type for " + kind); return created; }
            wrapped = wrapper.Wrap(guarded, sensitive, kind, record: false);
            if (ReferenceEquals(wrapped, guarded)) return created; // already traced by the application, or could not be wrapped
            var owner = Ownership.Owner(kind, serviceType, wrapped, new Release(wrapped, created));
            if (owner == null) { DisposeWrapper(wrapped); TraceHostingStartup.Report("left a registration untraced: no ownership type for " + kind); return created; }
            TraceHostingStartup.RecordWrapped(kind);
            return owner;
        }
        catch (Exception error)
        {
            if (wrapped != null) DisposeWrapper(wrapped);
            TraceHostingStartup.Report("could not wrap a registration: " + error.GetType().Name);
            return created;
        }
    }

    /// <summary>Releases a library wrapper that will not be used, so its ActivitySource and Meter do not leak. The wrapper
    /// sits on a shield (chat, embedding) or on nothing it disposes (agent), so the implementation stays untouched.</summary>
    private static void DisposeWrapper(object wrapped)
    {
        try { (wrapped as IDisposable)?.Dispose(); } catch { }
    }

    private static string? Classify(Type serviceType, Settings settings)
    {
        if (serviceType.FullName == ChatClient) return settings.Chat ? "chat" : null;
        if (serviceType.IsGenericType && serviceType.GetGenericTypeDefinition().FullName == EmbeddingGenerator) return settings.Chat ? "embedding" : null;
        if (serviceType.FullName == Agent) return settings.Agent ? "agent" : null;
        return null;
    }
}

/// <summary>The library's own OpenTelemetry wrapper for one service type, found by reflection.</summary>
internal sealed class Wrapper
{
    private readonly Type _wrapper;
    private readonly ConstructorInfo _constructor;
    private readonly PropertyInfo? _sensitive;

    private Wrapper(Type wrapper, ConstructorInfo constructor)
    {
        _wrapper = wrapper; _constructor = constructor; _sensitive = wrapper.GetProperty("EnableSensitiveData");
    }

    internal static Wrapper? For(string kind, Type serviceType)
    {
        try
        {
            var name = kind switch
            {
                "chat" => "Microsoft.Extensions.AI.OpenTelemetryChatClient",
                "embedding" => "Microsoft.Extensions.AI.OpenTelemetryEmbeddingGenerator`2",
                _ => "Microsoft.Agents.AI.OpenTelemetryAgent",
            };
            var assembly = serviceType.Assembly;
            Type? wrapper = null;
            // The wrapper ships in the same package family as the abstraction, or in the package the app references.
            foreach (var candidate in AppDomain.CurrentDomain.GetAssemblies().Prepend(assembly)) { wrapper = candidate.GetType(name, false); if (wrapper != null) break; }
            wrapper ??= LoadFrom(name, kind);
            if (wrapper == null) return null;
            if (wrapper.IsGenericTypeDefinition) wrapper = wrapper.MakeGenericType(serviceType.GetGenericArguments());
            var constructor = wrapper.GetConstructors().Where(c => c.GetParameters().Length > 0 && c.GetParameters()[0].ParameterType.IsAssignableFrom(serviceType) && serviceType.IsAssignableFrom(c.GetParameters()[0].ParameterType))
                .OrderBy(c => c.GetParameters().Length).FirstOrDefault();
            return constructor == null ? null : new Wrapper(wrapper, constructor);
        }
        catch { return null; }
    }

    private static Type? LoadFrom(string name, string kind)
    {
        foreach (var assemblyName in kind == "agent" ? new[] { "Microsoft.Agents.AI" } : new[] { "Microsoft.Extensions.AI" })
        {
            try { var type = Assembly.Load(assemblyName).GetType(name, false); if (type != null) return type; } catch { }
        }
        return null;
    }

    /// <summary>The wrapped instance, or the original when it already carries the wrapper.</summary>
    internal object Wrap(object inner, bool sensitive, string kind, bool record = true)
    {
        try
        {
            // An application that already traces this client has the wrapper; it is not wrapped twice or credited.
            if (AlreadyTraced(inner)) return inner;
            var parameters = _constructor.GetParameters();
            var args = new object?[parameters.Length];
            args[0] = inner;
            for (var i = 1; i < parameters.Length; i++) args[i] = parameters[i].HasDefaultValue ? parameters[i].DefaultValue : null;
            var wrapped = _constructor.Invoke(args);
            // Content is only ever switched on when the standard variable has not said otherwise: an application that set
            // OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT set to anything but true keeps it off whatever the traceAI opt-in says.
            if (sensitive && ContentVariableForbids()) sensitive = false;
            _sensitive?.SetValue(wrapped, sensitive);
            if (sensitive && _sensitive != null) TraceHostingStartup.NoteContentOn();
            if (record) TraceHostingStartup.RecordWrapped(kind);
            return wrapped;
        }
        catch (Exception error) { TraceHostingStartup.Report("could not wrap a registration: " + error.GetType().Name); return inner; }
    }

    /// <summary>The standard variable counts as "true" only as the word true, in any case; any other non-empty value
    /// ("false", "False", "0") is the application saying no.</summary>
    private static bool ContentVariableForbids()
    {
        var value = Environment.GetEnvironmentVariable("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")?.Trim();
        return !string.IsNullOrEmpty(value) && !string.Equals(value, "true", StringComparison.OrdinalIgnoreCase);
    }

    private bool AlreadyTraced(object inner)
    {
        // Both IChatClient/IEmbeddingGenerator and AIAgent expose GetService(Type, object?) down the delegating chain.
        var method = inner.GetType().GetMethods().FirstOrDefault(m => m.Name == "GetService" && m.GetParameters().Length == 2 && m.GetParameters()[0].ParameterType == typeof(Type));
        if (method == null) return false;
        return method.Invoke(inner, new object?[] { _wrapper, null }) != null;
    }
}
