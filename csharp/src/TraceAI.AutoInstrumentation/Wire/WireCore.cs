using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace TraceAI.AutoInstrumentation.Wire;

internal sealed record FilePart(string Name, string? Filename, string? ContentType, long? Size);
internal sealed class RequestData
{
    public string Method = "POST", Url = "";
    public Dictionary<string, string> Headers = new(StringComparer.OrdinalIgnoreCase);
    public byte[]? Body;
    public Dictionary<string, string>? Fields;
    public List<FilePart> Files = [];
    public long? BodySize;
    public bool Truncated, BodyUnavailable;
    // Discovery exhaustion belongs to this request, not the process-wide registry.
    public bool CredentialDiscoveryIncomplete;
    public int MaxBody=4194304;
    public string? UndecodedEncoding;
    public string ContentType => WireCore.ContentType(Headers);
}
internal sealed class ResponseData
{
    public int? Status;
    public Dictionary<string, string> Headers = new(StringComparer.OrdinalIgnoreCase);
    public byte[]? Body;
    public long Size;
    public bool Truncated, Incomplete;
    public int MaxBody=4194304;
    public string? UndecodedEncoding;
    public double? FirstChunkSeconds, FirstTextSeconds;
    internal bool ArrivalTrackingLost;
    internal readonly List<(long End,double Seconds)> Arrivals=[];
    internal void ObserveChunk(int count,double seconds)
    {
        if(count<=0) return;
        FirstChunkSeconds??=seconds;
        Size+=count;
        if(Arrivals.Count<StreamMilestones.MaxArrivals && !WireCore.IsBinary(ContentType) &&
            !Headers.Any(pair=>pair.Key.Equals("content-encoding",StringComparison.OrdinalIgnoreCase)&&pair.Value is not ("" or "identity")))
            Arrivals.Add((Size,seconds));
        else ArrivalTrackingLost=true;
    }
    internal double? ArrivalAt(int end)
    {
        // Earliest chunk that completed the frame, not the first byte of its JSON.
        int low=0,high=Arrivals.Count-1;
        while(low<=high)
        {
            int middle=low+(high-low)/2;
            if(Arrivals[middle].End<end) low=middle+1; else high=middle-1;
        }
        return low<Arrivals.Count?Arrivals[low].Seconds:null;
    }
    public Exception? Error;
    public string ContentType => WireCore.ContentType(Headers);
}
internal sealed record WireMatch(JsonNode Endpoint, string Provider, Uri Url, Dictionary<string, string> Groups,
    Dictionary<string, string> Query, bool KnownProvider);
internal sealed record SpanResult(string Name, Dictionary<string, object> Attributes, bool Ok, string? Description);

