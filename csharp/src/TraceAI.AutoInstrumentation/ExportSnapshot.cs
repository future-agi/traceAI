using System.Diagnostics;
using System.Reflection;
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
internal sealed class ExportSnapshot(Activity[] spans,Resource resource)
{
    internal Activity[] Spans { get; }=spans;
    internal Resource Resource { get; }=resource;
    private const BindingFlags Fields=BindingFlags.Instance|BindingFlags.NonPublic;
    private static readonly MethodInfo Memberwise=typeof(object).GetMethod("MemberwiseClone",Fields)!;
    internal static object Clone(object value)=>Memberwise.Invoke(value,null)!;
    private static void Set(object value,string field,object? replacement) =>
        (value.GetType().GetField(field,Fields)??throw new MissingFieldException(value.GetType().Name,field)).SetValue(value,replacement);
    private static Activity Shell(Activity original)
    {
        // Materialize lazy identity before detaching the parent reference.
        _=original.TraceId; _=original.SpanId; _=original.ParentSpanId;
        var parentId=original.ParentId;
        var copy=(Activity)Clone(original);
        foreach(var field in new[] { "_tags","_events","_links","_baggage","_customProperties","<Parent>k__BackingField" }) Set(copy,field,null);
        Set(copy,"_parentId",parentId);
        // This reference is not exported, and must not retain an application's activity graph.
        typeof(Activity).GetField("_previousActiveActivity",Fields)?.SetValue(copy,null);
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
            var value=pair.Value==null?null:safe(pair.Key,WirePrivacy.Scrub(pair.Value,path:pair.Key,hints:hints),operation);
            budget.Value(value);
            if(value is string text) value=redact(text);
            result[key]=value;
        }
        return result;
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
        Func<string,object,string?,object?> safe,Func<string,string> redact,Func<string,string,string>? eventKey=null,int? attributeLimit=null)
    {
        var spans=new List<Activity>();
        var shared=new Budget(65536,8*1024*1024);
        foreach(var original in batch)
        {
            if(original.GetCustomProperty("traceai.suppressed") is true) continue;
            Activity copy;
            try { copy=Shell(original); }
            catch { TraceAIAuto.Warn("snapshot","Unsupported DiagnosticSource layout; telemetry omitted."); continue; }
            try
            {
                var budget=new Budget(shared:shared);
                var operation=original.GetTagItem("gen_ai.operation.name") as string;
                Set(copy,"<Source>k__BackingField",Scope(original.Source,budget,safe,redact));
                Set(copy,"_traceState",original.TraceStateString==null?null:budget.Text(original.TraceStateString,redact));
                copy.DisplayName=budget.Text(original.DisplayName,redact);
                Set(copy,"<OperationName>k__BackingField",budget.Text(original.OperationName,redact));
                copy.SetStatus(original.Status==ActivityStatusCode.Unset?ActivityStatusCode.Ok:original.Status,
                    original.StatusDescription==null?null:budget.Text(original.StatusDescription,redact));
                foreach(var tag in Tags(original.TagObjects,budget,safe,redact,operation)) copy.SetTag(tag.Key,tag.Value);
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
                    var field=typeof(Activity).GetField("_links",Fields)!;
                    var list=Activator.CreateInstance(field.FieldType,nonPublic:true)!;
                    var add=field.FieldType.GetMethod("Add",Fields|BindingFlags.Public)!;
                    foreach(var link in links) add.Invoke(list,new object[] { link });
                    field.SetValue(copy,list);
                }
                AutoProcessor.Map(copy);
                // Mapping appends derived identity/usage. Sanitize before selecting
                // atomic groups so dropping MIME hints cannot expose their payload.
                // The projection's content count is separate from SDK eviction counts.
                var projected=AttributeProjection.Project(copy.TagObjects,attributeLimit??AttributeProjection.Limit());
                Set(copy,"_tags",null);
                foreach(var tag in projected) copy.SetTag(tag.Key,tag.Value);
            }
            catch
            {
                // Keep identity/timing on any privacy failure, but discard ALL content.
                copy=Shell(original);
                var source=(ActivitySource)Clone(original.Source);
                Set(source,"<Name>k__BackingField","traceai.redacted"); Set(source,"<Version>k__BackingField",null);
                typeof(ActivitySource).GetField("<Tags>k__BackingField",Fields)?.SetValue(source,null);
                typeof(ActivitySource).GetField("<TelemetrySchemaUrl>k__BackingField",Fields)?.SetValue(source,null);
                Set(copy,"<Source>k__BackingField",source); Set(copy,"_traceState",null);
                string Text(string text) { try { return redact(text); } catch { return "traceai.redacted"; } }
                copy.DisplayName=Text(original.DisplayName); Set(copy,"<OperationName>k__BackingField",Text(original.OperationName)); copy.SetStatus(original.Status);
                var retained=new ActivityTagsCollection();
                var operation=original.GetTagItem("gen_ai.operation.name") as string;
                var originalTags=original.TagObjects.Take(PrivacyCatalog.MaxNodes+1).ToArray();
                Dictionary<string,PrivacyCatalog.Hint>? hints;
                try { hints=originalTags.Length<=PrivacyCatalog.MaxNodes?PrivacyCatalog.IndexHints(originalTags):null; } catch { hints=null; }
                foreach(var tag in originalTags.Where(tag=>AttributeProjection.Protected(tag.Key)))
                {
                    string key;
                    try { key=redact(tag.Key); }
                    catch { if(!FixedSemanticKey(tag.Key)) continue; key=tag.Key; }
                    object? value=tag.Value==null?null:hints==null?WirePrivacy.Redacted:WirePrivacy.Scrub(tag.Value,path:tag.Key,hints:hints);
                    try
                    {
                        value=value==null?null:safe(tag.Key,value,operation);
                        if(value is string text) value=redact(text);
                    }
                    catch { value=ClosedValue(value); }
                    retained[key]=value;
                }
                foreach(var tag in AttributeProjection.Project(retained,attributeLimit??AttributeProjection.Limit())) copy.SetTag(tag.Key,tag.Value);
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
