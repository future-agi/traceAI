using System.Reflection;

namespace TraceAI.AutoInstrumentation.Tests;

// Production has no reset/reconfiguration API. Unit tests simulate separate
// processes explicitly; real startup-hook tests always use fresh processes.
public abstract class InitIsolation : IDisposable
{
    protected InitIsolation() => Reset();
    public void Dispose() => Reset();
    internal static void ResetSecrets()
    {
        var state=(object[])typeof(KnownSecrets).GetField("State",BindingFlags.Static|BindingFlags.NonPublic)!.GetValue(null)!;
        lock(state[0])
        {
            ((Dictionary<string,LinkedListNode<string>>)state[1]).Clear(); ((LinkedList<string>)state[2]).Clear();
            state[3]=Array.Empty<string>(); state[4]=false; state[5]=false; state[6]=false;
        }
    }
    private static void Reset()
    {
        TraceAIAuto.Shutdown();
        ResetSecrets();
        const BindingFlags flags=BindingFlags.Static|BindingFlags.NonPublic;
        typeof(TraceAIAuto).GetField("s_initialized",flags)!.SetValue(null,false);
        AppDomain.CurrentDomain.SetData("TraceAI.AutoInstrumentation.InitState",null);
        AppDomain.CurrentDomain.SetData("TraceAI.AutoInstrumentation.InitState.RepeatLogged",null);
        typeof(TraceAIAuto).GetField("s_config",flags)!.SetValue(null,null);
        typeof(TraceAIAuto).GetField("s_failed",flags)!.SetValue(null,false);
    }
}
