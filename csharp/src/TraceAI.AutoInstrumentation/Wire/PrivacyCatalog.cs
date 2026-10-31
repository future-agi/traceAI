using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace TraceAI.AutoInstrumentation.Wire;

/// <summary>S6 recognition contract loaded from the byte-identical shipping catalog.
/// Transport discovery and the detached export boundary use these same predicates.</summary>
internal static class PrivacyCatalog
{
    private static readonly JsonNode Catalog=Load();
    private static JsonNode Load()
    {
        using var stream=typeof(PrivacyCatalog).Assembly.GetManifestResourceStream("TraceAI.AutoInstrumentation.Wire.privacy.json")
            ??throw new InvalidDataException("Privacy catalog unavailable");
        return JsonNode.Parse(stream)!;
    }
    private static readonly JsonNode Credentials=Catalog["credentials"]!, Media=Catalog["media"]!, Mime=Media["mime_siblings"]!, Audio=Media["audio"]!;
    private static string[] Strings(JsonNode node)=>node.AsArray().Select(n=>n!.GetValue<string>()).ToArray();
    private static readonly string[] Patterns=Credentials["patterns"]!.AsArray().Select(n=>n!["contains"]!.GetValue<string>()).ToArray();
    private static readonly (Regex Rule,string[] Guards)[] Exclusions=Credentials["exclusions"]!.AsArray().Select(n=>(Rx(n!["regex"]!.GetValue<string>()),
        n["unless_contains_patterns"] is {} guards?Strings(guards).Select(id=>Credentials["patterns"]!.AsArray().First(p=>p!["id"]!.GetValue<string>()==id)!["contains"]!.GetValue<string>()).ToArray():Array.Empty<string>())).ToArray();
    private static readonly Regex[] ContextExclusions=Credentials["contextual_exclusions"]!.AsArray().Select(n=>Rx(n!["path_regex"]!.GetValue<string>())).ToArray();
    private static readonly HashSet<string> Signed=Strings(Catalog["signed_url"]!["parameters"]!).Select(Normalize).ToHashSet(StringComparer.Ordinal);
    private static readonly HashSet<char> Alphabet=Media["base64_alphabet"]!.GetValue<string>().ToHashSet();
    private static readonly HashSet<string> Unrecognized=Strings(Media["unrecognized_base64_fields"]!).ToHashSet(StringComparer.Ordinal);
    private static readonly HashSet<string> MimeNames=Strings(Mime["names"]!).ToHashSet(StringComparer.Ordinal), MimePayload=Strings(Mime["payload_fields"]!).ToHashSet(StringComparer.Ordinal);
    private static readonly string[] MimePrefixes=Strings(Mime["prefixes"]!), MimeExact=Strings(Mime["exact"]!);
    private static readonly HashSet<string> AudioFields=Strings(Audio["fields"]!).ToHashSet(StringComparer.Ordinal), AudioPayload=Strings(Audio["payload_fields"]!).ToHashSet(StringComparer.Ordinal),
        AudioTypes=Strings(Audio["type_names"]!).ToHashSet(StringComparer.Ordinal), Metadata=Strings(Audio["metadata_fields"]!).ToHashSet(StringComparer.Ordinal);
    private sealed record MagicTest(int Offset,byte[] Bytes,byte[] Mask);
    private static readonly MagicTest[][] Magic=Media["magic"]!.AsArray().Select(rule=>rule!["tests"]!.AsArray().Select(test=>{
        var bytes=Convert.FromHexString(test!["hex"]!.GetValue<string>());
        return new MagicTest(test["offset"]!.GetValue<int>(),bytes,test["mask"] is {} mask?Convert.FromHexString(mask.GetValue<string>()):Enumerable.Repeat((byte)255,bytes.Length).ToArray());
    }).ToArray()).ToArray();
    internal static readonly int MaxDepth=Catalog["limits"]!["max_depth"]!.GetValue<int>(), MaxNodes=Catalog["limits"]!["max_nodes"]!.GetValue<int>(), MaxText=Catalog["limits"]!["max_text_chars"]!.GetValue<int>();
    private static readonly int PrefixBytes=Media["base64_prefix_decoded_bytes"]!.GetValue<int>(), Threshold=Media["unrecognized_base64_min_chars_exclusive"]!.GetValue<int>();
    internal static readonly string Redacted=Credentials["replacement"]!.GetValue<string>(), OtherMedia=Media["other_replacement"]!.GetValue<string>();
    private static readonly string DataOther=Media["data_uri"]!["other_replacement"]!.GetValue<string>();
    private static Regex Rx(string pattern)=>new(pattern,RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(250));
    private const string OmissionPattern=@"<omitted: (?:[0-9]+ base64 chars|media|data URI)>";
    private static readonly Regex Omission=Rx(@"\A"+OmissionPattern+@"\z"), SplitPath=Rx(@"[.\[\]]+");
    private const string DataPattern="(?<![A-Za-z0-9+.\\-_:])(?i:data:)[^\\s\"'<>\\\\]*(?:[\\r\\n]+[A-Za-z0-9+/_=\\-]+)*";
    private const string UrlPattern="(?:(?i:https?://)|(?<!\\S)[/?])(?:<redacted>|"+OmissionPattern+"|[^\\s\"<>\\\\])+";
    private static readonly Regex Data=Rx(DataPattern), Url=Rx(UrlPattern), ExactUrl=Rx(@"\A(?:"+UrlPattern+@")\z"),
        TextTokens=Rx("(?<url>"+UrlPattern+")|(?<data>"+DataPattern+")"), UserInfo=Rx(@"\A((?i:https?://))[^\s/@]+:[^\s/@]+@");
    internal static string Normalize(string name)
    {
        var result=new StringBuilder(name.Length);
        foreach(char c in name) if(c is not ('-' or '_' or '.')) result.Append(c is >= 'A' and <= 'Z'?(char)(c+32):c);
        return result.ToString();
    }
    private static bool Numeric(string part)=>part.Length>0&&part.All(c=>c is >= '0' and <= '9');
    private static string[] Parts(string path,bool indices=true)=>SplitPath.Split(path).Where(p=>p.Length>0&&(indices||!Numeric(p))).ToArray();
    internal static string Leaf(string path)=>Normalize(Parts(path,false).LastOrDefault()??"");
    private static bool Full(Regex rule,string name) { var m=rule.Match(name); return m.Success&&m.Length==name.Length; }
    private static bool Excluded(string name)=>Exclusions.Any(e=>Full(e.Rule,name)&&!e.Guards.Any(name.Contains));
    internal static bool CredentialName(string name)
    {
        var parts=Parts(name,false).Select(Normalize).ToArray(); bool excluded=false;
        for(int i=0;i<parts.Length;i++)
        {
            string folded=parts[i], context=string.Join('.',parts.Take(i+1));
            if(Excluded(folded)||ContextExclusions.Any(r=>Full(r,context))) excluded=true;
            else if(Patterns.Any(folded.Contains)) return true;
        }
        string full=Normalize(name);
        return !excluded&&!Excluded(full)&&Patterns.Any(full.Contains);
    }
    internal static bool QueryName(string name)=>Signed.Contains(Normalize(name))||CredentialName(name);
    private static bool Encoded(string value)=>value.Length>0&&value.All(Alphabet.Contains);
    private static string Omitted(int count)=>Media["replacement"]!.GetValue<string>().Replace("{length}",count.ToString(System.Globalization.CultureInfo.InvariantCulture),StringComparison.Ordinal);
    private static bool HasMagic(string value)
    {
        if(!Encoded(value)) return false;
        // Only decode the catalog's prefix budget, even for multi-megabyte media.
        int chars=(PrefixBytes*8+5)/6;
        var prefix=new StringBuilder(chars+3);
        foreach(char c in value)
        {
            if(c is '\r' or '\n') continue;
            prefix.Append(c=='-'?'+':c=='_'?'/':c);
            if(prefix.Length==chars) break;
        }
        while(prefix.Length%4!=0) prefix.Append('=');
        byte[] decoded;
        try { decoded=Convert.FromBase64String(prefix.ToString()); } catch(FormatException) { return false; }
        int length=Math.Min(decoded.Length,PrefixBytes);
        return Magic.Any(rule=>rule.All(test=>test.Offset+test.Bytes.Length<=length&&Enumerable.Range(0,test.Bytes.Length).All(i=>(decoded[test.Offset+i]&test.Mask[i])==(test.Bytes[i]&test.Mask[i]))));
    }
    internal readonly record struct Hint(bool Media,bool Audio);
    private static Hint GetHint(string name,object? value,Budget budget)
    {
        string leaf=Leaf(name);
        if(!MimeNames.Contains(leaf)&&leaf!="type") return default;
        if(value is JsonValue json) value=WireCore.S(json);
        if(value is JsonArray array)
        {
            if(array.Count>MaxNodes) throw new InvalidDataException("Privacy hint budget");
            return HintStrings(leaf,array.Select(v=>{ budget.Node(0); return WireCore.S(v); }).OfType<string>(),budget);
        }
        if(value is Array values)
        {
            if(values.Length>MaxNodes) throw new InvalidDataException("Privacy hint budget");
            return HintStrings(leaf,values.Cast<object?>().Select(v=>{ budget.Node(0); return v as string; }).OfType<string>(),budget);
        }
        return value is string text?ScalarHint(leaf,text,budget):default;
    }
    private static Hint HintStrings(string leaf,IEnumerable<string> values,Budget budget)=>Combine(values.Select(text=>ScalarHint(leaf,text,budget)));
    private static Hint ScalarHint(string leaf,string text,Budget budget)
    {
        budget.Text(text.Length);
        if(text.Length>MaxText) throw new InvalidDataException("Privacy hint text budget");
        string mime=text.Split(';',2)[0].Trim().ToLowerInvariant();
        if(MimeNames.Contains(leaf)) return new(MimePrefixes.Any(p=>mime.StartsWith(p,StringComparison.Ordinal))||MimeExact.Contains(mime),mime.StartsWith("audio/",StringComparison.Ordinal));
        return new(false,leaf=="type"&&AudioTypes.Contains(Normalize(text)));
    }
    private static Hint Combine(IEnumerable<Hint> hints) { bool media=false,audio=false; foreach(var hint in hints) { media|=hint.Media; audio|=hint.Audio; } return new(media,audio); }
    internal static Dictionary<string,Hint> IndexHints(IEnumerable<KeyValuePair<string,object?>> items,string path="",Dictionary<string,Hint>? inherited=null)
    {
        var hints=inherited==null?new Dictionary<string,Hint>(StringComparer.Ordinal):new(inherited,StringComparer.Ordinal);
        var budget=new Budget();
        foreach(var pair in items)
        {
            budget.Node(0); budget.Text(pair.Key.Length);
            var hint=GetHint(pair.Key,pair.Value,budget); if(!hint.Media&&!hint.Audio) continue;
            var parts=Parts(path.Length>0?path+"."+pair.Key:pair.Key).ToList();
            while(parts.Count>0&&Numeric(parts[^1])) parts.RemoveAt(parts.Count-1);
            if(parts.Count>0) parts.RemoveAt(parts.Count-1);
            string parent=string.Join('.',parts); hints.TryGetValue(parent,out var previous);
            hints[parent]=new(previous.Media||hint.Media,previous.Audio||hint.Audio);
        }
        return hints;
    }
    internal static (bool Media,bool Audio,bool Payload) Context(string path,Dictionary<string,Hint>? hints=null,bool media=false,bool audio=false)
    {
        hints??=[]; hints.TryGetValue("",out var root); media|=root.Media; audio|=root.Audio;
        bool payload=false,direct=false; var prefix=new List<string>();
        foreach(string part in Parts(path))
        {
            prefix.Add(part); string name=Normalize(part);
            if(!Numeric(part))
            {
                if(Metadata.Contains(name)) { media=false; audio=false; payload=false; }
                direct=AudioFields.Contains(name); audio|=direct;
                if(media&&MimePayload.Contains(name)||audio&&AudioPayload.Contains(name)) payload=true;
            }
            if(hints.TryGetValue(string.Join('.',prefix),out var hint)) { media|=hint.Media; audio|=hint.Audio; }
        }
        return (media,audio,payload||direct);
    }
    internal static string? DataUri(string value)
    {
        if(!value.StartsWith("data:",StringComparison.OrdinalIgnoreCase)) return null;
        int comma=value.IndexOf(','); if(comma<0) return null;
        string metadata=value[5..comma],payload=Uri.UnescapeDataString(value[(comma+1)..]);
        return metadata.EndsWith(";base64",StringComparison.OrdinalIgnoreCase)&&(payload.Length==0||Encoded(payload))?Omitted(payload.Length):DataOther;
    }
    // The only media predicate; path context, magic bytes and exact data URI boundaries
    // are independent rules. Stable omission markers never count as payload bytes.
    internal static string? MediaString(string value,string path="",Dictionary<string,Hint>? hints=null,bool framed=true,bool media=false,bool audio=false)
    {
        if(value==Redacted||value==FIInstrumentation.TraceConfig.RedactedValue||Full(Omission,value)) return null;
        if(framed&&DataUri(value) is {} data) return data;
        bool encoded=Encoded(value);
        if(HasMagic(value)||Context(path,hints,media,audio).Payload) return encoded?Omitted(value.Length):OtherMedia;
        return encoded&&Unrecognized.Contains(Leaf(path))&&value.Length>Threshold?Omitted(value.Length):null;
    }
    private static string DataText(string value)=>Data.Replace(value,m=>DataUri(m.Value)??m.Value);
    internal static string DecodeQuery(string value)=>Uri.UnescapeDataString(value.Replace('+',' '));
    internal readonly record struct QueryPair(string Name,string Separator,string Value,string DecodedName,string DecodedValue);
    internal static QueryPair[] QueryPairs(string url)
    {
        string target=url.Split('#',2)[0]; int question=target.IndexOf('?'); if(question<0) return [];
        return target[(question+1)..].Split('&').Select(pair=>{
            int equals=pair.IndexOf('='); string name=equals<0?pair:pair[..equals],value=equals<0?"":pair[(equals+1)..];
            return new QueryPair(name,equals<0?"":"=",value,DecodeQuery(name),DecodeQuery(value));
        }).ToArray();
    }
    private static string UrlValue(string original,bool validation=false)
    {
        int hash=original.IndexOf('#'); string target=hash<0?original:original[..hash],fragment=hash<0?"":original[hash..];
        int question=target.IndexOf('?'); string head=question<0?target:target[..question];
        var parsed=QueryPairs(original);
        var hints=IndexHints(parsed.Where(p=>p.Separator.Length>0).Select(p=>new KeyValuePair<string,object?>(p.DecodedName,p.DecodedValue)));
        var pairs=new string[parsed.Length];
        for(int i=0;i<parsed.Length;i++)
        {
            var pair=parsed[i]; string value=pair.Value;
            if(pair.Separator.Length>0)
            {
                if(validation) { if(DataUri(pair.DecodedValue)!=null) value=DataOther; }
                else if(QueryName(pair.DecodedName)) value=Redacted;
                else
                {
                    string? replacement=MediaString(pair.DecodedValue,pair.DecodedName,hints);
                    if(replacement==null) { string clean=DataText(pair.DecodedValue); if(clean!=pair.DecodedValue) replacement=clean; }
                    if(replacement!=null) value=replacement;
                }
            }
            pairs[i]=pair.Name+pair.Separator+value;
        }
        if(!validation) head=DataText(UserInfo.Replace(head,"$1"+Redacted+"@"));
        return head+(question<0?"":"?"+string.Join('&',pairs))+(validation?fragment:DataText(fragment));
    }
    private static bool IsUrlValue(string value)
    {
        if(Full(ExactUrl,value)) return true;
        if(!(value.StartsWith("http://",StringComparison.OrdinalIgnoreCase)||value.StartsWith("https://",StringComparison.OrdinalIgnoreCase)||value.StartsWith('/')||value.StartsWith('?'))) return false;
        string candidate=UrlValue(value,true);
        return candidate!=value&&Full(ExactUrl,candidate);
    }
    internal static string Text(string value,IEnumerable<string>? secrets=null,bool framed=true)
    {
        if(framed&&IsUrlValue(value)) value=UrlValue(value);
        else
        {
            string source=value;
            value=TextTokens.Replace(value,m=>{
                if(m.Groups["data"].Success) return DataText(m.Value);
                string original=m.Value,quote="";
                if(m.Index>0&&source[m.Index-1]=='\''&&original.EndsWith('\'')) { original=original[..^1]; quote="'"; }
                return UrlValue(original)+quote;
            });
        }
        foreach(string secret in (secrets??[]).Where(s=>s.Length>=8&&s!=Redacted).Distinct(StringComparer.Ordinal).OrderByDescending(s=>s.Length).ThenBy(s=>s,StringComparer.Ordinal))
            value=string.Join(Redacted,value.Split(Redacted,StringSplitOptions.None).Select(part=>part.Replace(secret,Redacted,StringComparison.Ordinal)));
        return value;
    }
    internal sealed class Budget
    {
        private int nodes=MaxNodes,characters=MaxText;
        internal void Node(int depth) { if(depth>MaxDepth||--nodes<0) throw new InvalidDataException("Privacy structure budget"); }
        internal void Text(int length) { if((characters-=length)<0) throw new InvalidDataException("Privacy text budget"); }
    }
    internal static JsonNode? Scrub(JsonNode? value,string path="",Dictionary<string,Hint>? hints=null,IEnumerable<string>? secrets=null,bool framed=true,Budget? budget=null,int depth=0,bool media=false,bool audio=false)
    {
        budget??=new(); budget.Node(depth);
        if(CredentialName(path)) return JsonValue.Create(Redacted);
        if(value is JsonObject obj)
        {
            budget.Text(obj.Sum(p=>p.Key.Length));
            hints=IndexHints(obj.Select(p=>new KeyValuePair<string,object?>(p.Key,p.Value)),path,hints);
            var result=new JsonObject();
            foreach(var pair in obj)
            {
                string key=Text(pair.Key,secrets),child=path.Length>0?path+"."+pair.Key:pair.Key;
                // Preserve the pre-S6 embedding-vector export policy. This is not
                // a credential/media recognition list and never affects harvesting.
                result[key]=pair.Key.ToLowerInvariant() is "embedding" or "embeddings" && pair.Value is JsonObject or JsonArray
                    ?JsonValue.Create(Redacted):Scrub(pair.Value,child,hints,secrets,true,budget,depth+1,media,audio);
            }
            return result;
        }
        var context=Context(path,hints,media,audio);
        if(value is JsonArray array)
        {
            if(context.Payload&&array.Count>0&&array.All(IsNumberOrBool)) return JsonValue.Create(OtherMedia);
            var result=new JsonArray();
            for(int i=0;i<array.Count;i++) result.Add(Scrub(array[i],path+"."+i,hints,secrets,true,budget,depth+1,media,audio));
            return result;
        }
        if(WireCore.S(value) is not {} text) return value!=null&&context.Payload?JsonValue.Create(OtherMedia):value?.DeepClone();
        budget.Text(text.Length);
        return JsonValue.Create(String(text,path,hints,secrets,framed,budget,depth,media,audio));
    }
    private static bool IsNumberOrBool(JsonNode? value)=>value is JsonValue scalar&&(scalar.GetValueKind() is System.Text.Json.JsonValueKind.Number or System.Text.Json.JsonValueKind.True or System.Text.Json.JsonValueKind.False);
    internal static string String(string value,string path="",Dictionary<string,Hint>? hints=null,IEnumerable<string>? secrets=null,bool framed=true,Budget? budget=null,int depth=0,bool media=false,bool audio=false)
    {
        if(CredentialName(path)) return Redacted;
        if(MediaString(value,path,hints,framed,media,audio) is {} replacement) return replacement;
        var trimmed=value.AsSpan().TrimStart();
        if(trimmed.Length>0&&trimmed[0] is '{' or '[')
        {
            // Invalid/deep serialized JSON fails closed at S1 rather than being treated as prose.
            var parsed=JsonNode.Parse(value); var context=Context(path,hints,media,audio);
            var clean=Scrub(parsed,"",null,secrets,true,budget,depth+1,context.Media,context.Audio);
            if(!JsonNode.DeepEquals(parsed,clean)) value=WireCore.J(clean);
        }
        return Text(value,secrets,framed);
    }
}
