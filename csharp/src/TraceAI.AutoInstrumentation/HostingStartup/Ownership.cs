using System.Collections.Concurrent;
using System.Reflection;
using System.Reflection.Emit;

namespace TraceAI.AutoInstrumentation.HostingStartupSupport;

/// <summary>Releases what a wrapped, container-created registration owns, exactly once.
/// The container disposes only the object its factory returns. That is the library's OpenTelemetry wrapper, and
/// for an agent that wrapper does not dispose the agent it wraps (and no wrapper releases an IAsyncDisposable-only
/// inner), so the object handed to the container is a small subclass of the library's own delegating type that
/// forwards to this class. Public so the generated subclass can call it.</summary>
public sealed class Release
{
    private readonly object _wrapper;
    private readonly object _implementation;
    private int _done;

    internal Release(object wrapper, object implementation) { _wrapper = wrapper; _implementation = implementation; }

    /// <summary>Synchronous disposal: the wrapper (its own meters and sources), then the implementation.</summary>
    public void Sync()
    {
        if (Interlocked.Exchange(ref _done, 1) == 1) return;
        try { (_wrapper as IDisposable)?.Dispose(); }
        finally
        {
            if (_implementation is IDisposable disposable) disposable.Dispose();
            else if (_implementation is IAsyncDisposable asyncDisposable) asyncDisposable.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
    }

    /// <summary>Asynchronous disposal: IAsyncDisposable is preferred for the implementation, as the container prefers it.</summary>
    public async ValueTask Async()
    {
        if (Interlocked.Exchange(ref _done, 1) == 1) return;
        try { if (_wrapper is IAsyncDisposable a) await a.DisposeAsync(); else (_wrapper as IDisposable)?.Dispose(); }
        finally
        {
            if (_implementation is IAsyncDisposable asyncDisposable) await asyncDisposable.DisposeAsync();
            else if (_implementation is IDisposable disposable) disposable.Dispose();
        }
    }
}

/// <summary>Generates, once per library type, the two small subclasses described on <see cref="Release"/>: a
/// "shield" that stops the library's wrapper from disposing the inner it wraps, and an "owner" that is the
/// object the container owns and that forwards disposal to a <see cref="Release"/>. Everything is fail-open:
/// a null result means the caller leaves the registration unwrapped.</summary>
internal static class Ownership
{
    private static readonly Lazy<ModuleBuilder> Module = new(() =>
        AssemblyBuilder.DefineDynamicAssembly(new AssemblyName("TraceAI.AutoInstrumentation.HostingStartup.Generated"), AssemblyBuilderAccess.Run)
            .DefineDynamicModule("Generated"));
    private static readonly ConcurrentDictionary<(Type, bool), ConstructorInfo?> Cache = new();
    private static int s_counter;

    /// <summary>The delegating base type the library offers for this wrapper's service type, or null.</summary>
    private static Type? DelegatingBase(string kind, Type serviceType)
    {
        var name = kind switch
        {
            "chat" => "Microsoft.Extensions.AI.DelegatingChatClient",
            "embedding" => "Microsoft.Extensions.AI.DelegatingEmbeddingGenerator`2",
            _ => "Microsoft.Agents.AI.DelegatingAIAgent",
        };
        var type = serviceType.Assembly.GetType(name, false)
            ?? AppDomain.CurrentDomain.GetAssemblies().Select(a => a.GetType(name, false)).FirstOrDefault(t => t != null);
        if (type == null) return null;
        return type.IsGenericTypeDefinition ? type.MakeGenericType(serviceType.GetGenericArguments()) : type;
    }

    /// <summary>Wraps <paramref name="inner"/> so that disposing the result never disposes it (chat and embedding only:
    /// an agent wrapper never does). Null when it cannot be done.</summary>
    internal static object? Shield(string kind, Type serviceType, object inner)
    {
        if (kind == "agent") return inner;
        return Build(kind, serviceType, owner: false)?.Invoke(new[] { inner });
    }

    /// <summary>The object the container owns: it is the service, delegates everything to <paramref name="wrapper"/>, and
    /// releases wrapper and implementation exactly once through <paramref name="release"/>. Null when it cannot be done.</summary>
    internal static object? Owner(string kind, Type serviceType, object wrapper, Release release) =>
        Build(kind, serviceType, owner: true)?.Invoke(new[] { wrapper, release });

