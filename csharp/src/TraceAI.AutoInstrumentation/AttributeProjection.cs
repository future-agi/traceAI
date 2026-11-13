using System.Diagnostics;
using System.Reflection;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using TraceAI.AutoInstrumentation.Wire;

namespace TraceAI.AutoInstrumentation;

/// <summary>Project sanitized candidates before the destination's bounded container.
/// Activity tags themselves are unbounded on DS8–10. Never mutate a host Activity.</summary>
internal static class AttributeProjection
{
    internal const string DroppedContent="traceai.capture.dropped_content_count";
    internal static int Limit(BaseExporter<Activity>? exporter=null)
    {
        // OTel .NET owns limits at the exporter, not the unbounded Activity. Honor
        // its resolved options (including DI configuration), not just today's env.
        if(exporter is OtlpTraceExporter)
        {
            var options=typeof(OtlpTraceExporter).GetField("sdkLimitOptions",BindingFlags.Instance|BindingFlags.NonPublic)?.GetValue(exporter);
            if(options?.GetType().GetProperty("SpanAttributeCountLimit")?.GetValue(options) is int configured && configured>=0) return configured;
        }
        int? Read(string name)=>int.TryParse(Environment.GetEnvironmentVariable(name),out var value)&&value>=0?value:null;
        return Read("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT")??Read("OTEL_ATTRIBUTE_COUNT_LIMIT")??128;
    }
    internal static IOrderedEnumerable<KeyValuePair<string,object?>> Order(IEnumerable<KeyValuePair<string,object?>> tags)=>
        tags.OrderBy(pair=>Family(pair.Key)).ThenBy(pair=>pair.Key,StringComparer.Ordinal);

