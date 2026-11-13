using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using Xunit;

[assembly: CollectionBehavior(DisableTestParallelization = true)]
namespace TraceAI.AutoInstrumentation.Tests;

public class StartupHookTests
{
    private const string JsonResponse="{\"id\":\"local-id\",\"model\":\"local-test\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello back\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    private const string StreamResponse="data: {\"id\":\"s1\",\"model\":\"local-test\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"héllo\"}}]}\r\n\r\ndata: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\" back\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n";
    public static IEnumerable<object[]> HookCases =>
        from framework in new[] { "net8.0","net10.0" }
        from mode in new[] { "json","stream","buffered","sync","dedup","missing","disabled","sampled_out","wire_disabled","mask","binary","truncated","dead_collector","idle","transaction" }
        select new object[] { mode,framework };
    [Theory]
    [MemberData(nameof(HookCases))]
    public async Task RealStartupHookPreservesApplicationAndExports(string mode,string framework)
    {
        var bytes=mode=="binary"?Enumerable.Range(0,9000).Select(i=>(byte)(i%256)).ToArray():Encoding.UTF8.GetBytes(mode=="stream"?StreamResponse:JsonResponse);
        await using var server=new MockServer(bytes,mode=="stream"?"text/event-stream":mode=="binary"?"audio/mpeg":"application/json");
        var environment=new Dictionary<string,string?> { ["FI_BASE_URL"]=server.Url,["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_PROJECT_NAME"]="hook-test",["FI_CONFIG_FILE"]=Path.Combine(WireFixtureTests.Repo,"csharp","no-such-config.toml") };
        if(mode=="missing") environment["FI_SECRET_KEY"]=null;
        if(mode=="disabled") environment["OTEL_SDK_DISABLED"]="true";
        if(mode=="sampled_out") environment["FI_SAMPLE_RATE"]="0";
        if(mode=="wire_disabled") environment["FI_DISABLED_INSTRUMENTATIONS"]="wire";
        if(mode=="mask") { environment["FI_HIDE_INPUTS"]="true"; environment["FI_HIDE_OUTPUTS"]="true"; }
        if(mode=="truncated") environment["FI_WIRE_MAX_BODY_BYTES"]="100";
        if(mode=="dead_collector") environment["FI_BASE_URL"]="http://127.0.0.1:1";
        var result=await RunApp(server.Url+"v1/chat/completions",mode,environment,framework);
        Assert.Equal(0,result.ExitCode);
        Assert.Equal(mode=="idle"?[]:bytes,Convert.FromBase64String(result.Output.Trim()));
        var spans=server.Exports.SelectMany(Otlp.Spans).ToArray();
        var wire=spans.Where(s=>s.Attributes.GetValueOrDefault("traceai.capture.layer")?.ToString()=="wire").ToArray();
        if(mode is "missing" or "disabled" or "sampled_out" or "wire_disabled" or "dead_collector" or "idle")
        {
            Assert.Empty(wire);
            if(mode=="idle") Assert.Empty(server.Exports);
            if(mode=="missing") Assert.Equal(1,result.Error.Split("FI_API_KEY and FI_SECRET_KEY are required").Length-1);
            else if(mode=="dead_collector") Assert.Equal(1,result.Error.Split("Collector unavailable").Length-1);
            else Assert.Equal("",result.Error);
            return;
        }
        if(mode=="dedup") { Assert.Empty(wire); Assert.Single(spans,s=>s.Name=="chat library"); return; }
        var span=Assert.Single(wire);
        Assert.Equal("chat local-test",span.Name); Assert.Equal(3,span.Kind); Assert.Equal("LLM",span.Attributes["gen_ai.span.kind"]);
        Assert.Equal("openai_compatible",span.Attributes["gen_ai.provider.name"]); Assert.Equal("200",span.Attributes["http.response.status_code"].ToString());
        Assert.Equal("observe",span.Resource["project_type"]); Assert.Equal("hook-test",span.Resource["project_name"]);
        Assert.Equal("DEFAULT_PROJECT_VERSION_NAME",span.Resource["project_version_name"]);
        Assert.True(Guid.TryParse(span.Resource["project_version_id"].ToString(),out _));
        Assert.Equal("[]",span.Resource["eval_tags"]); Assert.Equal("{}",span.Resource["metadata"]); Assert.Equal("fi",span.Resource["semantic_convention"]);
        Assert.True(span.End>=span.Start); Assert.True(span.Start>0);
        if(mode=="transaction")
        {
            var serverSpan=Assert.Single(spans,s=>s.Kind==2);
            Assert.Equal(serverSpan.Id,span.ParentId);
            Assert.Equal("",serverSpan.ParentId);
        }
        if(mode=="binary") { Assert.Equal("9000",span.Attributes["traceai.audio.output_bytes"].ToString()); Assert.False(span.Attributes.ContainsKey("output.value")); }
        else if(mode=="truncated") Assert.Equal(true,span.Attributes["traceai.wire.truncated"]);
        else if(mode=="mask") { Assert.Equal("__REDACTED__",span.Attributes["input.value"]); Assert.Equal("__REDACTED__",span.Attributes["gen_ai.input.messages.0.message.content"]); Assert.Equal("__REDACTED__",span.Attributes["output.value"]); }
        else
        {
            Assert.Equal(mode=="stream"?"héllo back":"hello back",span.Attributes["output.value"]);
            Assert.Equal("hello",span.Attributes["gen_ai.input.messages.0.message.content"]);
            if(mode=="stream") { Assert.Equal(true,span.Attributes["gen_ai.request.stream"]); Assert.True(span.Attributes.ContainsKey("gen_ai.response.time_to_first_chunk")); }
            else Assert.Equal("5",span.Attributes["gen_ai.usage.total_tokens"].ToString());
        }
        Assert.Equal("",result.Error);
    }
    [Theory]
    [InlineData("net8.0")]
    [InlineData("net10.0")]
    public async Task NativeOpenAiSdkIsEnabledByStartupHook(string framework)
    {
        await using var server=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var result=await RunApp(server.Url+"v1/chat/completions","native_openai",new Dictionary<string,string?> {
            ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url },framework);
        Assert.Equal(0,result.ExitCode); Assert.Equal("",result.Error);
        Assert.Equal("hello back",Encoding.UTF8.GetString(Convert.FromBase64String(result.Output.Trim())));
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        Assert.Equal("LLM",span.Attributes["gen_ai.span.kind"]); Assert.Equal("library",span.Attributes["traceai.capture.layer"]);
        Assert.Equal("local-test",span.Attributes["gen_ai.request.model"]); Assert.Equal("5",span.Attributes["gen_ai.usage.total_tokens"].ToString());
    }
    [Theory]
    [InlineData("net8.0",8)]
    [InlineData("net10.0",10)]
    public async Task HookIsolatesOlderHostTelemetryAndSharesDiagnosticSource(string framework,int runtime)
    {
        await using var server=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var result=await RunApp(server.Url+"v1/chat/completions","dependency_conflict",new Dictionary<string,string?> {
            ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url },framework);
        Assert.Equal(0,result.ExitCode); Assert.Equal("",result.Error);
        using var document=System.Text.Json.JsonDocument.Parse(result.Output);
        var root=document.RootElement;
        Assert.Equal(runtime,root.GetProperty("runtime").GetInt32());
        Assert.Equal(JsonResponse,Encoding.UTF8.GetString(Convert.FromBase64String(root.GetProperty("body").GetString()!)));
        Assert.StartsWith("1.11.",root.GetProperty("hostVersion").GetString());
        Assert.StartsWith("1.15.",root.GetProperty("agentVersion").GetString());
        Assert.Equal("Default",root.GetProperty("hostContext").GetString());
        Assert.Equal("traceai-auto",root.GetProperty("agentContext").GetString());
        Assert.Equal(1,root.GetProperty("diagnosticSourceCount").GetInt32());
        Assert.Equal("Default",root.GetProperty("diagnosticSourceContext").GetString());
        Assert.StartsWith(runtime==8?"9.":"10.",root.GetProperty("diagnosticSourceVersion").GetString());
        Assert.Equal(2,root.GetProperty("hostSpans").GetInt32());
        // Exactly one span proves the host's separate suppression scope was honored.
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        Assert.Equal("wire",span.Attributes["traceai.capture.layer"]);
        Assert.Equal(root.GetProperty("parent").GetString(),span.ParentId);
    }
    [Theory]
    [InlineData("net8.0",8)]
    [InlineData("net10.0",10)]
    public async Task StockConsoleAppNeedsNoTelemetryDependenciesOrRuntimeUpgrade(string framework,int runtime)
    {
        await using var server=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var result=await RunApp(server.Url+"v1/chat/completions","json",new Dictionary<string,string?> {
            ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url },framework,stock:true);
        Assert.True(result.ExitCode==0,result.Error); Assert.True(result.Error=="",result.Error);
        using var document=System.Text.Json.JsonDocument.Parse(result.Output); var root=document.RootElement;
        Assert.Equal(runtime,root.GetProperty("runtime").GetInt32());
        Assert.Equal(JsonResponse,Encoding.UTF8.GetString(Convert.FromBase64String(root.GetProperty("body").GetString()!)));
        Assert.Equal(1,root.GetProperty("diagnosticSourceCount").GetInt32());
        Assert.StartsWith(runtime+".",root.GetProperty("diagnosticSourceVersion").GetString());
        Assert.Equal("Default",root.GetProperty("diagnosticSourceContext").GetString());
        var span=Assert.Single(server.Exports.SelectMany(Otlp.Spans));
        Assert.Equal("wire",span.Attributes["traceai.capture.layer"]); Assert.Equal("hello back",span.Attributes["output.value"]);
    }
    internal sealed record ChildResult(int ExitCode,string Output,string Error);
    internal static async Task<ChildResult> RunApp(string url,string mode,Dictionary<string,string?> environment,string framework="net10.0",bool stock=false)
    {
        using var process=StartApp(url,mode,environment,framework,stock);
        var stdout=process.StandardOutput.ReadToEndAsync(); var stderr=process.StandardError.ReadToEndAsync();
        try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30)); }
        catch { process.Kill(true); throw; }
        return new(process.ExitCode,await stdout,await stderr);
    }
    internal static Process StartApp(string url,string mode,Dictionary<string,string?> environment,string framework="net10.0",bool stock=false)
    {
        var csharp=Path.Combine(WireFixtureTests.Repo,"csharp");
        var configuration=AppContext.BaseDirectory.Contains("/Release/")?"Release":"Debug";
        var host=Environment.GetEnvironmentVariable("DOTNET_HOST_PATH")??Path.Combine(csharp,".dotnet","dotnet");
        var start=new ProcessStartInfo(host) { RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,UseShellExecute=false,WorkingDirectory=csharp };
        var app=Path.Combine(csharp,"tests","TraceAI.AutoInstrumentation.TestApp");
        start.ArgumentList.Add(stock?Path.Combine(app,"Stock","bin",configuration,framework,"TraceAI.AutoInstrumentation.StockApp.dll"):
            Path.Combine(app,"bin",configuration,framework,"TraceAI.AutoInstrumentation.TestApp.dll"));
        start.ArgumentList.Add(url); start.ArgumentList.Add(mode);
        foreach(var key in start.Environment.Keys.Where(k=>k.StartsWith("FI_")||k.StartsWith("OTEL_")||k=="DOTNET_STARTUP_HOOKS").ToArray()) start.Environment.Remove(key);
        start.Environment["DOTNET_ROOT"]=Path.GetDirectoryName(host);
        start.Environment["DOTNET_STARTUP_HOOKS"]=Path.Combine(csharp,"src","TraceAI.AutoInstrumentation","bin",configuration,framework,"TraceAI.AutoInstrumentation.dll");
        foreach(var pair in environment) if(pair.Value==null) start.Environment.Remove(pair.Key); else start.Environment[pair.Key]=pair.Value;
        return Process.Start(start)!;
    }
}

