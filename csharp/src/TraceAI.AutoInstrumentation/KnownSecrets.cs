using System.Collections;

namespace TraceAI.AutoInstrumentation;

/// <summary>BCL-only process state: shared by the private hook and default load contexts.
/// No activity references, per-trace groups or diagnostic representation of values.</summary>
internal static class KnownSecrets
{
    private const string StateKey="TraceAI.AutoInstrumentation.KnownSecrets.v2";
    internal const int Capacity=1024;
    internal const int MaxLength=4096;
    // gate, LRU index/list, immutable sorted snapshot, failed, environment seeded, dirty, pinned (outside the LRU: never evicted, not bounded).
    private static readonly object[] State=ProcessState();
    private static object[] ProcessState()
    {
        lock(AppDomain.CurrentDomain)
        {
            if(AppDomain.CurrentDomain.GetData(StateKey) is object[] existing) return existing;
            object[] created=[new object(),new Dictionary<string,LinkedListNode<string>>(StringComparer.Ordinal),new LinkedList<string>(),Array.Empty<string>(),false,false,false,new HashSet<string>(StringComparer.Ordinal)];
            AppDomain.CurrentDomain.SetData(StateKey,created); return created;
        }
    }
    internal static void Seed(AutoConfig config)
    {
        try
        {
            lock(State[0])
            {
                if(State[5] is not true)
                {
                    State[5]=true;
                    foreach(DictionaryEntry pair in Environment.GetEnvironmentVariables())
                        if(pair.Key is string name && pair.Value is string value &&
                            new[] { "KEY","TOKEN","SECRET","PASSWORD","PASSWD","CREDENTIAL","AUTH" }.Any(part=>name.Contains(part,StringComparison.OrdinalIgnoreCase)))
                        {
                            // A value this long is not matched within budget. Skip it with a warning
                            // that names the variable, never its value, rather than hiding all content.
                            if(value.Length>MaxLength) TraceAIAuto.Warn("secret-length-"+name,"Environment variable "+name+" is longer than 4,096 characters and is not used for redaction.");
                            else Add(value);
                        }
                }
                Pin(config.CredentialValues);
            }
        }
        catch { FailClosed(); }
    }
    internal static void Register(string? value)
    {
        if(value==null || value.Length<8) return;
        try { lock(State[0]) Add(value); }
        catch { FailClosed(); }
    }
    // Secrets the application registered through RegisterSecret, kept apart from the environment-seeded and wire-observed values: only these
    // (with the Future AGI key values) decide whether another collector may be given ambient OTLP headers.
    private static readonly HashSet<string> Explicit=new(StringComparer.Ordinal);
    private static readonly Queue<string> ExplicitOrder=new();
    /// <summary>Registers a secret the application named through <c>TraceAIAuto.RegisterSecret</c>: redacted like any known secret, and screened for in ambient exporter headers.</summary>
    internal static void RegisterExplicit(string? value)
    {
        if(value==null || value.Length<8) return;
        Register(value);
        if(value.Length>MaxLength) return;
        lock(State[0])
        {
            if(!Explicit.Add(value)) return;
            ExplicitOrder.Enqueue(value);
            // Bounded like the registry: the oldest registration goes first, so the most recent ones are always screened for.
            if(Explicit.Count>Capacity) Explicit.Remove(ExplicitOrder.Dequeue());
        }
    }
    /// <summary>The secrets registered through <c>TraceAIAuto.RegisterSecret</c>.</summary>
    internal static string[] Registered() { lock(State[0]) return Explicit.ToArray(); }
    private static void Add(string value)
    {
        if(value.Length<8) return;
        if(value.Length>MaxLength) { MarkFailed(); return; }
        var index=(Dictionary<string,LinkedListNode<string>>)State[1];
        var lru=(LinkedList<string>)State[2];
        if(index.TryGetValue(value,out var found)) { lru.Remove(found); lru.AddLast(found); return; }
        if(index.Count==Capacity) { index.Remove(lru.First!.Value); lru.RemoveFirst(); }
        index[value]=lru.AddLast(value); State[3]=Array.Empty<string>(); State[6]=true;
    }
    // The FI credentials of a configuration live outside the LRU, in a set that is not bounded: registering any number of other
    // secrets never drops them, and they never fall back to the LRU. Only a value over MaxLength fails the registry closed.
    private static void Pin(IEnumerable<string> values)
    {
        var pinned=(HashSet<string>)State[7];
        foreach(var value in values)
        {
            if(value.Length<8) continue;
            if(value.Length>MaxLength) { MarkFailed(); continue; }
            if(pinned.Contains(value)) continue;
            pinned.Add(value); State[3]=Array.Empty<string>(); State[6]=true;
        }
    }
    internal static void Register(IEnumerable<string> values)
    {
        try { lock(State[0]) foreach(var value in values) Add(value); }
        catch { FailClosed(); }
    }
    internal static void FailClosed()
    { lock(State[0]) MarkFailed(); }
    // Failing closed is process-wide, so say so once; never print the value.
    private static void MarkFailed()
    {
        if(State[4] is true) return;
        State[4]=true;
        TraceAIAuto.Warn("secrets-failed","Credential redaction is unavailable; exported spans keep identity and timing but no content for the rest of this process.");
    }
    // One lookup per export batch, and one for each wire span the agent ends (shared by building it and its application copy);
    // never per span start or attribute. Pinned secrets are always in it. Returned arrays
    // are immutable; mutation/eviction publishes a new snapshot on the next export.
    internal static string[] Snapshot()
    { lock(State[0]) return ReadSnapshot(); }
    internal static string[] ForExport(AutoConfig config)
    {
        lock(State[0])
        {
            // FI keys are mandatory at every export. They are pinned, so no amount of unrelated churn can evict them.
            Pin(config.CredentialValues);
            return ReadSnapshot();
        }
    }
    private static string[] ReadSnapshot()
    {
        if(State[4] is true) throw new InvalidDataException("Credential privacy unavailable");
        if(State[6] is true)
        {
            State[3]=((LinkedList<string>)State[2]).Concat((HashSet<string>)State[7]).Distinct(StringComparer.Ordinal).OrderByDescending(value=>value.Length).ToArray(); State[6]=false;
        }
        return (string[])State[3];
    }
}
