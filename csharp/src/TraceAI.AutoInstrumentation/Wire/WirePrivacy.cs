using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>Shared by the parser and exporter, so derived content cannot bypass scrubbing.</summary>
internal static class WirePrivacy
{
    internal const string Redacted="<redacted>";
    private static readonly System.Reflection.FieldInfo? ExceptionMessage=typeof(Exception).GetField("_message",System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic);
    // Message is virtual. Read only the BCL's stored text, never an application's
    // override (including overrides nested inside AggregateException.Message).
    internal static string ErrorMessage(Exception error)=>ExceptionMessage?.GetValue(error) as string??error.GetType().Name;
    internal static bool CredentialParameter(string key)=>PrivacyCatalog.QueryName(key);
    internal static bool CredentialKey(string key)=>PrivacyCatalog.CredentialName(key);
    internal static string[] HeaderSecrets(IEnumerable<KeyValuePair<string,string>> headers) => headers
        .Where(h=>CredentialKey(h.Key)).SelectMany(h=>PrivacyCatalog.Leaf(h.Key).EndsWith("authorization",StringComparison.Ordinal)
            ?new[] { h.Value.Trim(' ','\t'),AfterScheme(h.Value.Trim(' ','\t')) }:new[] { h.Value.Trim(' ','\t') })
        .Where(s=>s.Length>=8).Distinct().OrderByDescending(s=>s.Length).ToArray();
    // Learn both the encoded and decoded query values: providers can echo either.
    // Overflow is reported on the observation, never encoded as a fake credential.
    internal static string[] QuerySecrets(string? url,RequestData? observation=null)
    {
        var secrets=new HashSet<string>(StringComparer.Ordinal);
        try
        {
            if(url==null) return [];
            if(url.Length>PrivacyCatalog.MaxText) throw new InvalidDataException("Credential query budget");
            var pairs=PrivacyCatalog.QueryPairs(url);
            if(pairs.Length>4096) throw new InvalidDataException("Credential query budget");
            foreach(var pair in pairs)
            {
                if(pair.Separator.Length==0||!CredentialParameter(pair.DecodedName)) continue;
                string raw=pair.Value,decoded=pair.DecodedValue;
                if(raw.Length>KnownSecrets.MaxLength||decoded.Length>KnownSecrets.MaxLength) throw new InvalidDataException("Credential value budget");
                if(raw.Length>=8) secrets.Add(raw);
                if(decoded.Length>=8) secrets.Add(decoded);
                if(secrets.Count>128) throw new InvalidDataException("Credential query budget");
            }
        }
        catch { if(observation!=null) observation.CredentialDiscoveryIncomplete=true; }
        return secrets.OrderByDescending(s=>s.Length).ToArray();
    }
    internal static string[] RequestSecrets(RequestData request,ReadOnlyMemory<byte>? buffered=null)
    {
        var secrets=new HashSet<string>(StringComparer.Ordinal);
        try
        {
            int remaining=PrivacyCatalog.MaxNodes,characters=PrivacyCatalog.MaxText;
            void Add(string secret)
            {
                if(secret.Length<8) return;
                if(secret.Length>4096 || (secrets.Add(secret) && secrets.Count>128)) throw new InvalidDataException("Credential budget");
            }
            void TextBudget(string text)
            { if((characters-=text.Length)<0) throw new InvalidDataException("Credential text budget"); }
            void Collect(JsonNode? node,int depth,string path="")
            {
                if(depth>PrivacyCatalog.MaxDepth || --remaining<0) throw new InvalidDataException("Credential traversal budget");
                if(node is JsonObject obj) foreach(var (key,value) in obj)
                {
                    TextBudget(key);
                    Collect(value,depth+1,path.Length>0?path+"."+key:key);
                }
                else if(node is JsonArray array) for(int i=0;i<array.Count;i++) Collect(array[i],depth+1,path+"."+i);
                else if(WireCore.S(node) is {} text)
                {
                    TextBudget(text);
                    if(CredentialKey(path)) Add(text);
                    else if(PrivacyJson(text) is {} nested) Collect(nested,depth+1,path);
                }
            }
            foreach(string secret in HeaderSecrets(request.Headers).Concat(QuerySecrets(request.Url,request))) Add(secret);
            // A parsed multipart body is represented by its bounded text fields.
            // Never inspect file bytes (or charge their size to credential work).
            if(buffered.HasValue || request.Body!=null && request.Fields==null)
            {
                ReadOnlyMemory<byte> body=buffered??request.Body!;
                if(body.Length>4*1024*1024) throw new InvalidDataException("Credential body budget");
                if(PrivacyJson(Encoding.UTF8.GetString(body.Span)) is {} node) Collect(node,0);
            }
            foreach(var pair in request.Fields??[])
            {
                if(--remaining<0) throw new InvalidDataException("Credential field budget");
                TextBudget(pair.Key); TextBudget(pair.Value);
                if(CredentialKey(pair.Key)) Add(pair.Value);
                else if(PrivacyJson(pair.Value) is {} node) Collect(node,0,pair.Key);
            }
        }
        catch { request.CredentialDiscoveryIncomplete=true; }
        // Keep already discovered credentials available to later echoes. An exhausted
        // request walk is not a registry failure: never register a fabricated secret
        // that permanently disables every unrelated span and resource in the process.
        return secrets.OrderByDescending(s=>s.Length).ToArray();
    }
    internal static bool ObserveField(string name,string value,bool complete=true)
    {
        // Publish fields as soon as the real-send parser sees them, including
        // duplicate names that a later part can overwrite. No response/span is
        // needed before unrelated telemetry gets the same process-wide protection.
        if(!complete && CredentialKey(name)) return true;
        var request=new RequestData { Fields=new() { [name]=value } };
        KnownSecrets.Register(RequestSecrets(request));
        return request.CredentialDiscoveryIncomplete;
    }
    private static string AfterScheme(string value)
    { int split=value.IndexOfAny([' ','\t']); return split<0?value:value[(split+1)..].Trim(); }
    internal const int MaxAttributeNodes=4096;
    internal static object Scrub(object value,IEnumerable<string>? secrets=null,string path="",Dictionary<string,PrivacyCatalog.Hint>? hints=null,bool framed=true)
    {
        try
        {
            int remaining=MaxAttributeNodes,characters=4*1024*1024;
            Inspect(value,0,new HashSet<object>(ReferenceEqualityComparer.Instance),ref remaining,ref characters);
            remaining=MaxAttributeNodes;
            return ScrubValue(value,secrets,0,new HashSet<object>(ReferenceEqualityComparer.Instance),ref remaining,path,hints,framed)!;
        }
        catch { return Redacted; }
    }
    // Preflight before copying or rendering: shared references to a large string
    // must not multiply into gigabytes of temporary sanitized output. Only closed
    // framework containers are inspected, never an application's enumerable/getter.
    private static void Inspect(object? value,int depth,HashSet<object> ancestors,ref int nodes,ref int characters)
    {
        if(depth>=64 || --nodes<0) throw new InvalidDataException("Privacy value budget");
        if(value is string text)
        { if((characters-=text.Length)<0) throw new InvalidDataException("Privacy text budget"); }
        else if(value is byte[]) return;
        else if(value is Array array)
        {
            if(array.Length>nodes || !ancestors.Add(array)) throw new InvalidDataException("Privacy array budget");
            try { foreach(var item in array) Inspect(item,depth+1,ancestors,ref nodes,ref characters); }
            finally { ancestors.Remove(array); }
        }
        else if(value is JsonElement element)
        {
            if(element.ValueKind==JsonValueKind.Object)
                foreach(var property in element.EnumerateObject())
                {
                    if((characters-=property.Name.Length)<0) throw new InvalidDataException("Privacy key budget");
                    Inspect(property.Value,depth+1,ancestors,ref nodes,ref characters);
                }
            else if(element.ValueKind==JsonValueKind.Array)
            {
                if(element.GetArrayLength()>nodes) throw new InvalidDataException("Privacy JSON array budget");
                foreach(var item in element.EnumerateArray()) Inspect(item,depth+1,ancestors,ref nodes,ref characters);
            }
            else if(element.ValueKind==JsonValueKind.String && (characters-=element.GetString()!.Length)<0) throw new InvalidDataException("Privacy JSON text budget");
        }
        else if(value is JsonNode node && node.GetType().Assembly==typeof(JsonNode).Assembly)
        {
            if(node is JsonObject obj) foreach(var pair in obj)
            {
                if((characters-=pair.Key.Length)<0) throw new InvalidDataException("Privacy key budget");
                Inspect(pair.Value,depth+1,ancestors,ref nodes,ref characters);
            }
            else if(node is JsonArray items)
            {
                if(items.Count>nodes) throw new InvalidDataException("Privacy JSON array budget");
                foreach(var item in items) Inspect(item,depth+1,ancestors,ref nodes,ref characters);
            }
            else if(node is JsonValue scalar)
            {
                if(scalar.TryGetValue<string>(out var str) && (characters-=str.Length)<0) throw new InvalidDataException("Privacy JSON text budget");
                if(scalar.TryGetValue<JsonElement>(out var json)) Inspect(json,depth+1,ancestors,ref nodes,ref characters);
            }
        }
    }
    private static object? ScrubValue(object? value,IEnumerable<string>? secrets,int depth,HashSet<object> ancestors,ref int remaining,
        string path,Dictionary<string,PrivacyCatalog.Hint>? hints,bool framed=true)
    {
        if(depth>PrivacyCatalog.MaxDepth || --remaining<0) throw new InvalidDataException("Privacy traversal limit");
        if(CredentialKey(path)) return Redacted;
        if(value is Array array && value is not byte[])
        {
            if(array.Rank!=1||array.Length>remaining||!ancestors.Add(array)) throw new InvalidDataException("Privacy array limit");
            try
            {
                if(PrivacyCatalog.Context(path,hints).Payload&&array.Length>0&&array.Cast<object?>().All(IsNumericOrBool)) return PrivacyCatalog.OtherMedia;
                var values=new object?[array.Length]; bool sameType=true;
                for(int i=0;i<array.Length;i++)
                {
                    var original=array.GetValue(i+array.GetLowerBound(0));
                    values[i]=ScrubValue(original,secrets,depth+1,ancestors,ref remaining,path+"."+i,hints,framed);
                    sameType&=original?.GetType()==values[i]?.GetType();
                }
                if(!sameType) return values;
                var copy=Array.CreateInstance(array.GetType().GetElementType()!,array.Length);
                for(int i=0;i<values.Length;i++) copy.SetValue(values[i],i);
                return copy;
            }
            finally { ancestors.Remove(array); }
        }
        return value switch {
            null=>null,
            string text=>PrivacyCatalog.String(text,path,hints,secrets,framed),
            JsonNode node when node.GetType().Assembly==typeof(JsonNode).Assembly=>WireCore.J(PrivacyCatalog.Scrub(SafeJson(node,depth,ref remaining),path,hints,secrets,framed)),
            JsonElement element=>WireCore.J(PrivacyCatalog.Scrub(JsonNode.Parse(element.GetRawText()),path,hints,secrets,framed)),
            double number when !double.IsFinite(number)=>PrivacyCatalog.String(number.ToString(System.Globalization.CultureInfo.InvariantCulture),path,hints,secrets,framed),
            float number when !float.IsFinite(number)=>PrivacyCatalog.String(number.ToString(System.Globalization.CultureInfo.InvariantCulture),path,hints,secrets,framed),
            bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal=>PrivacyCatalog.Context(path,hints).Payload?PrivacyCatalog.OtherMedia:value,
            byte[]=>Redacted,
            _=>PrivacyCatalog.String(value.GetType().FullName??value.GetType().Name,path,hints,secrets,framed)
        };
    }
    private static bool IsNumericOrBool(object? value)=>value is bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal;
    // JsonValue.Create<T> can wrap arbitrary application objects. Never ask the DOM
    // to serialize them: even a public getter can block Activity.Stop indefinitely.
    private static JsonNode? SafeJson(JsonNode? node,int depth,ref int remaining)
    {
        if(depth>=64 || --remaining<0) throw new InvalidDataException("Privacy JSON limit");
        if(node is JsonObject obj)
        {
            var safe=new JsonObject();
            foreach(var pair in obj) safe[pair.Key]=SafeJson(pair.Value,depth+1,ref remaining);
            return safe;
        }
        if(node is JsonArray array)
        {
            if(array.Count>remaining) throw new InvalidDataException("Privacy JSON limit");
            var safe=new JsonArray();
            foreach(var item in array) safe.Add(SafeJson(item,depth+1,ref remaining));
            return safe;
        }
        if(node is not JsonValue value) return null;
        // TryGetValue only unboxes the stored value; it does not run its serializer.
        if(value.TryGetValue<JsonElement>(out var element))
        {
            var parsed=JsonNode.Parse(element.GetRawText());
            return element.ValueKind is JsonValueKind.Object or JsonValueKind.Array?SafeJson(parsed,depth+1,ref remaining):parsed;
        }
        if(value.TryGetValue<string>(out var text)) return JsonValue.Create(text);
        if(value.TryGetValue<bool>(out var boolean)) return JsonValue.Create(boolean);
        if(value.TryGetValue<byte>(out var u8)) return JsonValue.Create(u8);
        if(value.TryGetValue<sbyte>(out var i8)) return JsonValue.Create(i8);
        if(value.TryGetValue<short>(out var i16)) return JsonValue.Create(i16);
        if(value.TryGetValue<ushort>(out var u16)) return JsonValue.Create(u16);
        if(value.TryGetValue<int>(out var i32)) return JsonValue.Create(i32);
        if(value.TryGetValue<uint>(out var u32)) return JsonValue.Create(u32);
        if(value.TryGetValue<long>(out var i64)) return JsonValue.Create(i64);
        if(value.TryGetValue<ulong>(out var u64)) return JsonValue.Create(u64);
        if(value.TryGetValue<float>(out var f32)) return float.IsFinite(f32)?JsonValue.Create(f32):JsonValue.Create(f32.ToString(System.Globalization.CultureInfo.InvariantCulture));
        if(value.TryGetValue<double>(out var f64)) return double.IsFinite(f64)?JsonValue.Create(f64):JsonValue.Create(f64.ToString(System.Globalization.CultureInfo.InvariantCulture));
        if(value.TryGetValue<decimal>(out var number)) return JsonValue.Create(number);
        var type=value.GetType().GenericTypeArguments.FirstOrDefault();
        return JsonValue.Create(type?.FullName??Redacted);
    }
    internal static string ScrubString(string value,IEnumerable<string>? secrets=null,string path="",Dictionary<string,PrivacyCatalog.Hint>? hints=null,bool framed=true)
    {
        try
        {
            if(value.Length>PrivacyCatalog.MaxText) return Redacted;
            return PrivacyCatalog.String(value,path,hints,secrets,framed);
        }
        catch { return Redacted; }
    }
    private static JsonNode? PrivacyJson(string value)
    {
        var text=value.AsSpan().TrimStart();
        return text.Length>0 && text[0] is '{' or '[' ? JsonNode.Parse(value) : null;
    }
}
