using System.Text;
using System.Text.Json.Nodes;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class WireFixtureTests
{
    internal static string Repo
    {
        get
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir != null && !Directory.Exists(Path.Combine(dir.FullName, "spec", "auto-instrumentation"))) dir = dir.Parent;
            return dir?.FullName ?? throw new DirectoryNotFoundException("Shared fixture directory not found");
        }
    }
    private static IEnumerable<string> AllFixtures() => Directory.GetFiles(Path.Combine(Repo,"spec","auto-instrumentation","fixtures"),"*.json",SearchOption.AllDirectories).Order();
    // The spec explicitly marks these synthetic SSE carriers as decoded-payload
    // recognition, not proof of AWS binary event-stream decoding.
    private static bool DecodedBedrock(string path)=>Path.GetFileName(path).Contains("milestone-a10-bedrock-",StringComparison.Ordinal);
    public static IEnumerable<object[]> Fixtures()=>AllFixtures().Where(path=>!DecodedBedrock(path)).Select(path=>new object[] { path });
    public static IEnumerable<object[]> DecodedFixtures()=>AllFixtures().Where(DecodedBedrock).Select(path=>new object[] { path });
    [Theory,MemberData(nameof(DecodedFixtures))]
    public void DecodedBedrockEventRecognizer(string path)
    {
        var fixture=JsonNode.Parse(File.ReadAllText(path))!;
        var response=fixture["response"]!;
        var events=(JsonArray)response["sse"]!;
        var arrivals=(JsonArray)response["arrival_offsets_ms"]!;
        double? first=null;
        for(int i=0;i<events.Count;i++)
            if(first==null&&StreamMilestones.HasOutput("bedrock.converse",events[i]!)) first=arrivals[i]!.GetValue<double>()/1000;
        var expected=fixture["expect"]!["attributes"]!;
        Assert.Equal(expected["gen_ai.response.time_to_first_chunk"]!.GetValue<double>(),arrivals[0]!.GetValue<double>()/1000);
        Assert.Equal(expected["gen_ai.response.time_to_first_text"]?.GetValue<double>(),first);
    }
    [Fact] public void CatalogIsByteIdentical() => Assert.Equal(File.ReadAllBytes(Path.Combine(Repo,"spec","auto-instrumentation","wire-protocols.json")), WireCore.CatalogBytes);
    [Fact] public void AllGoldenCasesArePresent()
    {
        Assert.True(Fixtures().Count()>=86);
        Assert.Contains(Fixtures(),row=>Path.GetFileName((string)row[0])=="55-openai-responses-cumulative-partial-details.json");
    }
    internal static Dictionary<string,string> Headers(JsonNode? node) => node is JsonObject o ? o.ToDictionary(x=>x.Key,x=>x.Value!.GetValue<string>(),StringComparer.OrdinalIgnoreCase) : new(StringComparer.OrdinalIgnoreCase);
    [Theory, MemberData(nameof(Fixtures))]
    public void GoldenFixture(string path)
    {
        var fixture = JsonNode.Parse(File.ReadAllText(path))!; var rq = fixture["request"]!; var rs = fixture["response"]!; var expect = fixture["expect"]!;
        var req = new RequestData { Method = rq["method"]!.GetValue<string>(), Url = rq["url"]!.GetValue<string>(), Headers = Headers(rq["headers"]) };
        if (rq["json"] is {} json) { req.Body = Encoding.UTF8.GetBytes(WireCore.J(json)); req.Headers.TryAdd("content-type","application/json"); }
        else if(rq["text"] is {} text) req.Body = Encoding.UTF8.GetBytes(text.GetValue<string>());
        else if(rq["binary"] is {} binary) { req.BodySize=binary["size"]!.GetValue<int>(); req.Headers.TryAdd("content-type",binary["content_type"]!.GetValue<string>()); }
        else if(rq["multipart"] is {} multi)
        {
            const string boundary="traceai-fixture-boundary";
            using var bytes=new MemoryStream();
            void Write(string s) => bytes.Write(Encoding.UTF8.GetBytes(s));
            foreach(var (name,value) in Headers(multi["fields"])) Write($"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n");
            foreach(var f in WireCore.A(multi["files"]))
            {
                Write($"--{boundary}\r\nContent-Disposition: form-data; name=\"{f["name"]}\"; filename=\"{f["filename"]}\"\r\nContent-Type: {f["content_type"]}\r\n\r\n");
                bytes.Write(new byte[f["size"]!.GetValue<int>()]); Write("\r\n");
            }
            Write($"--{boundary}--\r\n"); req.Headers["content-type"]=$"multipart/form-data; boundary={boundary}"; req.Body=bytes.ToArray();
        }
        var resp = new ResponseData { Status=rs["status"]!.GetValue<int>(),Headers=Headers(rs["headers"]) };
        var chunks=new List<byte[]>();
        if(rs["json"] is {} rj) resp.Body=Encoding.UTF8.GetBytes(WireCore.J(rj));
        else if(rs["text"] is {} rt) resp.Body=Encoding.UTF8.GetBytes(rt.GetValue<string>());
        else if(rs["binary"] is {} rb) resp.Size=rb["size"]!.GetValue<int>();
        else if(rs["sse"] is JsonArray sse)
        {
            foreach(var entry in sse)
            {
                var data=new StringBuilder();
                if(entry is JsonObject obj) { var clone=(JsonObject)obj.DeepClone(); if(clone["event"] is {} ev) { data.Append("event: ").Append(ev.GetValue<string>()).Append('\n'); clone.Remove("event"); } data.Append("data: ").Append(WireCore.J(clone)).Append("\n\n"); }
                else data.Append("data: ").Append(entry!.GetValue<string>()).Append("\n\n");
                chunks.Add(Encoding.UTF8.GetBytes(data.ToString()));
            }
            resp.Body=chunks.SelectMany(chunk=>chunk).ToArray();
        }
        else if(rs["ndjson"] is JsonArray nd)
        {
            chunks.AddRange(nd.Select(item=>Encoding.UTF8.GetBytes(WireCore.J(item)+"\n")));
            resp.Body=chunks.SelectMany(chunk=>chunk).ToArray();
        }
        if(rs["chunk_sizes"] is JsonArray sizes && resp.Body!=null)
        {
            chunks.Clear(); int offset=0;
            foreach(var size in sizes)
            {
                int count=size!.GetValue<int>(); chunks.Add(resp.Body.AsSpan(offset,count).ToArray()); offset+=count;
            }
            Assert.Equal(resp.Body.Length,offset);
        }
        if(chunks.Count==0 && resp.Body!=null) chunks.Add(resp.Body);
        if(rq["unavailable"]?.GetValue<bool>()==true) { req.Body=null; req.BodyUnavailable=true; }
        if(rs["error_after_chunks"] is {} after)
        {
            int count=after.GetValue<int>();
            chunks=chunks.Take(count).ToList();
            if(resp.Body!=null) resp.Body=chunks.SelectMany(chunk=>chunk).ToArray();
            resp.Error=new TimeoutException("fixture transport timeout");
        }
        if(rs["gzip"]?.GetValue<bool>()==true && resp.Body!=null)
        {
            resp.Body=AdversarialTests.Compress(resp.Body,"gzip"); resp.Headers["content-encoding"]="gzip";
        }
        if(rs["arrival_offsets_ms"] is JsonArray arrivals)
        {
            for(int i=0;i<chunks.Count;i++) resp.ObserveChunk(chunks[i].Length,arrivals[i]!.GetValue<double>()/1000);
        }
        if(resp.Body!=null) resp.Size=resp.Body.Length;
        var match=WireCore.Match(req.Method,req.Url); var result=match==null?null:WireCore.Build(match,req,resp);
        if(!expect["captured"]!.GetValue<bool>()) { Assert.Null(result); return; }
        Assert.NotNull(result); Assert.Equal(expect["name"]!.GetValue<string>(),result.Name); Assert.Equal(expect["status"]!.GetValue<string>()=="OK",result.Ok);
        if(expect["status_description"] is {} description) Assert.Equal(description.GetValue<string>(),result.Description);
        foreach(var pair in (JsonObject?)expect["attributes"]??new JsonObject())
        {
            Assert.True(result.Attributes.TryGetValue(pair.Key,out var actual),$"{fixture["name"]}: missing {pair.Key}");
            var got=JsonNode.Parse(WireCore.J(actual));
            Assert.True(JsonNode.DeepEquals(pair.Value,got),$"{fixture["name"]}: {pair.Key} got {got}, expected {pair.Value}");
        }
        foreach(var pair in (JsonObject?)expect["not_contains"]??new JsonObject())
            if(result.Attributes.TryGetValue(pair.Key,out var value)) foreach(var forbidden in WireCore.A(pair.Value)) Assert.DoesNotContain(forbidden.GetValue<string>(),WireCore.J(value));
        foreach(var key in WireCore.A(expect["present"])) Assert.True(result.Attributes.ContainsKey(key.GetValue<string>()));
        foreach(var key in WireCore.A(expect["absent"])) Assert.False(result.Attributes.ContainsKey(key.GetValue<string>()));
    }
}