internal sealed class MockServer : IAsyncDisposable
{
    private readonly HttpListener _listener=new();
    private readonly CancellationTokenSource _stop=new();
    private readonly Task _run;
    internal readonly ConcurrentQueue<byte[]> Exports=new();
    internal readonly ConcurrentQueue<Dictionary<string,string>> ExportHeaders=new();
    internal readonly ConcurrentQueue<Dictionary<string,string>> RequestHeaders=new();
    internal readonly ConcurrentQueue<byte[]> RequestBodies=new();
    internal bool UseContentLength { get; set; }
    internal Func<Stream,Task>? ResponseWriter;
    internal readonly Dictionary<string,string> ResponseHeaders=new();
    internal readonly string Url;
    private readonly byte[] _body;
    private readonly string _contentType;
    internal int StatusCode { get; set; } = 200;
    internal MockServer(byte[] body,string contentType)
    {
        var port=new TcpListener(IPAddress.Loopback,0); port.Start(); int number=((IPEndPoint)port.LocalEndpoint).Port; port.Stop();
        Url=$"http://127.0.0.1:{number}/"; _body=body; _contentType=contentType;
        _listener.Prefixes.Add(Url); _listener.Start(); _run=Serve();
    }
    private async Task Serve()
    {
        try
        {
            while(!_stop.IsCancellationRequested)
            {
                var context=await _listener.GetContextAsync().WaitAsync(_stop.Token);
                using var input=new MemoryStream(); await context.Request.InputStream.CopyToAsync(input);
                if(context.Request.Url!.AbsolutePath=="/tracer/v1/traces")
                {
                    Exports.Enqueue(input.ToArray());
                    ExportHeaders.Enqueue(context.Request.Headers.AllKeys.Where(k=>k!=null).ToDictionary(k=>k!,k=>context.Request.Headers[k!]!,StringComparer.OrdinalIgnoreCase));
                    context.Response.ContentType="application/x-protobuf"; context.Response.StatusCode=200; context.Response.ContentLength64=0;
                }
                else
                {
                    RequestHeaders.Enqueue(context.Request.Headers.AllKeys.Where(k=>k!=null).ToDictionary(k=>k!,k=>context.Request.Headers[k!]!,StringComparer.OrdinalIgnoreCase));
                    RequestBodies.Enqueue(input.ToArray());
                    context.Response.ContentType=_contentType; context.Response.StatusCode=StatusCode;
                    if(UseContentLength) context.Response.ContentLength64=_body.Length; else context.Response.SendChunked=true;
                    foreach(var header in ResponseHeaders) context.Response.Headers[header.Key]=header.Value;
                    if(ResponseWriter!=null) await ResponseWriter(context.Response.OutputStream);
                    else for(int offset=0;offset<_body.Length;offset+=17)
                    { await context.Response.OutputStream.WriteAsync(_body.AsMemory(offset,Math.Min(17,_body.Length-offset))); await context.Response.OutputStream.FlushAsync(); }
                }
                context.Response.Close();
            }
        }
        catch(Exception ex) when(ex is OperationCanceledException or HttpListenerException or ObjectDisposedException) { }
    }
    public async ValueTask DisposeAsync() { _stop.Cancel(); _listener.Close(); await _run; _stop.Dispose(); }
}

