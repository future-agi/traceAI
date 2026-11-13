using System.Collections.Concurrent;
using System.Diagnostics;

namespace TraceAI.AutoInstrumentation;

/// <summary>Passive ancestry tracking, including explicit-parent native activities
/// whose Activity.Parent is null. The listener never requests sampling/export of
/// unrelated sources, and weak references cannot keep application activities alive.</summary>
internal sealed class ModelCoverage
{
    private readonly ConcurrentDictionary<(ActivityTraceId,ActivitySpanId),WeakReference<Activity>> _active=new();
    private Activity? Parent(Activity current)
    {
        if(current.Parent is {} parent) return parent;
        return current.ParentSpanId!=default && _active.TryGetValue((current.TraceId,current.ParentSpanId),out var reference) && reference.TryGetTarget(out var found)?found:null;
    }
    internal void Start(Activity activity)
    {
        // Bound retained context keys too, even for broken libraries that never Stop.
        if(_active.Count<65536) _active[(activity.TraceId,activity.SpanId)]=new(activity);
        if(Covered(activity)) activity.SetCustomProperty("traceai.model.covered",true);
    }
    internal void End(Activity activity)=>_active.TryRemove((activity.TraceId,activity.SpanId),out _);
    internal void Clear() => _active.Clear();
    internal bool Covered(Activity activity)
    {
        Activity? current=activity;
        for(int depth=0;current!=null && depth<256;depth++)
        {
            if(current.GetCustomProperty("traceai.model.covered") is true || current.GetTagItem("gen_ai.span.kind") is "LLM" or "EMBEDDING" or "RERANKER" or "GUARDRAIL") return true;
            current=Parent(current);
        }
        return false;
    }
}
