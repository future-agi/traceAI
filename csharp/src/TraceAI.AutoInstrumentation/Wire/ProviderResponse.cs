using System.Text.Json.Nodes;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Explicit parsed provider-response entry point for the catalog's closed
/// <c>logprob_tokens</c> list. Only a caller that parsed the provider's own response body,
/// or holds the complete buffered chunk array of one stream, may use it. Inside that root,
/// logprob tokens are checked against registered secrets and credential value shapes
/// instead of the key-name rule. Generic attributes, tool results, user content, nested JSON
/// strings and flattened keys never get this context.
///
/// The result starts from the generic <see cref="PrivacyCatalog.Scrub"/> output. A token is
/// only restored where its object and every ancestor is found in that output unchanged in
/// shape and carries no media/type hint; anything else keeps the generic redaction. When
/// value masking changes a choice's text, its whole logprobs block is masked, and for a
/// stream the deltas are also checked joined by choice index. Like the generic scrubber it
/// throws when its budget is exhausted; callers fail closed.</summary>
internal static class ProviderResponse
{
    private const long MaxSafeInteger=9007199254740991;
    private sealed class Slot
    {
        internal readonly List<(object[] Choice,List<object[]> Fields)> Choices=[];
        internal readonly Dictionary<string,string> Joined=new(StringComparer.Ordinal);
        internal bool Masked;
    }
    internal static JsonNode? Scrub(JsonNode? response,IEnumerable<string>? secrets=null,string format="openai.chat")
    {
        string[] known=secrets?.ToArray()??[];
        var clean=PrivacyCatalog.Scrub(response,"",null,known);
        if(response is not (JsonObject or JsonArray)||!PrivacyCatalog.LogprobTokens.TryGetValue(format,out var rule)) return clean;
        var budget=new PrivacyCatalog.Budget();
        bool streaming=response is JsonArray;
        var roots=new List<object[]>();
        if(response is JsonArray chunks)
        {
            if(chunks.Count>PrivacyCatalog.MaxNodes) throw new InvalidDataException("Privacy response budget");
            for(int i=0;i<chunks.Count;i++) roots.Add([i]);
        }
        else roots.Add([]);
        var slots=new Dictionary<long,Slot>();
        var unjoinable=new List<object[]>();
        foreach(var root in roots)
        {
            if(At(response,root) is not JsonObject chunk||chunk["choices"] is not JsonArray choices) continue;
            if(choices.Count>PrivacyCatalog.MaxNodes) throw new InvalidDataException("Privacy response budget");
            for(int i=0;i<choices.Count;i++)
            {
                if(choices[i] is not JsonObject choice) continue;
                object[] location=[..root,"choices",i];
                long? index=Index(choice["index"]);
                // A stream delta without a usable index cannot be joined to its other fragments.
                if(streaming&&index is not (>=0 and <=MaxSafeInteger)) { unjoinable.Add(location); continue; }
                long key=streaming?index!.Value:i;
                if(!slots.TryGetValue(key,out var slot)) slots[key]=slot=new();
                var fields=new List<object[]>();
                foreach(var textPath in rule.TextPaths)
                {
                    JsonNode? parent=choice;
                    foreach(string part in textPath[..^1]) parent=parent is JsonObject owner?owner[part]:null;
                    if(parent is not JsonObject container||container[textPath[^1]] is not {} field) continue;
                    fields.Add([..location,..textPath]);
                    if(WireCore.S(field) is not {} text) { slot.Masked=true; continue; }
                    if(Changed(text,known,budget)) slot.Masked=true;
                    if(streaming)
                    {
                        string name=string.Join('.',textPath),joined=slot.Joined.GetValueOrDefault(name,"")+text;
                        if(joined.Length>PrivacyCatalog.MaxText) throw new InvalidDataException("Privacy response text budget");
                        slot.Joined[name]=joined;
                    }
                }
                slot.Choices.Add((location,fields));
            }
        }
        // Secrets and credential shapes can cross delta boundaries.
        foreach(var slot in slots.Values) if(!slot.Masked&&slot.Joined.Values.Any(text=>Changed(text,known,budget))) slot.Masked=true;
        foreach(var root in roots)
        foreach(var path in rule.Paths)
        {
            var found=new List<object[]>();
            Tokens(At(response,root),path,0,new List<object>(root),found,0);
            foreach(var parent in found) Exempt(response,clean,parent,known,budget);
        }
        foreach(var choice in unjoinable) MaskLogprobs(clean,choice);
        foreach(var slot in slots.Values.Where(candidate=>candidate.Masked))
        foreach(var (choice,fields) in slot.Choices)
        {
            MaskLogprobs(clean,choice);
            // Streamed text of an affected choice is masked in every chunk, so it cannot
            // be rebuilt from the surviving fragments.
            if(streaming) foreach(var field in fields)
                if(At(clean,field[..^1]) is JsonObject owner&&owner.ContainsKey((string)field[^1])) owner[(string)field[^1]]=PrivacyCatalog.Redacted;
        }
        return clean;
    }
    private static bool Changed(string text,string[] secrets,PrivacyCatalog.Budget budget)=>
        text.Contains(PrivacyCatalog.Redacted,StringComparison.Ordinal)||text.Contains(FIInstrumentation.TraceConfig.RedactedValue,StringComparison.Ordinal)||
        PrivacyCatalog.String(text,"",null,secrets,true,budget)!=text;
    private static long? Index(JsonNode? node)
    {
        if(node is not JsonValue value) return null;
        if(value.TryGetValue<long>(out var integer)) return integer;
        if(value.TryGetValue<int>(out var small)) return small;
        if(value.TryGetValue<double>(out var number)&&double.IsFinite(number)&&Math.Floor(number)==number&&Math.Abs(number)<=MaxSafeInteger) return (long)number;
        return null;
    }
    private static JsonNode? At(JsonNode? node,IEnumerable<object> steps)
    {
        foreach(var step in steps)
            node=step switch {
                string key when node is JsonObject obj=>obj.TryGetPropertyValue(key,out var child)?child:null,
                int index when node is JsonArray array&&index<array.Count=>array[index],
                _=>null
            };
        return node;
    }
    // Wildcards require real arrays reached from the explicit root.
    private static void Tokens(JsonNode? node,string[] path,int at,List<object> location,List<object[]> found,int depth)
    {
        if(depth>PrivacyCatalog.MaxDepth) throw new InvalidDataException("Privacy response depth");
        if(at>=path.Length) return;
        if(path[at]=="*")
        {
            if(node is not JsonArray array) return;
            if(array.Count>PrivacyCatalog.MaxNodes) throw new InvalidDataException("Privacy response budget");
            for(int i=0;i<array.Count;i++)
            {
                location.Add(i); Tokens(array[i],path,at+1,location,found,depth+1); location.RemoveAt(location.Count-1);
            }
        }
        else if(node is JsonObject obj)
        {
            if(at==path.Length-1) { if(path[at]=="token"&&WireCore.S(obj["token"])!=null) found.Add(location.ToArray()); }
            else if(obj.TryGetPropertyValue(path[at],out var child))
            {
                location.Add(path[at]); Tokens(child,path,at+1,location,found,depth+1); location.RemoveAt(location.Count-1);
            }
        }
    }
    private static void Exempt(JsonNode response,JsonNode? clean,object[] parent,string[] secrets,PrivacyCatalog.Budget budget)
    {
        // Media/type hints or audio/payload names anywhere on the way keep the generic result.
        JsonNode? node=response;
        for(int step=0;;step++)
        {
            if(node is JsonObject obj&&PrivacyCatalog.IndexHints(obj.Select(pair=>new KeyValuePair<string,object?>(pair.Key,pair.Value))).Count>0) return;
            if(step==parent.Length) break;
            node=At(node,[parent[step]]);
        }
        var context=PrivacyCatalog.Context(string.Join('.',parent.Append("token")));
        if(context.Media||context.Audio||context.Payload) return;
        if(node is not JsonObject original||WireCore.S(original["token"]) is not {} token) return;
        // Only undo the name rule; any other change made by the generic pass stays.
        if(At(clean,parent) is not JsonObject target||WireCore.S(target["token"])!=PrivacyCatalog.Redacted) return;
        string value=PrivacyCatalog.String(token,"",null,secrets,true,budget);
        target["token"]=value;
        // Token bytes are another encoding of the same value.
        if(value!=token&&target.ContainsKey("bytes")) target["bytes"]=PrivacyCatalog.Redacted;
    }
    private static void MaskLogprobs(JsonNode? clean,object[] choice)
    {
        if(At(clean,choice) is JsonObject target&&target.ContainsKey("logprobs")) target["logprobs"]=PrivacyCatalog.Redacted;
    }
}
