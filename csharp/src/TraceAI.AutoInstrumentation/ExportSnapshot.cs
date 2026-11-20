using System.Diagnostics;
using System.Reflection;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Resources;
using TraceAI.AutoInstrumentation.Wire;

namespace TraceAI.AutoInstrumentation;

/// <summary>
/// The sole authoritative export boundary. Detached framework shells retain IDs,
/// flags and timestamps, but no mutable content, parent or application objects.
/// Cloning avoids ActivitySource constructors/listeners and Activity.Start/Stop:
/// those APIs would invoke application callbacks on the exporter thread.
/// Field access is deliberately limited to the supported DiagnosticSource 8–10
/// layout. A missing field fails closed, never falls back to the shared activity.
/// </summary>
/// <summary>What the agent put on a span it created, kept in an agent-side table keyed by the Activity (never on the Activity itself): the unreplaced name,
/// status description and attributes, beside what the application's copy was given. The Future AGI copy is replaced at export
/// from the unreplaced values, except for anything a later processor changed on the shared Activity: that is exported from the
/// Activity as it is, and sanitized like any other host value.</summary>
internal sealed class WireRecord(string name,string? description,bool capDescription,KeyValuePair<string,object?>[] unreplaced,IEnumerable<KeyValuePair<string,object?>> applied,string appliedName,string? appliedDescription)
{
    // Keyed by the Activity, not stored on it: neither a custom property, a tag nor any other member the application can read.
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Activity,WireRecord> Records=new();
    internal static void Keep(Activity activity,WireRecord record)=>Records.AddOrUpdate(activity,record);
    internal static WireRecord? For(Activity activity)=>Records.TryGetValue(activity,out var record)?record:null;
    /// <summary>The description cap applies to the replaced text of a non-JSON response body, never to the unreplaced one.</summary>
    internal string Cap(Activity current,string replaced)=>capDescription&&current.StatusDescription==appliedDescription?WireCore.CapDescription(replaced):replaced;
    private readonly Dictionary<string,object?> _unreplaced=unreplaced.ToDictionary(pair=>pair.Key,pair=>pair.Value);
    private readonly Dictionary<string,object?> _applied=applied.ToDictionary(pair=>pair.Key,pair=>pair.Value);
    internal string Name(Activity current)=>current.DisplayName==appliedName?name:current.DisplayName;
    internal string? Description(Activity current)=>current.StatusDescription==appliedDescription?description:current.StatusDescription;
    /// <summary>The Activity's tags with the unreplaced value in place of each one the agent set and nobody changed.</summary>
    internal IEnumerable<KeyValuePair<string,object?>> Tags(Activity current)
    {
        foreach(var tag in current.TagObjects)
            yield return _applied.TryGetValue(tag.Key,out var applied)&&Same(applied,tag.Value)&&_unreplaced.TryGetValue(tag.Key,out var kept)?new(tag.Key,kept):tag;
    }
    private static bool Same(object? a,object? b)
    {
        if(Equals(a,b)) return true;
        return a is Array x&&b is Array y&&x.Length==y.Length&&x.Cast<object?>().Zip(y.Cast<object?>(),Equals).All(same=>same);
    }
}

