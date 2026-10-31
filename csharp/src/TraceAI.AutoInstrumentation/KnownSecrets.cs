using System.Collections;

namespace TraceAI.AutoInstrumentation;

/// <summary>BCL-only process state: shared by the private hook and default load contexts.
/// No activity references, per-trace groups or diagnostic representation of values.</summary>
internal static class KnownSecrets
{
    private const string StateKey="TraceAI.AutoInstrumentation.KnownSecrets.v1";
    internal const int Capacity=1024;
    internal const int MaxLength=4096;
    // gate, LRU index/list, immutable sorted snapshot, failed, environment seeded, dirty.
    private static readonly object[] State=ProcessState();
    private static object[] ProcessState()
    {
        lock(AppDomain.CurrentDomain)
        {
            if(AppDomain.CurrentDomain.GetData(StateKey) is object[] existing) return existing;
            object[] created=[new object(),new Dictionary<string,LinkedListNode<string>>(StringComparer.Ordinal),new LinkedList<string>(),Array.Empty<string>(),false,false,false];
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
                            new[] { "KEY","TOKEN","SECRET","PASSWORD","PASSWD","CREDENTIAL","AUTH" }.Any(part=>name.Contains(part,StringComparison.OrdinalIgnoreCase))) Add(value);
                }
                foreach(var value in config.CredentialValues) Add(value);
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
    private static void Add(string value)
    {
        if(value.Length<8) return;
        if(value.Length>MaxLength) { State[4]=true; return; }
        var index=(Dictionary<string,LinkedListNode<string>>)State[1];
        var lru=(LinkedList<string>)State[2];
        if(index.TryGetValue(value,out var found)) { lru.Remove(found); lru.AddLast(found); return; }
        if(index.Count==Capacity) { index.Remove(lru.First!.Value); lru.RemoveFirst(); }
        index[value]=lru.AddLast(value); State[3]=Array.Empty<string>(); State[6]=true;
    }
    internal static void Register(IEnumerable<string> values)
    {
        try { lock(State[0]) foreach(var value in values) Add(value); }
        catch { FailClosed(); }
    }
    internal static void FailClosed()
    { lock(State[0]) State[4]=true; }
    // One lookup per export batch, never per span start or attribute. Returned arrays
    // are immutable; mutation/eviction publishes a new snapshot on the next export.
    internal static string[] Snapshot()
    { lock(State[0]) return ReadSnapshot(); }
    internal static string[] ForExport(AutoConfig config)
    {
        lock(State[0])
        {
            // FI keys are mandatory at every export, even after unrelated LRU churn.
            // Refresh them in the same bounded registry/lookup, not a second secret set.
            foreach(var value in config.CredentialValues) Add(value);
            return ReadSnapshot();
        }
    }
    private static string[] ReadSnapshot()
    {
        if(State[4] is true) throw new InvalidDataException("Credential privacy unavailable");
        if(State[6] is true)
        {
            State[3]=((LinkedList<string>)State[2]).OrderByDescending(value=>value.Length).ToArray(); State[6]=false;
        }
        return (string[])State[3];
    }
}