    private static ConstructorInfo? Build(string kind, Type serviceType, bool owner)
    {
        var baseType = DelegatingBase(kind, serviceType);
        if (baseType == null) return null;
        return Cache.GetOrAdd((baseType, owner), key =>
        {
            try { return Generate(kind, key.Item1, owner); }
            catch (Exception error) { TraceHostingStartup.Report("could not generate the ownership type: " + error.GetType().Name); return null; }
        });
    }

    private static ConstructorInfo? Generate(string kind, Type baseType, bool owner)
    {
        var baseCtor = baseType.GetConstructors(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            .FirstOrDefault(c => c.GetParameters().Length == 1);
        if (baseCtor == null) return null;
        var innerType = baseCtor.GetParameters()[0].ParameterType;
        var name = (owner ? "Owner" : "Shield") + Interlocked.Increment(ref s_counter);
        var type = Module.Value.DefineType(name, TypeAttributes.Public | TypeAttributes.Sealed, baseType);
        FieldBuilder? field = null;
        ConstructorBuilder ctor;
        if (owner)
        {
            field = type.DefineField("_release", typeof(Release), FieldAttributes.Private | FieldAttributes.InitOnly);
            ctor = type.DefineConstructor(MethodAttributes.Public, CallingConventions.Standard, new[] { innerType, typeof(Release) });
            var il = ctor.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Call, baseCtor);
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldarg_2); il.Emit(OpCodes.Stfld, field);
            il.Emit(OpCodes.Ret);
        }
        else
        {
            ctor = type.DefineConstructor(MethodAttributes.Public, CallingConventions.Standard, new[] { innerType });
            var il = ctor.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Call, baseCtor);
            il.Emit(OpCodes.Ret);
        }

        if (kind != "agent")
        {
            // protected virtual void Dispose(bool disposing): the shield does nothing (the inner stays alive);
            // the owner releases through Release and does not call the base (which would dispose the wrapper again).
            var dispose = type.DefineMethod("Dispose", MethodAttributes.Family | MethodAttributes.Virtual | MethodAttributes.HideBySig, typeof(void), new[] { typeof(bool) });
            var il = dispose.GetILGenerator();
            if (owner)
            {
                var end = il.DefineLabel();
                il.Emit(OpCodes.Ldarg_1); il.Emit(OpCodes.Brfalse_S, end);
                il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldfld, field!); il.Emit(OpCodes.Call, typeof(Release).GetMethod(nameof(Release.Sync))!);
                il.MarkLabel(end);
            }
            il.Emit(OpCodes.Ret);
        }
        else
        {
            // AIAgent is not IDisposable: the owner adds the interface.
            type.AddInterfaceImplementation(typeof(IDisposable));
            var dispose = type.DefineMethod("Dispose", MethodAttributes.Public | MethodAttributes.Virtual | MethodAttributes.Final | MethodAttributes.HideBySig | MethodAttributes.NewSlot, typeof(void), Type.EmptyTypes);
            var il = dispose.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldfld, field!); il.Emit(OpCodes.Call, typeof(Release).GetMethod(nameof(Release.Sync))!); il.Emit(OpCodes.Ret);
            type.DefineMethodOverride(dispose, typeof(IDisposable).GetMethod(nameof(IDisposable.Dispose))!);
        }

        if (owner)
        {
            type.AddInterfaceImplementation(typeof(IAsyncDisposable));
            var disposeAsync = type.DefineMethod("DisposeAsync", MethodAttributes.Public | MethodAttributes.Virtual | MethodAttributes.Final | MethodAttributes.HideBySig | MethodAttributes.NewSlot, typeof(ValueTask), Type.EmptyTypes);
            var il = disposeAsync.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Ldfld, field!); il.Emit(OpCodes.Call, typeof(Release).GetMethod(nameof(Release.Async))!); il.Emit(OpCodes.Ret);
            type.DefineMethodOverride(disposeAsync, typeof(IAsyncDisposable).GetMethod(nameof(IAsyncDisposable.DisposeAsync))!);
        }
        return type.CreateType().GetConstructors().Single();
    }
}