// Minimal OTLP protobuf reader keeps test apps dependency-free and verifies the real bytes sent to the collector.
internal static class Otlp
{
    private sealed record Field(int Number,int Wire,byte[] Bytes,ulong Value);
    internal sealed record Span(string Name,int Kind,ulong Start,ulong End,Dictionary<string,object> Attributes,Dictionary<string,object> Resource,string Id,string ParentId)
    { internal ulong DroppedAttributes { get; init; } }
    private static ulong Varint(byte[] bytes,ref int i)
    { ulong value=0; int shift=0; while(i<bytes.Length) { byte b=bytes[i++]; value|=(ulong)(b&127)<<shift; if(b<128)return value; shift+=7; if(shift>63)throw new InvalidDataException(); } throw new EndOfStreamException(); }
    private static List<Field> Fields(byte[] bytes)
    {
        var result=new List<Field>(); int i=0;
        while(i<bytes.Length)
        {
            var tag=Varint(bytes,ref i); int wire=(int)(tag&7),n=(int)(tag>>3); ulong v=0; byte[] data=[];
            if(wire==0) v=Varint(bytes,ref i);
            else if(wire==1) { v=BitConverter.ToUInt64(bytes,i); i+=8; }
            else if(wire==2) { int length=checked((int)Varint(bytes,ref i)); data=bytes.AsSpan(i,length).ToArray(); i+=length; }
            else if(wire==5) i+=4; else throw new InvalidDataException();
            result.Add(new(n,wire,data,v));
        }
        return result;
    }
    private static string String(byte[] bytes)=>Encoding.UTF8.GetString(bytes);
    private static object Value(byte[] bytes)
    {
        var f=Fields(bytes).First(); return f.Number switch { 1=>String(f.Bytes),2=>f.Value!=0,3=>(long)f.Value,4=>BitConverter.Int64BitsToDouble((long)f.Value),5=>Fields(f.Bytes).Select(x=>Value(x.Bytes)).ToArray(),_=>"" };
    }
    private static Dictionary<string,object> Attributes(IEnumerable<Field> fields) => fields.ToDictionary(f=>String(Fields(f.Bytes).First(x=>x.Number==1).Bytes),f=>Value(Fields(f.Bytes).First(x=>x.Number==2).Bytes));
    internal static IEnumerable<string> Extras(byte[] bytes,int field)
    {
        foreach(var resource in Fields(bytes).Where(f=>f.Number==1))
        foreach(var scope in Fields(resource.Bytes).Where(f=>f.Number==2))
        foreach(var span in Fields(scope.Bytes).Where(f=>f.Number==2))
        foreach(var item in Fields(span.Bytes).Where(f=>f.Number==field))
        {
            var parts=Fields(item.Bytes);
            object decoded=field switch {
                11=>new { Time=parts.First(p=>p.Number==1).Value,Name=String(parts.First(p=>p.Number==2).Bytes),Tags=Attributes(parts.Where(p=>p.Number==3)) },
                13=>new { Trace=Convert.ToHexString(parts.First(p=>p.Number==1).Bytes),Id=Convert.ToHexString(parts.First(p=>p.Number==2).Bytes),Tags=Attributes(parts.Where(p=>p.Number==4)) },
                _=>new { Description=String(parts.FirstOrDefault(p=>p.Number==2)?.Bytes??[]),Code=parts.FirstOrDefault(p=>p.Number==3)?.Value??0 }
            };
            yield return System.Text.Json.JsonSerializer.Serialize(decoded);
        }
    }
    internal static IEnumerable<Span> Spans(byte[] bytes)
    {
        foreach(var resource in Fields(bytes).Where(f=>f.Number==1))
        {
            var rf=Fields(resource.Bytes);
            var attrs=Attributes(Fields(rf.First(f=>f.Number==1).Bytes).Where(f=>f.Number==1));
            foreach(var scope in rf.Where(f=>f.Number==2)) foreach(var span in Fields(scope.Bytes).Where(f=>f.Number==2))
            {
                var sf=Fields(span.Bytes); yield return new(String(sf.First(f=>f.Number==5).Bytes),(int)sf.First(f=>f.Number==6).Value,
                    sf.First(f=>f.Number==7).Value,sf.First(f=>f.Number==8).Value,Attributes(sf.Where(f=>f.Number==9)),attrs,
                    Convert.ToHexString(sf.First(f=>f.Number==2).Bytes),Convert.ToHexString(sf.FirstOrDefault(f=>f.Number==4)?.Bytes??[]))
                    { DroppedAttributes=sf.FirstOrDefault(f=>f.Number==10)?.Value??0 };
            }
        }
    }
}