    private static int Family(string key)=>key switch {
        DroppedContent or "traceai.wire.incomplete" or "traceai.wire.truncated" or "traceai.wire.undecoded_encoding" or "traceai.wire.body_unavailable"=>0,
        "gen_ai.span.kind" or "gen_ai.response.id" or "project_name" or "project_type" or "project_version_name" or "project_version_id"=>1,
        "error.type" or "http.response.status_code" or "gen_ai.response.finish_reasons"=>2,
        "gen_ai.operation.name" or "gen_ai.provider.name" or "gen_ai.request.model" or "gen_ai.response.model"=>3,
        _ when key.StartsWith("gen_ai.usage.",StringComparison.Ordinal)=>4,
        "traceai.capture.layer" or "traceai.wire.endpoint" or "server.address" or "server.port" or "http.request.method"=>5,
        _=>6
    };
    internal static bool Protected(string key)=>Family(key)<6;
    internal static bool ContentKey(string key)=>key.StartsWith("input.",StringComparison.Ordinal)||key.StartsWith("output.",StringComparison.Ordinal)||
        key.StartsWith("gen_ai.input.",StringComparison.Ordinal)||key.StartsWith("gen_ai.output.",StringComparison.Ordinal)||
        key.StartsWith("gen_ai.tool.",StringComparison.Ordinal)||key.StartsWith("gen_ai.system_instructions",StringComparison.Ordinal)||
        key.StartsWith("gen_ai.request.parameters",StringComparison.Ordinal)||key=="gen_ai.audio.transcript";
    private sealed class Group(string name,int priority,long index=0)
    {
        internal readonly string Name=name;
        internal int Priority=priority;
        internal long Index=index;
        internal readonly List<KeyValuePair<string,object?>> Tags=[];
        internal int ContentCount;
    }
    private static (string Name,int Priority,long Index) GroupKey(string key,string? operation)
    {
        foreach(var (prefix,priority) in new[] { ("gen_ai.output.messages.",0),("output.messages.",0),("gen_ai.input.messages.",2),("input.messages.",2) })
            if(key.StartsWith(prefix,StringComparison.Ordinal))
            {
                var index=key[prefix.Length..].Split('.')[0];
                if(long.TryParse(index,out long number)&&number>=0) return (prefix+index,priority,number);
            }
        if(key.StartsWith("output.",StringComparison.Ordinal)) return (key is "output.value" or "output.mime_type"?"output.value":key,1,0);
        if(key.StartsWith("gen_ai.output.",StringComparison.Ordinal)||key.StartsWith("gen_ai.tool.call.result",StringComparison.Ordinal)||key.StartsWith("gen_ai.tool.output",StringComparison.Ordinal)) return (key,0,0);
        if(key=="gen_ai.audio.transcript") return (key,operation=="text_to_speech"?3:1,0);
        if(key is "input.value" or "input.mime_type") return ("input.value",4,0);
        if(key.StartsWith("gen_ai.tool.definitions.",StringComparison.Ordinal))
        {
            var index=key["gen_ai.tool.definitions.".Length..].Split('.')[0];
            return ("gen_ai.tool.definitions."+index,3,long.TryParse(index,out var number)?number:0);
        }
        if(key.StartsWith("gen_ai.request.parameters",StringComparison.Ordinal)) return ("gen_ai.request.parameters",3,0);
        return (key,ContentKey(key)?3:5,0);
    }
    private static string PathKey(string key)=>string.Join('.',key.Split(new[] { '.','[',']' },StringSplitOptions.RemoveEmptyEntries));
    private static bool Within(string key,string parent)=>parent.Length==0||key==parent||key.StartsWith(parent+".",StringComparison.Ordinal);
    internal static KeyValuePair<string,object?>[] Project(IEnumerable<KeyValuePair<string,object?>> candidates,int limit)
    {
        limit=Math.Max(0,limit);
        var tags=candidates.Where(pair=>pair.Value!=null).ToDictionary(pair=>pair.Key,pair=>pair.Value,StringComparer.Ordinal);
        long prior=tags.GetValueOrDefault(DroppedContent) switch { long n when n>0=>n,int n when n>0=>n,_=>0 };
        tags.Remove(DroppedContent);
        string? operation=tags.GetValueOrDefault("gen_ai.operation.name") as string;
        var protectedTags=Order(tags.Where(pair=>Family(pair.Key)<6)).ToList();
        var groups=new Dictionary<string,Group>(StringComparer.Ordinal);
        foreach(var tag in tags.Where(pair=>Family(pair.Key)==6).OrderBy(pair=>pair.Key,StringComparer.Ordinal))
        {
            var key=GroupKey(tag.Key,operation);
            if(!groups.TryGetValue(key.Name,out var group)) groups[key.Name]=group=new(key.Name,key.Priority,key.Index);
            group.Tags.Add(tag); if(ContentKey(tag.Key)) group.ContentCount++;
        }
        // MIME/type hints and every field they describe stay atomic, even for
        // flattened/indexed native attributes outside the standard message keys.
        // Merge existing message groups rather than splitting their role/tool fields.
        var hints=PrivacyCatalog.IndexHints(tags);
        var contextual=tags.Where(tag=>PrivacyCatalog.Context(tag.Key,hints).Payload||PrivacyCatalog.IndexHints(new[] { tag }).Count>0)
            .Select(tag=>tag.Key).ToHashSet(StringComparer.Ordinal);
        foreach(var parent in hints.Keys.OrderBy(path=>path.Length).ThenBy(path=>path,StringComparer.Ordinal))
        {
            var related=groups.Values.Where(group=>group.Tags.Any(tag=>contextual.Contains(tag.Key)&&Within(PathKey(tag.Key),parent))).ToArray();
            if(related.Length==0) continue;
            var merged=new Group("mime:"+parent,related.Min(group=>group.Priority),related[0].Index);
            foreach(var group in related) { merged.Tags.AddRange(group.Tags); groups.Remove(group.Name); }
            merged.ContentCount=merged.Tags.Count(tag=>ContentKey(tag.Key)||contextual.Contains(tag.Key));
            if(merged.Priority==5) merged.Priority=3;
            groups[merged.Name]=merged;
        }
        var ordered=groups.Values.OrderBy(group=>group.Priority).ThenBy(group=>group.Priority==2?-group.Index:group.Index).ThenBy(group=>group.Name,StringComparer.Ordinal).ToArray();
        int contentCount=ordered.Sum(group=>group.ContentCount);
        List<KeyValuePair<string,object?>> Select(bool marker)
        {
            int available=Math.Max(0,limit-protectedTags.Count-(marker?1:0));
            var selected=new List<KeyValuePair<string,object?>>();
            bool outputOmitted=false,inputGap=false;
            foreach(var group in ordered)
            {
                bool blocked=group.Priority is >=2 and <=4&&outputOmitted || group.Priority==2&&inputGap || group.Priority==4&&inputGap;
                if(blocked||group.Tags.Count>available)
                {
                    if(group.Priority<=1) outputOmitted=true;
                    if(group.Priority==2) inputGap=true;
                    continue;
                }
                available-=group.Tags.Count;
                selected.AddRange(group.Tags.OrderBy(tag=>tag.Key,StringComparer.Ordinal));
            }
            return selected;
        }
        bool marker=prior>0;
        var selected=Select(marker);
        // Count content keys, not groups, tokens, bytes or unrelated SDK drops.
        int SelectedContent(List<KeyValuePair<string,object?>> chosen)
        {
            var keys=chosen.Select(pair=>pair.Key).ToHashSet(StringComparer.Ordinal);
            return ordered.Where(group=>group.Tags.All(tag=>keys.Contains(tag.Key))).Sum(group=>group.ContentCount);
        }
        int omitted=contentCount-SelectedContent(selected);
        if(omitted>0&&!marker) { marker=true; selected=Select(true); omitted=contentCount-SelectedContent(selected); }
        if(marker)
        {
            long total=prior>long.MaxValue-omitted?long.MaxValue:prior+omitted;
            protectedTags.Add(new(DroppedContent,total));
        }
        if(protectedTags.Count>limit)
        {
            // Select() admitted no content when protected fields did not fit.
            if(limit==0) TraceAIAuto.Warn("attribute-limit-zero","Span attribute limit is zero; semantic and degradation attributes cannot be retained.");
            return Order(protectedTags).Take(limit).ToArray();
        }
        return Order(protectedTags).Concat(selected).ToArray();
    }
}