internal sealed class ExportSnapshot(Activity[] spans,Resource resource)
{
    internal Activity[] Spans { get; }=spans;
    internal Resource Resource { get; }=resource;
    private const BindingFlags Fields=BindingFlags.Instance|BindingFlags.NonPublic;
    internal static object Clone(object value)=>(RuntimeLayout.CloneMethod??throw new MissingMethodException(nameof(Object),"MemberwiseClone")).Invoke(value,null)!;
    // Members are resolved once (RuntimeLayout); a missing one fails this span closed.
    private static void Set(object value,string field,object? replacement)
    {
        var type=value is Activity?typeof(Activity):value.GetType();
        (RuntimeLayout.Field(type,field)??throw new MissingFieldException(type.Name,field)).SetValue(value,replacement);
    }
    private static Activity Shell(Activity original)
    {
        // Materialize lazy identity before detaching the parent reference.
        _=original.TraceId; _=original.SpanId; _=original.ParentSpanId;
        var parentId=original.ParentId;
        var copy=(Activity)Clone(original);
        foreach(var field in new[] { "_tags","_events","_links","_baggage","_customProperties","<Parent>k__BackingField" }) Set(copy,field,null);
        Set(copy,"_parentId",parentId);
        // This reference is not exported, and must not retain an application's activity graph.
        RuntimeLayout.Field(typeof(Activity),"_previousActiveActivity")?.SetValue(copy,null);
        return copy;
    }
    private sealed class Budget(int nodes=WirePrivacy.MaxAttributeNodes,int characters=8*1024*1024,Budget? shared=null)
    {
        private int _nodes=nodes;
        private int _characters=characters;
        internal void Node() { if(--_nodes<0) throw new InvalidDataException("Export node budget"); shared?.Node(); }
        private void ChargeText(int count)
        {
            if((_characters-=count)<0) throw new InvalidDataException("Export text budget");
            shared?.ChargeText(count);
        }
        internal string Text(string value,Func<string,string> redact)
        {
            Node(); ChargeText(value.Length); return redact(value);
        }
        internal void Value(object? value,int depth=0)
        {
            Node(); if(depth>64) throw new InvalidDataException("Export value depth");
            if(value is string text) ChargeText(text.Length);
            else if(value is Array array) foreach(var item in array) Value(item,depth+1);
        }
    }
    private static ActivityTagsCollection Tags(IEnumerable<KeyValuePair<string,object?>> tags,Budget budget,
        Func<string,object,string?,object?> safe,Func<string,string> redact,string? operation)
    {
        var result=new ActivityTagsCollection();
        // Index the original bounded, framework-owned collection before touching values:
        // hints may follow payloads and may be indexed arrays. Never enumerate unknown
        // application values while discovering hints.
        var materialized=new List<KeyValuePair<string,object?>>();
        foreach(var pair in tags)
        {
            budget.Text(pair.Key,static key=>key);
            if(pair.Value is string text) budget.Value(text);
            materialized.Add(pair);
        }
        var hints=PrivacyCatalog.IndexHints(materialized);
        foreach(var pair in AttributeProjection.Order(materialized))
        {
            var key=redact(pair.Key);
            var value=pair.Value==null?null:safe(pair.Key,WirePrivacy.Scrub(pair.Value,path:AutoProcessor.ScrubPath(pair.Key),hints:hints,shapes:false),operation);
            budget.Value(value);
            if(value is string text) value=redact(text);
            result[key]=value;
        }
        return result;
    }
    // The FI context captured when the activity started joins the copy's candidates,
    // so it passes the same privacy rules. The application's own tags win on a clash.
    private static IEnumerable<KeyValuePair<string,object?>> WithContext(IEnumerable<KeyValuePair<string,object?>> tags,KeyValuePair<string,string>[]? context)
    {
        if(context==null||context.Length==0) return tags;
        var merged=tags.ToList();
        foreach(var pair in context)
            if(!merged.Exists(tag=>tag.Key==pair.Key)) merged.Add(new(pair.Key,pair.Value));
        return merged;
    }
    private static ActivitySource Scope(ActivitySource original,Budget budget,Func<string,object,string?,object?> safe,Func<string,string> redact)
    {
        var scope=(ActivitySource)Clone(original);
        Set(scope,"<Name>k__BackingField",budget.Text(original.Name,redact));
        Set(scope,"<Version>k__BackingField",original.Version==null?null:budget.Text(original.Version,redact));
        var tags=typeof(ActivitySource).GetProperty("Tags");
        if(tags!=null)
        {
            var values=tags.GetValue(original) as IEnumerable<KeyValuePair<string,object?>>;
            // Framework materialized collections only; never enumerate application iterators.
            bool trusted=values==null || values.GetType().Assembly==typeof(object).Assembly || values.GetType().Assembly==typeof(ActivitySource).Assembly;
            Set(scope,"<Tags>k__BackingField",trusted && values!=null?Tags(values,budget,safe,redact,null).ToArray():null);
        }
        var schema=typeof(ActivitySource).GetProperty("TelemetrySchemaUrl");
        if(schema!=null) Set(scope,"<TelemetrySchemaUrl>k__BackingField",schema.GetValue(original) is string url?budget.Text(url,redact):null);
        // Never dispose this shell: it does not own the source's listener registrations.
        return scope;
    }
    internal static ExportSnapshot Create(IEnumerable<Activity> batch,Resource resource,
        Func<string,object,string?,object?> safe,Func<string,string> redact,Func<string,string,string>? eventKey=null,int? attributeLimit=null,
        bool hideErrorDetails=false,Func<Activity,bool>? sampled=null,Func<Activity,Activity,bool>? samplingMetadata=null)
    {
        void Project(Activity original,Activity copy)
        {
            // Accepted spans are record-only on the shared Activity. Stamp only the
            // detached copy, after sanitization, on both normal and fallback paths.
            if(sampled!=null) copy.ActivityTraceFlags|=ActivityTraceFlags.Recorded;
            bool trustedRatio=sampled!=null && samplingMetadata?.Invoke(original,copy)==true;
            var ratio=trustedRatio?copy.GetTagItem(RatioSampler.Attribute):null;
            int limit=Math.Max(0,attributeLimit??AttributeProjection.Limit());
            // Reserve a slot before atomic content selection, not after it. Application
            // tags cannot claim this reservation, and a zero limit still means no tags.
            var candidates=trustedRatio?copy.TagObjects.Where(tag=>tag.Key!=RatioSampler.Attribute):copy.TagObjects;
            var projected=AttributeProjection.Project(candidates,limit,reserved:trustedRatio&&limit>0?1:0);
            Set(copy,"_tags",null);
            foreach(var tag in projected) copy.SetTag(tag.Key,tag.Value);
            if(trustedRatio&&limit>0) copy.SetTag(RatioSampler.Attribute,ratio);
        }
        var spans=new List<Activity>();
        var shared=new Budget(65536,8*1024*1024);
        foreach(var original in batch)
        {
            if(original.GetCustomProperty("traceai.suppressed") is true) continue;
            // A shared Activity may have been recorded by a different provider. Never
            // export or promote it if our own head sampler rejected it.
            if(sampled!=null && !sampled(original)) continue;
            Activity copy;
            try { copy=Shell(original); }
            catch { TraceAIAuto.Warn("snapshot","Unsupported DiagnosticSource layout; telemetry omitted."); continue; }
            try
            {
                var budget=new Budget(shared:shared);
                var unreplaced=WireRecord.For(original);
                var operation=original.GetTagItem("gen_ai.operation.name") as string;
                Set(copy,"<Source>k__BackingField",Scope(original.Source,budget,safe,redact));
                Set(copy,"_traceState",original.TraceStateString==null?null:budget.Text(original.TraceStateString,redact));
                copy.DisplayName=budget.Text(unreplaced?.Name(original)??original.DisplayName,redact);
                Set(copy,"<OperationName>k__BackingField",budget.Text(original.OperationName,redact));
                // Hidden error details keep the status code but never its description.
                var statusText=unreplaced==null?original.StatusDescription:unreplaced.Description(original);
                copy.SetStatus(original.Status==ActivityStatusCode.Unset?ActivityStatusCode.Ok:original.Status,
                    hideErrorDetails||statusText==null?null:unreplaced==null?budget.Text(statusText,redact):unreplaced.Cap(original,budget.Text(statusText,redact)));
                var startContext=original.GetCustomProperty(AutoProcessor.ContextProperty) as KeyValuePair<string,string>[];
                foreach(var tag in Tags(WithContext(unreplaced?.Tags(original)??original.TagObjects,startContext),budget,safe,redact,operation)) copy.SetTag(tag.Key,tag.Value);
                foreach(var item in original.Events)
                {
                    budget.Node();
                    copy.AddEvent(new ActivityEvent(budget.Text(item.Name,redact),item.Timestamp,Tags(item.Tags,budget,
                        (key,value,op)=>safe(eventKey?.Invoke(item.Name,key)??key,value,op),redact,operation)));
                }
                var links=new List<ActivityLink>();
                foreach(var item in original.Links)
                {
                    budget.Node(); var c=item.Context;
                    var context=new ActivityContext(c.TraceId,c.SpanId,c.TraceFlags,c.TraceState==null?null:budget.Text(c.TraceState,redact),c.IsRemote);
                    links.Add(new ActivityLink(context,Tags(item.Tags??[],budget,safe,redact,operation)));
                }
                if(links.Count>0)
                {
                    // AddLink is absent on DS8. Populate its stable BCL list
                    // directly instead; no application listener or iterator runs.
                    var field=RuntimeLayout.Field(typeof(Activity),"_links")??throw new MissingFieldException(nameof(Activity),"_links");
                    var list=Activator.CreateInstance(field.FieldType,nonPublic:true)!;
                    var add=field.FieldType.GetMethod("Add",Fields|BindingFlags.Public)!;
                    foreach(var link in links) add.Invoke(list,new object[] { link });
                    field.SetValue(copy,list);
                }
                AutoProcessor.Map(copy);
                // Mapping appends derived identity/usage. Sanitize before selecting
                // atomic groups so dropping MIME hints cannot expose their payload.
                // The projection's content count is separate from SDK eviction counts.
                Project(original,copy);
            }
            catch
            {
                // Keep identity/timing on any privacy failure, but discard ALL content.
                copy=Shell(original);
                var source=(ActivitySource)Clone(original.Source);
                Set(source,"<Name>k__BackingField","traceai.redacted"); Set(source,"<Version>k__BackingField",null);
                RuntimeLayout.Field(typeof(ActivitySource),"<Tags>k__BackingField")?.SetValue(source,null);
                RuntimeLayout.Field(typeof(ActivitySource),"<TelemetrySchemaUrl>k__BackingField")?.SetValue(source,null);
                Set(copy,"<Source>k__BackingField",source); Set(copy,"_traceState",null);
                string Text(string text) { try { return redact(text); } catch { return "traceai.redacted"; } }
                copy.DisplayName=Text(WireRecord.For(original) is {} named?named.Name(original):original.DisplayName); Set(copy,"<OperationName>k__BackingField",Text(original.OperationName)); copy.SetStatus(original.Status);
                var retained=new ActivityTagsCollection();
                var operation=original.GetTagItem("gen_ai.operation.name") as string;
                var originalTags=(WireRecord.For(original) is {} kept?kept.Tags(original):original.TagObjects).Take(PrivacyCatalog.MaxNodes+1).ToArray();
                Dictionary<string,PrivacyCatalog.Hint>? hints;
                try { hints=originalTags.Length<=PrivacyCatalog.MaxNodes?PrivacyCatalog.IndexHints(originalTags):null; } catch { hints=null; }
                foreach(var tag in originalTags.Where(tag=>AttributeProjection.Protected(tag.Key)))
                {
                    string key;
                    try { key=redact(tag.Key); }
                    catch { if(!FixedSemanticKey(tag.Key)) continue; key=tag.Key; }
                    object? value=tag.Value==null?null:hints==null?WirePrivacy.Redacted:WirePrivacy.Scrub(tag.Value,path:tag.Key,hints:hints,shapes:false);
                    try
                    {
                        value=value==null?null:safe(tag.Key,value,operation);
                        if(value is string text) value=redact(text);
                    }
                    catch { value=ClosedValue(value); }
                    retained[key]=value;
                }
                foreach(var tag in retained) copy.SetTag(tag.Key,tag.Value);
                Project(original,copy);
            }
            spans.Add(copy);
        }
        return new(spans.ToArray(),SanitizeResource(resource,(key,value)=>safe(key,value,null),redact));
    }
    private static object? ClosedValue(object? value)=>value switch {
        null=>null, bool or byte or sbyte or short or ushort or int or uint or long or ulong or decimal=>value,
        double n when double.IsFinite(n)=>n, float n when float.IsFinite(n)=>n, _=>WirePrivacy.Redacted
    };
    private static bool FixedSemanticKey(string key)=>AttributeProjection.Protected(key) &&
        (!key.StartsWith("gen_ai.usage.",StringComparison.Ordinal) || key is "gen_ai.usage.input_tokens" or "gen_ai.usage.output_tokens" or
        "gen_ai.usage.total_tokens" or "gen_ai.usage.cache_read_tokens" or "gen_ai.usage.cache_write_tokens" or "gen_ai.usage.provider_total_tokens" or
        "gen_ai.usage.output_tokens.reasoning" or "gen_ai.usage.input_tokens.audio" or "gen_ai.usage.output_tokens.audio");
    private static bool ResourceIdentity(string key)=>key is "service.name" or "service.namespace" or "service.version" or "service.instance.id" or
        "project_name" or "project_type" or "project_version_name" or "project_version_id";
    internal static Resource SanitizeResource(Resource resource,Func<string,object,object?> safe,Func<string,string> redact)
    {
        try
        {
            var budget=new Budget(characters:4*1024*1024);
            var attributes=Tags(resource.Attributes.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)),budget,(key,value,_)=>safe(key,value),redact,null);
            return new Resource(attributes.Where(p=>p.Value!=null).Select(p=>new KeyValuePair<string,object>(p.Key,p.Value!)));
        }
        catch
        {
            // One bad resource field must not erase service identity or FI routing.
            // Retry only the fixed identity set, each independently bounded. If the
            // registry itself is unavailable, keep its keys with closed values.
            var retained=new List<KeyValuePair<string,object>>();
            foreach(var tag in resource.Attributes.Take(PrivacyCatalog.MaxNodes).Where(tag=>ResourceIdentity(tag.Key)))
            {
                try
                {
                    foreach(var pair in Tags([new(tag.Key,tag.Value)],new Budget(characters:4*1024*1024),
                        (key,value,_)=>safe(key,value),redact,null))
                        if(pair.Value!=null) retained.Add(new(pair.Key,pair.Value));
                }
                catch { if(ClosedValue(tag.Value) is {} value) retained.Add(new(tag.Key,value)); }
            }
            return new Resource(retained);
        }
    }
}