/// <summary>Transport-independent port of the shared Python wire parser. No HTTP or telemetry dependencies.</summary>
internal static class WireCore
{
    internal static readonly JsonSerializerOptions JsonOptions = new() { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    internal static readonly byte[] CatalogBytes = LoadCatalog();
    private static readonly JsonNode Catalog = JsonNode.Parse(CatalogBytes)!;
    private static readonly (JsonNode Endpoint,Regex Pattern)[] Endpoints = A(Catalog["endpoints"])
        .Select(e=>(e,new Regex(S(e["path"])!,RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100)))).ToArray();
    private static readonly HashSet<string> ContentKeys = new("messages input prompt contents system systemInstruction system_instruction tools documents text file transcript instructions chat_history message preamble texts inputs query content requests audio_url url file_url image_url".Split(' '));
    internal static byte[] LoadCatalog()
    {
        using var stream = typeof(WireCore).Assembly.GetManifestResourceStream("TraceAI.AutoInstrumentation.Wire.wire-protocols.json")!;
        using var bytes = new MemoryStream(); stream.CopyTo(bytes); return bytes.ToArray();
    }
    internal static JsonNode? Parse(byte[]? bytes) { try { return bytes == null ? null : JsonNode.Parse(bytes); } catch { return null; } }
    internal static JsonNode? G(JsonNode? n, params string[] keys)
    {
        foreach (var key in keys) n = n is JsonObject o ? o[key] : n is JsonArray a && int.TryParse(key, out int i) && i >= 0 && i < a.Count ? a[i] : null;
        return n;
    }
    internal static string? S(JsonNode? n) => n is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
    private static object? Number(JsonNode? n)
    {
        if(n is not JsonValue v) return null;
        if(v.TryGetValue<long>(out var integer)) return integer;
        if(v.TryGetValue<double>(out var number)) return number;
        return null;
    }
    private static long? Int(JsonNode? n) => Number(n) switch {
        long integer => integer,
        double number when double.IsFinite(number) && number>=long.MinValue && number<long.MaxValue => (long)Math.Truncate(number),
        _ => null
    };
    private static bool True(JsonNode? n) => n is JsonValue v && v.TryGetValue<bool>(out bool b) && b;
    internal static IEnumerable<JsonNode> A(JsonNode? n) => n is JsonArray a ? a.Where(x => x != null).Select(x => x!) : [];
    internal static string J(object? n) => n is JsonNode node ? node.ToJsonString(JsonOptions) : JsonSerializer.Serialize(n, JsonOptions);
    private static string? Args(JsonNode? n) => n == null ? null : S(n) ?? J(n);
    internal static string ContentType(Dictionary<string, string> h) => h.FirstOrDefault(p => p.Key.Equals("content-type", StringComparison.OrdinalIgnoreCase)).Value?.Split(';')[0].Trim().ToLowerInvariant() ?? "";
    internal static bool IsBinary(string ct) => ct.StartsWith("audio/") || ct.StartsWith("video/") || ct.StartsWith("image/") || ct is "application/octet-stream" or "application/vnd.amazon.eventstream";
    internal static void Put(Dictionary<string, object> a, string key, object? value)
    {
        if (value == null || value is string s && s.Length == 0 || value is string[] { Length: 0 }) return;
        a[key] = value is JsonNode n ? J(n) : value;
    }
    internal static WireMatch? Match(string method, string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return null;
        string? hostProvider = null;
        string[] rules=["hosts", "host_suffixes", "host_prefixes", "ports"];
        bool Matches(JsonNode p,string rule) => A(p[rule]).Any(v=>rule switch {
            "hosts"=>string.Equals(S(v),uri.Host,StringComparison.OrdinalIgnoreCase),
            "host_suffixes"=>uri.Host.EndsWith(S(v)!,StringComparison.OrdinalIgnoreCase),
            "host_prefixes"=>uri.Host.StartsWith(S(v)!,StringComparison.OrdinalIgnoreCase),
            _=>Int(v)==uri.Port
        });
        foreach(var rule in rules)
        {
            foreach(var provider in A(Catalog["providers"]))
            {
                if(!Matches(provider,rule)) continue;
                if(S(provider["match"])=="all" && !rules.Where(r=>provider[r] is JsonArray { Count:>0 }).All(r=>Matches(provider,r))) continue;
                hostProvider=S(provider["name"]); break;
            }
            if(hostProvider!=null) break;
        }
        foreach (var (e,regex) in Endpoints)
        {
            var provider=hostProvider??S(e["default_provider"])??S(Catalog["default_provider"])!;
            if (!A(e["methods"]).Any(v => S(v) == method.ToUpperInvariant())) continue;
            if (e["providers"] is JsonArray && !A(e["providers"]).Any(v => S(v) == provider)) continue;
            if (A(e["exclude_providers"]).Any(v => S(v) == provider)) continue;
            var found = regex.Match(uri.AbsolutePath); if (!found.Success) continue;
            var groups = regex.GetGroupNames().Where(x => !int.TryParse(x, out _) && found.Groups[x].Success)
                .ToDictionary(x => x, x => Uri.UnescapeDataString(found.Groups[x].Value));
            var query = new Dictionary<string, string>();
            foreach (var part in uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
            {
                var pair = part.Split('=', 2);
                if (pair.Length == 2) query.TryAdd(Uri.UnescapeDataString(pair[0]), Uri.UnescapeDataString(pair[1].Replace('+', ' ')));
            }
            return new(e, provider, uri, groups, query, hostProvider!=null);
        }
        return null;
    }
    private sealed class Call(string? id, string? name, string? arguments)
    { public string? Id = id, Name = name, Arguments = arguments; }
    private sealed class Message(string role, string? content = null)
    { public string Role = role; public string? Content = content, ToolId; public List<Call> Calls = []; }
    private sealed class Parsed
    {
        public string? Model, ResponseModel, Id, Input, Output, InputMime, OutputMime;
        public List<string> Finish = [];
        public List<Message> In = [], Out = [];
        public List<JsonNode> Tools = [];
        public Dictionary<string, object> Extra = [], Params = [];
        public Dictionary<string, long> Usage = [];
        public HashSet<string> UnknownUsage = [];
        public string? UsageAlgebra;
        public bool Skip;
    }
    private sealed record Body(string Kind, JsonNode? Json = null, string? Text = null, List<JsonNode>? Events = null)
    { public IEnumerable<JsonNode> Payloads => Events ?? []; public bool Streamed => Kind is "sse" or "ndjson"; }
    internal static string? Text(JsonNode? c)
    {
        if (S(c) is { } s) return s;
        if (c is JsonObject) return S(c["text"]) ?? Text(c["parts"]);
        var texts = A(c).Select(p => S(p) ?? (G(p, "type") == null || S(G(p, "type")) is "text" or "input_text" or "output_text" ? S(G(p, "text")) : null)).Where(s => s != null).ToArray();
        return texts.Length == 0 ? null : string.Join("\n", texts);
    }
    private static Message OpenMessage(JsonNode raw)
    {
        var m = new Message(S(G(raw, "role")) ?? "user", Text(G(raw, "content"))) { ToolId = S(G(raw, "tool_call_id")) };
        foreach (var c in A(G(raw, "tool_calls"))) m.Calls.Add(new(S(G(c, "id")), S(G(c, "function", "name")), Args(G(c, "function", "arguments"))));
        return m;
    }
    private static Message AnthropicMessage(string role, JsonNode? content)
    {
        var m = new Message(role, S(content)); if (m.Content != null) return m;
        var texts = new List<string>();
        foreach (var b in A(content))
            switch (S(G(b, "type")))
            {
                case "text": if (S(G(b, "text")) is { } t) texts.Add(t); break;
                case "tool_use": m.Calls.Add(new(S(G(b,"id")), S(G(b,"name")), Args(G(b,"input")))); break;
                case "tool_result": m.ToolId = S(G(b,"tool_use_id")); if (Text(G(b,"content")) is { } r) texts.Add(r); break;
            }
        m.Content = texts.Count == 0 ? null : string.Join("\n", texts); return m;
    }
    private static Message GeminiMessage(JsonNode raw)
    {
        var role = S(G(raw,"role")) ?? "user"; var m = new Message(role == "model" ? "assistant" : role);
        var texts = new List<string>();
        foreach (var b in A(G(raw,"parts")))
        {
            if (S(G(b,"text")) is { } t && !True(G(b,"thought"))) texts.Add(t);
            else if (G(b,"functionCall") is { } c) m.Calls.Add(new(S(G(c,"id")), S(G(c,"name")), Args(G(c,"args"))));
            else if (G(b,"functionResponse") is { } r) { m.Role = "tool"; m.ToolId = S(G(r,"id")) ?? S(G(r,"name")); texts.Add(Args(G(r,"response")) ?? ""); }
        }
        m.Content = texts.Count == 0 ? null : string.Join("\n",texts); return m;
    }
    private static Message BedrockMessage(JsonNode raw)
    {
        var m = new Message(S(G(raw,"role")) ?? "user"); var texts = new List<string>();
        foreach (var b in A(G(raw,"content")))
        {
            if (S(G(b,"text")) is { } t) texts.Add(t);
            else if (G(b,"toolUse") is { } c) m.Calls.Add(new(S(G(c,"toolUseId")), S(G(c,"name")), Args(G(c,"input"))));
            else if (G(b,"toolResult") is { } r) { m.ToolId = S(G(r,"toolUseId")); texts.AddRange(A(G(r,"content")).Select(Text).OfType<string>()); }
        }
        m.Content = texts.Count == 0 ? null : string.Join("\n", texts); return m;
    }
    private static void Params(JsonNode b, Parsed p)
    {
        foreach (var key in new[] { "temperature", "top_p", "frequency_penalty", "presence_penalty", "seed" }) Put(p.Params, key, Number(G(b,key)));
        foreach (var key in new[] { "max_tokens", "max_completion_tokens", "max_output_tokens" }) if (Number(G(b,key)) is { } n) { p.Params["max_tokens"] = n; break; }
    }
    private static void MapParams(JsonNode? config, Parsed p, params (string, string)[] map)
    { foreach (var (src, dst) in map) Put(p.Params, dst, Number(G(config,src))); }
    private static bool Request(string f, JsonNode b, Parsed p)
    {
        switch (f)
        {
            case "openai.chat": case "ollama.chat":
                if (G(b,"messages") is JsonArray) p.In.AddRange(A(b["messages"]).Where(x => x is JsonObject).Select(OpenMessage));
                else if (f == "openai.chat" && S(G(b,"prompt")) is { } prompt) p.In.Add(new("user", prompt)); else return false;
                p.Tools = A(b["tools"]).ToList(); Params(b,p); break;
            case "openai.completions":
                if (G(b,"prompt") == null) return false;
                p.In.Add(new("user", S(b["prompt"]) ?? string.Join("\n", A(b["prompt"]).Select(S).OfType<string>()))); Params(b,p); break;
            case "openai.responses":
                if (G(b,"input") == null && G(b,"model") == null) return false;
                if (S(b["instructions"]) is { } instructions) p.In.Add(new("system", instructions));
                if (S(b["input"]) is { } input) p.In.Add(new("user",input));
                else foreach (var item in A(b["input"]).Where(x => x is JsonObject))
                {
                    if (S(item["type"]) == "function_call_output") p.In.Add(new("tool", Text(item["output"]) ?? Args(item["output"])) { ToolId = S(item["call_id"]) });
                    else if (S(item["type"]) == "function_call") p.In.Add(new("assistant") { Calls = [new(S(item["call_id"]), S(item["name"]), Args(item["arguments"]))] });
                    else if (S(item["role"]) is { } role) p.In.Add(new(role,Text(item["content"])));
                }
                p.Tools = A(b["tools"]).ToList(); Params(b,p); break;
            case "openai.embeddings":
                if (G(b,"input") == null) return false;
                p.Extra["gen_ai.request.embedding_inputs_count"] = b["input"] is JsonArray { Count: > 0 } a && Int(a[0]) == null ? a.Count : 1; break;
            case "openai.audio_speech": case "deepgram.speak": case "elevenlabs.tts": case "cartesia.tts":
                var key = f == "openai.audio_speech" ? "input" : f == "cartesia.tts" ? "transcript" : "text";
                if (S(G(b,key)) is not { } text) return false;
                p.Input = text; p.InputMime = "text/plain"; p.Extra["gen_ai.audio.transcript"] = text;
                if (f == "openai.audio_speech") Put(p.Extra,"gen_ai.request.voice",S(b["voice"])); break;
            case "openai.images":
                if (S(G(b,"prompt")) is not { } img) return false; p.In.Add(new("user",img)); break;
            case "openai.moderations": return G(b,"input") != null;
            case "rerank":
                if (S(G(b,"query")) is not { } query || G(b,"documents") is not JsonArray docs) return false;
                p.In.Add(new("user",query)); p.Extra["gen_ai.rerank.documents_count"] = docs.Count;
                Put(p.Extra,"gen_ai.request.top_n",Int(b["top_n"] ?? b["top_k"])); break;
            case "anthropic.messages":
                if (G(b,"messages") is not JsonArray) return false;
                if (G(b,"system") is { } sys) p.In.Add(new("system",Text(sys)));
                p.In.AddRange(A(b["messages"]).Where(x => x is JsonObject).Select(x => AnthropicMessage(S(x["role"]) ?? "user",x["content"])));
                p.Tools = A(b["tools"]).ToList(); Params(b,p); break;
            case "gemini.generate_content":
                if (G(b,"contents") is not { } contents) return false;
                if ((b["systemInstruction"] ?? b["system_instruction"]) is { } system) p.In.Add(new("system",Text(system)));
                if (S(contents) is { } str) p.In.Add(new("user",str));
                else p.In.AddRange((contents is JsonArray ? A(contents) : [contents]).Where(x => x is JsonObject).Select(GeminiMessage));
                p.Tools = A(b["tools"]).ToList();
                MapParams(b["generationConfig"] ?? b["generation_config"],p,("temperature","temperature"),("topP","top_p"),("maxOutputTokens","max_tokens"),("seed","seed"),("frequencyPenalty","frequency_penalty"),("presencePenalty","presence_penalty")); break;
            case "gemini.embed_content":
                if (G(b,"content") == null && G(b,"requests") is not JsonArray) return false;
                p.Extra["gen_ai.request.embedding_inputs_count"] = b["content"] != null ? 1 : ((JsonArray)b["requests"]!).Count; break;
            case "cohere.chat":
                if (G(b,"messages") is JsonArray) p.In.AddRange(A(b["messages"]).Where(x=>x is JsonObject).Select(OpenMessage));
                else if (S(G(b,"message")) is { } msg)
                {
                    if (S(b["preamble"]) is { } pre) p.In.Add(new("system",pre));
                    foreach (var turn in A(b["chat_history"])) p.In.Add(new(S(G(turn,"role"))?.ToUpperInvariant() switch { "CHATBOT"=>"assistant", "SYSTEM"=>"system", _=>"user" },Text(G(turn,"message"))));
                    p.In.Add(new("user",msg));
                } else return false;
                p.Tools = A(b["tools"]).ToList(); Params(b,p); Put(p.Params,"top_p",Number(b["p"])); break;
            case "cohere.embed":
                if ((G(b,"texts") ?? G(b,"inputs") ?? G(b,"images")) is not JsonArray list) return false;
                p.Extra["gen_ai.request.embedding_inputs_count"] = list.Count; break;
            case "bedrock.converse":
                p.UsageAlgebra="cache-inclusive";
                if (G(b,"messages") is not JsonArray) return false;
                var sysText = string.Join("\n",A(b["system"]).Select(Text).OfType<string>()); if (sysText.Length > 0) p.In.Add(new("system",sysText));
                p.In.AddRange(A(b["messages"]).Where(x=>x is JsonObject).Select(BedrockMessage));
                MapParams(b["inferenceConfig"],p,("temperature","temperature"),("topP","top_p"),("maxTokens","max_tokens")); p.Tools = A(G(b,"toolConfig","tools")).ToList(); break;
            case "bedrock.invoke": if (b is not JsonObject) return false; if (b["messages"] is JsonArray) Request("anthropic.messages",b,p); break;
            case "ollama.generate":
                if (S(G(b,"prompt")) is not { } pr) return false;
                if (S(b["system"]) is { } sy) p.In.Add(new("system",sy)); p.In.Add(new("user",pr)); break;
            case "ollama.embed":
                if ((G(b,"input") ?? G(b,"prompt")) is not { } val) return false;
                p.Extra["gen_ai.request.embedding_inputs_count"] = val is JsonArray ar ? ar.Count : 1; break;
        }
        if (f is "ollama.chat" or "ollama.generate")
        { p.Params.Clear(); MapParams(b["options"],p,("temperature","temperature"),("top_p","top_p"),("num_predict","max_tokens"),("seed","seed")); }
        return true;
    }
    private static Parsed? Check(WireMatch m, RequestData req)
    {
        var f = S(m.Endpoint["format"])!; var p = new Parsed();
        bool multipart = f is "openai.audio_transcription" or "elevenlabs.stt";
        bool any = f is "deepgram.listen" or "assemblyai.transcript";
        if(req.Fields!=null&&!multipart) return null;
        if(req.Body!=null&&!multipart&&!IsBinary(req.ContentType))
        {
            req.Body=BodyDecoder.Decode(req.Body,req.Headers,req.MaxBody,out var truncated,out var undecoded);
            req.Truncated|=truncated; req.UndecodedEncoding=undecoded; req.BodyUnavailable|=undecoded!=null;
        }
        bool unavailable=req.BodyUnavailable || (req.Fields==null && req.Truncated);
        if(unavailable)
        {
            if(!m.KnownProvider) return null;
            if(!req.Truncated) p.Extra["traceai.wire.body_unavailable"]=true;
        }
        else if (multipart)
        {
            if(req.Fields==null && req.Body!=null && req.Headers.TryGetValue("content-type",out var contentType)) ParseMultipart(req,contentType);
            var fields = req.Fields ?? new(); var modelKey = f == "elevenlabs.stt" ? "model_id" : "model";
            if (!fields.TryGetValue(modelKey,out var model) || string.IsNullOrEmpty(model)) return null;
            p.Model = model; Put(p.Extra,"gen_ai.audio.language",fields.GetValueOrDefault("language") ?? fields.GetValueOrDefault("language_code"));
            var parameters = fields.Where(kv=>!ContentKeys.Contains(kv.Key) && kv.Key != modelKey).ToDictionary(); if (parameters.Count > 0) p.Params["_json"] = J(parameters);
            var described = fields.ToDictionary(kv=>kv.Key,kv=>(object)kv.Value);
            foreach (var part in req.Files) described[part.Name] = new { filename = part.Filename, content_type = part.ContentType, bytes = part.Size };
            p.Input = J(described); p.InputMime = "application/json";
        }
        else if (IsBinary(req.ContentType)) { if (!any) return null; }
        else if (req.Body != null)
        {
            var b = Parse(req.Body);
            if (b == null) { if (!any && !req.Truncated) return null; }
            else
            {
                if (!Request(f,b,p)) return null;
                p.Input ??= Encoding.UTF8.GetString(req.Body); p.InputMime ??= "application/json";
                if (b is JsonObject o)
                {
                    p.Model = new[] { "model", "model_id", "speech_model", "modelId" }.Select(k=>S(o[k])).FirstOrDefault(x=>x!=null);
                    var parameters = new JsonObject(); foreach (var kv in o.Where(kv=>!ContentKeys.Contains(kv.Key))) parameters[kv.Key] = kv.Value?.DeepClone();
                    if (parameters.Count > 0) p.Params.TryAdd("_json",J(parameters));
                }
            }
        }
        else if (!any && !req.Truncated) return null;
        p.Model ??= m.Groups.GetValueOrDefault("model") ?? m.Query.GetValueOrDefault("model");
        if (f == "deepgram.listen") Put(p.Extra,"gen_ai.audio.language",m.Query.GetValueOrDefault("language"));
        Put(p.Extra,"gen_ai.request.voice",m.Groups.GetValueOrDefault("voice"));
        if (req.Files.FirstOrDefault() is { } file) { Put(p.Extra,"gen_ai.audio.mime_type",file.ContentType); Put(p.Extra,"traceai.audio.input_bytes",file.Size); }
        if (IsBinary(req.ContentType))
        {
            p.Extra["gen_ai.audio.mime_type"] = req.ContentType;
            Put(p.Extra,"traceai.audio.input_bytes",req.BodySize ?? req.Body?.LongLength);
        }
        return p;
    }
    private static void ParseMultipart(RequestData req,string contentType)
    {
        var boundary=Regex.Match(contentType,"boundary=\"?([^\";]+)",RegexOptions.CultureInvariant,TimeSpan.FromMilliseconds(100));
        if(!boundary.Success) return;
        var delimiter=Encoding.UTF8.GetBytes("--"+boundary.Groups[1].Value); var bytes=req.Body.AsSpan();
        req.Fields=new(); int offset=0;
        while(offset<bytes.Length)
        {
            int start=bytes[offset..].IndexOf(delimiter); if(start<0) break; start+=offset+delimiter.Length;
            int next=bytes[start..].IndexOf(delimiter); if(next<0) break; next+=start; offset=next;
            var part=bytes[start..next]; while(part.StartsWith("\r\n"u8)) part=part[2..];
            while(part.EndsWith("\r\n"u8)) part=part[..^2];
            int split=part.IndexOf("\r\n\r\n"u8); if(split<0) continue;
            if(split>8192) { req.Truncated=true; continue; }
            var headers=Encoding.UTF8.GetString(part[..split]); var payload=part[(split+4)..];
            var name=Regex.Match(headers,"name=\"([^\"]*)\""); if(!name.Success) continue;
            var filename=Regex.Match(headers,"filename=\"([^\"]*)\"");
            if(filename.Success)
            {
                var mime=Regex.Match(headers,"content-type:\\s*([^\\r\\n;]+)",RegexOptions.IgnoreCase);
                if(req.Files.Count<128) req.Files.Add(new(Bounded(name.Groups[1].Value,1024),Bounded(filename.Groups[1].Value,1024),mime.Success?Bounded(mime.Groups[1].Value.Trim(),256):null,payload.Length));
                else req.Truncated=true;
            }
            else if(req.Fields.Count<128)
            {
                if(payload.Length>65536) req.Truncated=true;
                var field=Bounded(name.Groups[1].Value,1024);
                var text=Encoding.UTF8.GetString(payload[..Math.Min(payload.Length,65536)]);
                req.Fields[field]=text;
                req.CredentialDiscoveryIncomplete|=WirePrivacy.ObserveField(field,text,payload.Length<=65536);
            }
            else req.Truncated=true;
        }
    }
    internal static string Bounded(string value,int max) => value[..Math.Min(value.Length,max)];
    private static Body Decode(ResponseData r,string format)
    {
        if (IsBinary(r.ContentType)) return new("binary");
        if (r.Body == null || r.Truncated) return new("empty");
        var bytes=BodyDecoder.Decode(r.Body,r.Headers,r.MaxBody,out var truncated,out var undecoded);
        r.Truncated|=truncated; r.UndecodedEncoding=undecoded;
        if(bytes==null) return new("empty");
        var text = Encoding.UTF8.GetString(bytes);
        bool sse=r.ContentType=="text/event-stream";
        if(sse || r.ContentType is "application/x-ndjson" or "application/jsonl" or "application/ndjson")
        {
            var events=new List<JsonNode>();
            foreach(var frame in StreamMilestones.Frames(bytes,sse))
            {
                if(StreamMilestones.Event(frame.Text,sse) is not {} item) continue;
                events.Add(item);
                if(r.FirstTextSeconds==null && StreamMilestones.HasOutput(format,item)) r.FirstTextSeconds=r.ArrivalAt(frame.End);
            }
            return new(sse?"sse":"ndjson",Events:events);
        }
        if(format=="gemini.generate_content" && text.TrimStart().StartsWith('['))
        {
            var array=new JsonArray();
            foreach(var frame in StreamMilestones.ArrayItems(bytes))
            {
                array.Add(frame.Item);
                if(r.FirstTextSeconds==null && StreamMilestones.HasOutput(format,frame.Item)) r.FirstTextSeconds=r.ArrivalAt(frame.End);
            }
            return new("json",array);
        }
        if (Parse(bytes) is { } json)
        {
            if(StreamMilestones.HasOutput(format,json)) r.FirstTextSeconds=r.ArrivalAt(StreamMilestones.JsonEnd(bytes));
            return new("json",json);
        }
        var lines = Lines(text); if (text.Split('\n').Count(x=>x.Trim().Length>0)>1 && lines.Count>0) return new("ndjson",Events:lines);
        return new("text",Text:text);
    }
    private static List<JsonNode> Lines(string s) => s.Split('\n').Select(x=>Parse(Encoding.UTF8.GetBytes(x))).Where(x=>x!=null).ToList()!;
    private static void Usage(Parsed p, JsonNode? u, params (string, string)[] map)
    {
        if(u is not JsonObject obj) return;
        foreach(var (dst,src) in map)
        {
            if(!obj.ContainsKey(src)) continue; // Cumulative snapshots merge, never add.
            if(obj[src] is JsonValue counter && counter.TryGetValue<long>(out var value) && value>=0) { p.Usage[dst]=value; p.UnknownUsage.Remove(dst); }
            else { p.Usage.Remove(dst); p.UnknownUsage.Add(dst); }
        }
    }
    private static void OpenUsage(Parsed p, JsonNode? u)
    {
        if (u is not JsonObject) return;
        Usage(p,u,("input",((JsonObject)u).ContainsKey("prompt_tokens")?"prompt_tokens":"input_tokens"),("output",((JsonObject)u).ContainsKey("completion_tokens")?"completion_tokens":"output_tokens"),("total","total_tokens"));
        void Details(string primary,string alternate,params (string Destination,string Source)[] map)
        {
            var obj=(JsonObject)u;
            string key=obj.ContainsKey(primary)?primary:alternate;
            if(!obj.ContainsKey(key)) return;
            if(obj[key] is JsonObject details) Usage(p,details,map);
            else foreach(var (destination,_) in map) { p.Usage.Remove(destination); p.UnknownUsage.Add(destination); }
        }
        Details("prompt_tokens_details","input_tokens_details",("cache_read","cached_tokens"),("input_audio","audio_tokens"));
        Details("completion_tokens_details","output_tokens_details",("reasoning","reasoning_tokens"),("output_audio","audio_tokens"));
    }
    private static void AnthropicUsage(Parsed p, JsonNode? u)
    {
        p.UsageAlgebra="cache-inclusive";
        Usage(p,u,("input","input_tokens"),("output","output_tokens"),("total","total_tokens"),("cache_read","cache_read_input_tokens"),("cache_write","cache_creation_input_tokens"));
    }
    private static void NormalizeUsage(Parsed p)
    {
        void Inclusive(string side,params string[] additions)
        {
            if(!p.Usage.TryGetValue(side,out var total)) return;
            if(additions.Any(p.UnknownUsage.Contains)) { p.Usage.Remove(side); return; }
            try { foreach(string name in additions) total=checked(total+p.Usage.GetValueOrDefault(name)); p.Usage[side]=total; }
            catch(OverflowException) { p.Usage.Remove(side); }
        }
        if(p.UsageAlgebra=="cache-inclusive") Inclusive("input","cache_read","cache_write");
        else if(p.UsageAlgebra=="gemini") { Inclusive("input","tool_input"); Inclusive("output","reasoning"); }
        if(p.Usage.TryGetValue("input",out var input)&&p.Usage.TryGetValue("output",out var output))
        {
            try
            {
                long total=checked(input+output);
                if(p.Usage.TryGetValue("total",out var raw)&&raw!=total) p.Extra["gen_ai.usage.provider_total_tokens"]=raw;
                p.Usage["total"]=total;
            }
            catch(OverflowException) { p.Usage.Remove("total"); }
        }
    }
    private static void Transcript(Parsed p, JsonNode? text)
    { if (S(text) is { } t) { p.Extra["gen_ai.audio.transcript"] = t; p.Output = t; p.OutputMime = "text/plain"; } }
    private static void Dimension(Parsed p, JsonNode? vector) { if (vector is JsonArray a) p.Extra["gen_ai.embeddings.dimension.count"] = a.Count; }
    private static void Response(string f, Body b, Parsed p, WireMatch m)
    {
        var d = b.Json;
        switch (f)
        {
            case "openai.chat": case "openai.completions":
                if (b.Kind == "json" && d is JsonObject)
                {
                    p.Id = S(d["id"]); p.ResponseModel = S(d["model"]);
                    foreach (var c in A(d["choices"]))
                    {
                        if (G(c,"message") is JsonObject msg) p.Out.Add(OpenMessage(msg));
                        else if (S(G(c,"text")) is { } t) p.Out.Add(new("assistant",t));
                        if (S(G(c,"finish_reason")) is { } reason) p.Finish.Add(reason);
                    }
                    OpenUsage(p,d["usage"]);
                }
                else if (b.Streamed) OpenStream(b,p); break;
            case "openai.responses":
                if (b.Kind == "json" && d is JsonObject) ResponsesOutput(p,d);
                else if (b.Streamed)
                {
                    JsonNode? final = null; var texts = new List<string>();
                    foreach (var e in b.Payloads)
                    {
                        var type = S(G(e,"type"));
                        // Every response envelope is a cumulative, possibly partial
                        // snapshot. Merge raw counters/details before normalizing once.
                        OpenUsage(p,G(e,"response","usage"));
                        if (type is "response.completed" or "response.incomplete" or "response.failed") final = G(e,"response");
                        else if (type == "response.created") { p.Id = S(G(e,"response","id")); p.ResponseModel = S(G(e,"response","model")); }
                        else if (type == "response.output_text.delta" && S(G(e,"delta")) is { } t) texts.Add(t);
                    }
                    if (final != null) ResponsesOutput(p,final); else if (texts.Count>0) p.Out.Add(new("assistant",string.Concat(texts)));
                } break;
            case "openai.embeddings":
                p.ResponseModel = S(G(d,"model")); Dimension(p,G(d,"data","0","embedding")); OpenUsage(p,G(d,"usage")); break;
            case "openai.audio_transcription":
                if (b.Kind == "text") { Transcript(p,JsonValue.Create(b.Text)); break; }
                if (b.Kind == "sse") d = b.Payloads.LastOrDefault(e=>S(G(e,"type"))=="transcript.text.done") ?? new JsonObject { ["text"] = string.Concat(b.Payloads.Where(e=>S(G(e,"type"))=="transcript.text.delta").Select(e=>S(G(e,"delta")))) };
                Transcript(p,G(d,"text")); Put(p.Extra,"gen_ai.audio.language",S(G(d,"language"))); Put(p.Extra,"gen_ai.audio.duration_secs",Number(G(d,"duration")));
                if (S(G(d,"usage","type")) == "duration") { if (!p.Extra.ContainsKey("gen_ai.audio.duration_secs")) Put(p.Extra,"gen_ai.audio.duration_secs",Number(G(d,"usage","seconds"))); }
                else OpenUsage(p,G(d,"usage")); break;
            case "openai.audio_speech": case "deepgram.speak": case "elevenlabs.tts": case "cartesia.tts":
                if (S(G(d,"audio_base64")) is { } encoded) p.Extra["traceai.audio.output_bytes"] = Base64Size(encoded);
                else if (b.Kind == "sse") p.Extra["traceai.audio.output_bytes"] = b.Payloads.Where(e=>(S(G(e,"type"))??"chunk")=="chunk").Sum(e=>Base64Size(S(G(e,"data"))??"")); break;
            case "openai.images":
                if (d is JsonObject) { var copy = d.DeepClone(); foreach (var item in A(copy["data"])) if (item is JsonObject && item["b64_json"]!=null) item["b64_json"]="<base64 omitted>"; p.Output=J(copy); p.OutputMime="application/json"; OpenUsage(p,d["usage"]); } break;
            case "openai.moderations":
                if (d is JsonObject) { p.Id=S(d["id"]); p.ResponseModel=S(d["model"]); p.Extra["traceai.moderation.flagged"]=A(d["results"]).Any(r=>True(G(r,"flagged"))); p.Output=J(d); p.OutputMime="application/json"; } break;
            case "rerank":
                if (d is JsonObject) { p.Id=S(d["id"]); p.ResponseModel=S(d["model"]); p.Output=J(A(d["results"]??d["data"]).Select(r=> new { index=G(r,"index"), relevance_score=G(r,"relevance_score") })); p.OutputMime="application/json"; Usage(p,d["usage"],("total","total_tokens")); } break;
            case "anthropic.messages":
                if (b.Kind=="json" && G(d,"content")!=null)
                { p.Id=S(G(d,"id")); p.ResponseModel=S(G(d,"model")); p.Out=[AnthropicMessage(S(G(d,"role"))??"assistant",G(d,"content"))]; if(S(G(d,"stop_reason")) is {} stop) p.Finish=[stop]; AnthropicUsage(p,G(d,"usage")); }
                else if(b.Streamed) AnthropicStream(b,p); break;
            case "gemini.generate_content":
                p.UsageAlgebra="gemini";
                var items=b.Kind=="json" ? d is JsonArray ? A(d) : d==null ? [] : new[]{d} : b.Payloads;
                var message=new Message("assistant"); var texts2=new List<string>();
                foreach(var item in items)
                {
                    p.ResponseModel=S(G(item,"modelVersion"))??p.ResponseModel; p.Id=S(G(item,"responseId"))??p.Id;
                    if(G(item,"candidates","0","content") is {} content) { var output=GeminiMessage(content); if(output.Content!=null) texts2.Add(output.Content); message.Calls.AddRange(output.Calls); }
                    var reasons=A(G(item,"candidates")).Select(c=>S(G(c,"finishReason"))).OfType<string>().ToList(); if(reasons.Count>0) p.Finish=reasons;
                    Usage(p,G(item,"usageMetadata"),("input","promptTokenCount"),("output","candidatesTokenCount"),("total","totalTokenCount"),("cache_read","cachedContentTokenCount"),("reasoning","thoughtsTokenCount"),("tool_input","toolUsePromptTokenCount"));
                }
                message.Content=texts2.Count>0?string.Concat(texts2):null; if(message.Content!=null||message.Calls.Count>0) p.Out=[message]; break;
            case "gemini.embed_content": Dimension(p,G(d,"embedding","values")??G(d,"embeddings","0","values")); break;
            case "cohere.chat":
                if(b.Streamed) { CohereStream(b,p); break; }
                if(d is not JsonObject) break;
                p.Id=S(d["id"])??S(d["generation_id"]);
                if(d["message"] is JsonObject co) p.Out=[OpenMessage(co)]; else if(S(d["text"]) is {} ct) p.Out=[new("assistant",ct)];
                if(S(d["finish_reason"]) is {} cr) p.Finish=[cr];
                Usage(p,G(d,"usage","tokens")??G(d,"meta","billed_units"),("input","input_tokens"),("output","output_tokens")); break;
            case "cohere.embed": Dimension(p,G(d,"embeddings","float","0")??G(d,"embeddings","0")); Usage(p,G(d,"meta","billed_units"),("input","input_tokens")); break;
            case "bedrock.converse":
                p.UsageAlgebra="cache-inclusive";
                if(G(d,"output","message") is {} bm) p.Out=[BedrockMessage(bm)]; if(S(G(d,"stopReason")) is {} br) p.Finish=[br];
                Usage(p,G(d,"usage"),("input","inputTokens"),("output","outputTokens"),("total","totalTokens"),("cache_read","cacheReadInputTokens"),("cache_write","cacheWriteInputTokens")); break;
            case "bedrock.invoke":
                if(G(d,"content")!=null||b.Streamed) Response("anthropic.messages",b,p,m); else if(b.Kind=="json") { p.Output=J(d); p.OutputMime="application/json"; } break;
            case "ollama.chat": case "ollama.generate":
                var ot=new List<string>();
                foreach(var item in b.Kind=="json" && d!=null ? new[]{d} : b.Payloads)
                {
                    p.ResponseModel=S(G(item,"model"))??p.ResponseModel;
                    if(S(G(item,"message","content")) is {} t) ot.Add(t); if(S(G(item,"response")) is {} t2) ot.Add(t2);
                    if(True(G(item,"done"))) { if(S(G(item,"done_reason")) is {} r) p.Finish=[r]; Usage(p,item,("input","prompt_eval_count"),("output","eval_count")); }
                }
                if(ot.Count>0) p.Out=[new("assistant",string.Concat(ot))]; break;
            case "ollama.embed": Dimension(p,G(d,"embeddings","0")??G(d,"embedding")); Usage(p,d,("input","prompt_eval_count")); break;
            case "deepgram.listen":
                p.Id=S(G(d,"metadata","request_id")); Put(p.Extra,"gen_ai.audio.duration_secs",Number(G(d,"metadata","duration"))); Transcript(p,G(d,"results","channels","0","alternatives","0","transcript")); break;
            case "elevenlabs.stt": Transcript(p,G(d,"text")); Put(p.Extra,"gen_ai.audio.language",S(G(d,"language_code"))); p.Id=S(G(d,"transcription_id")); break;
            case "assemblyai.transcript":
                if(d is not JsonObject) break;
                var status=S(d["status"]); if(S(m.Endpoint["only_when"])=="completed" && status is not ("completed" or "error")) { p.Skip=true; break; }
                p.Id=S(d["id"]); p.ResponseModel=S(d["speech_model"]); if(status!=null) p.Finish=[status]; if(status=="completed") Transcript(p,d["text"]);
                Put(p.Extra,"gen_ai.audio.duration_secs",Number(d["audio_duration"])); Put(p.Extra,"gen_ai.audio.language",S(d["language_code"])); break;
        }
    }
    private static void CohereStream(Body body,Parsed parsed)
    {
        var message=new Message("assistant"); var text=new StringBuilder(); var calls=new SortedDictionary<int,Call>();
        foreach(var item in body.Payloads)
        {
            var type=S(G(item,"type"))??S(G(item,"event_type"));
            if(type is "message-start" or "stream-start")
            { parsed.Id=S(G(item,"id"))??S(G(item,"generation_id"))??parsed.Id; message.Role=S(G(item,"delta","message","role"))??message.Role; }
            if(type=="content-delta") text.Append(S(G(item,"delta","message","content","text")));
            else if(type=="text-generation") text.Append(S(G(item,"text")));
            else if(type is "tool-call-start" or "tool-call-delta")
            {
                int index=(int)(Int(G(item,"index"))??0); var delta=G(item,"delta","message","tool_calls");
                if(delta is JsonArray) delta=G(delta,"0");
                if(!calls.TryGetValue(index,out var call)) calls[index]=call=new(null,null,null);
                call.Id=S(G(delta,"id"))??call.Id; call.Name=S(G(delta,"function","name"))??call.Name;
                call.Arguments=(call.Arguments??"")+(S(G(delta,"function","arguments"))??"");
            }
            if(type is "message-end" or "stream-end")
            {
                var final=G(item,"delta")??G(item,"response")??item;
                if((S(G(final,"finish_reason"))??S(G(item,"finish_reason"))) is {} reason) parsed.Finish=[reason];
                Usage(parsed,G(final,"usage","tokens")??G(final,"meta","billed_units"),("input","input_tokens"),("output","output_tokens"));
            }
        }
        message.Content=text.Length==0?null:text.ToString(); message.Calls=calls.Values.ToList();
        if(message.Content!=null||message.Calls.Count>0) parsed.Out=[message];
    }
    private static long Base64Size(string value)
    {
        // Count decoded size without allocating an audio byte array.
        int count=0, padding=0;
        foreach(char c in value) { if(char.IsAsciiLetterOrDigit(c)||c is '+' or '/') count++; else if(c=='=') padding++; }
        int remainder=count%4;
        if(remainder==1 || remainder==2&&padding<2 || remainder==3&&padding<1) return 0;
        return (long)count*6/8;
    }
    private static void ResponsesOutput(Parsed p,JsonNode d)
    {
        p.Id=S(G(d,"id"))??p.Id; p.ResponseModel=S(G(d,"model"))??p.ResponseModel;
        var msg=new Message("assistant"); var texts=new List<string>();
        foreach(var item in A(G(d,"output")))
        {
            if(S(G(item,"type"))=="message") texts.AddRange(A(G(item,"content")).Where(c=>S(G(c,"type"))=="output_text").Select(c=>S(G(c,"text"))).OfType<string>());
            else if(S(G(item,"type"))=="function_call") msg.Calls.Add(new(S(G(item,"call_id")),S(G(item,"name")),Args(G(item,"arguments"))));
        }
        msg.Content=texts.Count>0?string.Concat(texts):null; if(msg.Content!=null||msg.Calls.Count>0) p.Out=[msg];
        if(S(G(d,"status")) is {} status) p.Finish=[status]; OpenUsage(p,G(d,"usage"));
    }
    private static void OpenStream(Body b,Parsed p)
    {
        var messages=new SortedDictionary<int,Message>(); var calls=new Dictionary<int,SortedDictionary<int,Call>>(); var finish=new SortedDictionary<int,string>();
        foreach(var chunk in b.Payloads)
        {
            p.Id??=S(G(chunk,"id")); p.ResponseModel??=S(G(chunk,"model")); OpenUsage(p,G(chunk,"usage"));
            foreach(var choice in A(G(chunk,"choices")))
            {
                int i=(int)(Int(G(choice,"index"))??0); var delta=G(choice,"delta");
                if(S(G(delta,"role"))!=null||S(G(delta,"content"))!=null||S(G(choice,"text"))!=null||A(G(delta,"tool_calls")).Any())
                {
                    if(!messages.TryGetValue(i,out var msg)) messages[i]=msg=new("assistant");
                    msg.Role=S(G(delta,"role"))??msg.Role; msg.Content=(msg.Content??"")+(S(G(delta,"content"))??"")+(S(G(choice,"text"))??"");
                    foreach(var call in A(G(delta,"tool_calls")))
                    {
                        if(!calls.TryGetValue(i,out var slots)) calls[i]=slots=new(); int k=(int)(Int(G(call,"index"))??0);
                        if(!slots.TryGetValue(k,out var slot)) slots[k]=slot=new(null,null,null);
                        slot.Id??=S(G(call,"id")); slot.Name??=S(G(call,"function","name")); slot.Arguments=(slot.Arguments??"")+(S(G(call,"function","arguments"))??"");
                    }
                }
                if(S(G(choice,"finish_reason")) is {} reason) finish[i]=reason;
            }
        }
        foreach(var (i,msg) in messages) { if(msg.Content=="") msg.Content=null; if(calls.TryGetValue(i,out var slots)) msg.Calls=slots.Values.ToList(); p.Out.Add(msg); } p.Finish=finish.Values.ToList();
    }
    private static void AnthropicStream(Body b,Parsed p)
    {
        var blocks=new SortedDictionary<int,(JsonNode Block,StringBuilder Parts)>();
        foreach(var e in b.Payloads)
        {
            var kind=S(G(e,"type")); int i=(int)(Int(G(e,"index"))??blocks.Count);
            if(kind=="message_start") { p.Id=S(G(e,"message","id")); p.ResponseModel=S(G(e,"message","model")); AnthropicUsage(p,G(e,"message","usage")); }
            else if(kind=="content_block_start") { var block=G(e,"content_block")??new JsonObject(); blocks[i]=(block,new(S(G(block,"text"))??"")); }
            else if(kind=="content_block_delta")
            {
                i=(int)(Int(G(e,"index"))??0); if(!blocks.TryGetValue(i,out var block)) blocks[i]=block=(new JsonObject{["type"]="text"},new());
                block.Parts.Append(S(G(e,"delta","text"))??S(G(e,"delta","partial_json"))??"");
            }
            else if(kind=="message_delta") { if(S(G(e,"delta","stop_reason")) is {} reason) p.Finish=[reason]; AnthropicUsage(p,G(e,"usage")); }
        }
        var m=new Message("assistant"); var texts=new List<string>();
        foreach(var (block,parts) in blocks.Values)
            if(S(G(block,"type"))=="text") texts.Add(parts.ToString());
            else if(S(G(block,"type"))=="tool_use") m.Calls.Add(new(S(G(block,"id")),S(G(block,"name")),parts.Length>0?parts.ToString():Args(G(block,"input"))));
        m.Content=texts.Count>0?string.Concat(texts):null; if(m.Content!=null||m.Calls.Count>0) p.Out=[m];
    }
    private static void Messages(Dictionary<string,object> attrs,string prefix,List<Message> messages)
    {
        for(int i=0;i<messages.Count;i++)
        {
            var m=messages[i]; var key=$"{prefix}.{i}.message";
            Put(attrs,key+".role",m.Role); Put(attrs,key+".content",m.Content); Put(attrs,key+".tool_call_id",m.ToolId);
            for(int k=0;k<m.Calls.Count;k++) { var c=m.Calls[k]; var ck=$"{key}.tool_calls.{k}.tool_call"; Put(attrs,ck+".id",c.Id); Put(attrs,ck+".function.name",c.Name); Put(attrs,ck+".function.arguments",c.Arguments); }
        }
    }
    internal static SpanResult? Build(WireMatch m,RequestData req,ResponseData resp)
    {
        try { return BuildInternal(m,req,resp); } catch { return null; }
    }
    private static SpanResult? BuildInternal(WireMatch m,RequestData req,ResponseData resp)
    {
        var p=Check(m,req); if(p==null) return null;
        var body=Decode(resp,S(m.Endpoint["format"])!); bool ok=resp.Error==null&&(resp.Status??0)<400;
        if(S(m.Endpoint["only_when"])=="completed" && resp.Error==null && S(G(body.Json,"status")) is not ("completed" or "error")) return null;
        if((resp.Status??0)<400) { try { Response(S(m.Endpoint["format"])!,body,p,m); } catch { /* Malformed provider responses must not escape. */ } if(p.Skip && resp.Error==null) return null; }
        var operation=S(m.Endpoint["operation"])!;
        var a=new Dictionary<string,object> { ["gen_ai.span.kind"]=S(m.Endpoint["span_kind"])!,["gen_ai.operation.name"]=operation,["gen_ai.provider.name"]=m.Provider,["traceai.capture.layer"]="wire",["traceai.wire.endpoint"]=S(m.Endpoint["id"])!,["server.address"]=m.Url.Host,["server.port"]=m.Url.Port,["http.request.method"]=req.Method.ToUpperInvariant() };
        Put(a,"http.response.status_code",resp.Status); Put(a,"gen_ai.request.model",p.Model); Put(a,"gen_ai.response.model",p.ResponseModel); Put(a,"gen_ai.response.id",p.Id); Put(a,"gen_ai.response.finish_reasons",p.Finish.ToArray());
        foreach(var (k,v) in p.Params) Put(a,k=="_json"?"gen_ai.request.parameters":"gen_ai.request."+k,v);
        // Streaming is a property of the matched endpoint, not just its framing:
        // Bedrock binary event streams and JSON responses still carry stream timing.
        var path=m.Url.AbsolutePath;
        bool streamed=body.Streamed || path.EndsWith(":streamGenerateContent",StringComparison.Ordinal) ||
            path.EndsWith(":streamRawPredict",StringComparison.Ordinal) || path.EndsWith("/converse-stream",StringComparison.Ordinal) ||
            path.EndsWith("/invoke-with-response-stream",StringComparison.Ordinal);
        if(streamed)
        {
            a["gen_ai.request.stream"]=true;
            if(resp.FirstChunkSeconds is {} chunk) a["gen_ai.response.time_to_first_chunk"]=Math.Round(chunk,6);
            if(resp.FirstTextSeconds is {} text) a["gen_ai.response.time_to_first_text"]=Math.Round(text,6);
        }
        NormalizeUsage(p);
        foreach(var (key,name) in new[]{("input","input_tokens"),("output","output_tokens"),("total","total_tokens"),("cache_read","cache_read_tokens"),("cache_write","cache_write_tokens"),("reasoning","output_tokens.reasoning"),("input_audio","input_tokens.audio"),("output_audio","output_tokens.audio")}) if(p.Usage.TryGetValue(key,out var value)) a["gen_ai.usage."+name]=value;
        Messages(a,"gen_ai.input.messages",p.In); Messages(a,"gen_ai.output.messages",p.Out);
        for(int i=0;i<p.Tools.Count;i++) a[$"gen_ai.tool.definitions.{i}.tool.json_schema"]=J(p.Tools[i]);
        if(p.Input!=null) { a["input.value"]=p.Input; a["input.mime_type"]=p.InputMime??"application/json"; }
        if(p.Output==null&&p.Out.FirstOrDefault() is {} output)
        { if(!string.IsNullOrEmpty(output.Content)) { p.Output=output.Content; p.OutputMime="text/plain"; } else if(output.Calls.Count>0) { p.Output=J(output.Calls.Select(c=>new { id=c.Id,name=c.Name,arguments=c.Arguments })); p.OutputMime="application/json"; } }
        if(p.Output!=null) { a["output.value"]=p.Output; a["output.mime_type"]=p.OutputMime??"text/plain"; }
        foreach(var (k,v) in p.Extra) Put(a,k,v);
        if(body.Kind=="binary"&&ok && (resp.ContentType.StartsWith("audio/",StringComparison.Ordinal)||operation=="text_to_speech"))
        { a.TryAdd("gen_ai.audio.mime_type",resp.ContentType); a["traceai.audio.output_bytes"]=resp.Size; }
        if(req.Truncated||resp.Truncated) a["traceai.wire.truncated"]=true;
        if(resp.Incomplete || streamed&&resp.ArrivalTrackingLost) a["traceai.wire.incomplete"]=true;
        Put(a,"traceai.wire.undecoded_encoding",resp.UndecodedEncoding??req.UndecodedEncoding);
        string? description=null;
        if(!ok)
        {
            a["error.type"]=resp.Error?.GetType().Name??resp.Status?.ToString()??"unknown";
            var d=body.Json is JsonArray ? G(body.Json,"0") : body.Json;
            description=(resp.Error==null?null:WirePrivacy.ErrorMessage(resp.Error))??S(G(d,"error","message"))??S(G(d,"error"))??S(G(d,"message"))??S(G(d,"detail"))??S(G(d,"err_msg"))??S(G(d,"detail","message"))??(body.Text is {} t?t[..Math.Min(t.Length,500)]:null)??$"HTTP {resp.Status}";
        }
        KnownSecrets.Register(WirePrivacy.RequestSecrets(req));
        string[]? secrets;
        try { secrets=KnownSecrets.Snapshot(); }
        catch { secrets=null; }
        if(req.CredentialDiscoveryIncomplete || secrets==null)
        {
            long dropped=a.Keys.Count(AttributeProjection.ContentKey);
            foreach(var key in a.Keys.Where(key=>!AttributeProjection.Protected(key)).ToArray()) a.Remove(key);
            a["traceai.wire.incomplete"]=true;
            if(dropped>0) a[AttributeProjection.DroppedContent]=dropped;
            description=null;
        }
        foreach(var (key,value) in a.ToArray()) a[key]=secrets==null?
            (value is bool or int or long or double?value:WirePrivacy.Redacted):WirePrivacy.Scrub(value,secrets,path:key);
        var model=p.Model??p.ResponseModel;
        return new(secrets==null?"traceai.redacted":WirePrivacy.ScrubString(model!=null?$"{operation} {model}":operation,secrets),a,ok,description==null?null:WirePrivacy.ScrubString(description,secrets));
    }
}
