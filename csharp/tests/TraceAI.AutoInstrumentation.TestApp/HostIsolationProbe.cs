using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Trace;

// Reports what the application itself observes, so tests can compare runs with and
// without the startup hook.
internal static class HostIsolationProbe
{
    private const string RequestJson="{\"model\":\"local-test\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}";
    internal static async Task Run(string url,string mode)
    {
        switch(mode)
        {
            case "host_sampling_off": await Sampling(url,off:true); break;
            case "host_sampling_ratio": await Sampling(url,off:false); break;
            case "host_server_routes": await ServerRoutes(url); break;
            case "host_plain_headers": await PlainHeaders(url); break;
            case "host_runtime_layout": Layout(); break;
            default: throw new ArgumentException("Unknown probe mode: "+mode);
        }
    }
    private static WebApplication Web()
    {
        var builder=WebApplication.CreateBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        builder.Logging.ClearProviders();
        return builder.Build();
    }
    private static string Address(WebApplication app)=>app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
    // An external caller with no distributed parent header, so every request is a new trace.
    private static HttpClient Caller()=>new(new SocketsHttpHandler { ActivityHeadersPropagator=null });
    private static async Task Sampling(string url,bool off)
    {
        const int requests=200;
        var exporter=new CountingExporter();
        Sampler sampler=off?new AlwaysOffSampler():new ParentBasedSampler(new TraceIdRatioBasedSampler(0.01));
        using var provider=Sdk.CreateTracerProviderBuilder().SetSampler(sampler).AddSource("Microsoft.AspNetCore","Host.Work")
            .AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        var ratio=new TraceIdRatioBasedSampler(0.01);
        var gate=new object(); int observed=0,expected=0,recorded=0;
        using var work=new ActivitySource("Host.Work");
        await using var app=Web();
        app.MapGet("/work",async context=>
        {
            var request=Activity.Current;
            bool sampled=!off&&request!=null&&
                ratio.ShouldSample(new SamplingParameters(default,request.TraceId,"work",ActivityKind.Server)).Decision==SamplingDecision.RecordAndSample;
            lock(gate)
            {
                if(request!=null) observed++;
                if(sampled) expected++;
                if(request?.Recorded==true) recorded++;
            }
            using(work.StartActivity("work")) { }
            using var client=new HttpClient();
            using var response=await client.GetAsync(url+"downstream");
            await context.Response.WriteAsync("ok");
        });
        await app.StartAsync();
        string address=Address(app);
        using(var caller=Caller()) for(int i=0;i<requests;i++) await caller.GetStringAsync(address+"/work");
        await app.StopAsync();
        provider.ForceFlush();
        Console.WriteLine(JsonSerializer.Serialize(new { requests,observed,expected,recorded,exported=exporter.Count }));
    }
    private static async Task ServerRoutes(string url)
    {
        using var nested=new ActivitySource("Microsoft.AspNetCore.TestSubsource");
        await using var app=Web();
        app.MapGet("/ai",async context=>
        {
            using(nested.StartActivity("nested framework work")) { }
            using var client=new HttpClient();
            using var response=await client.PostAsync(url,new StringContent(RequestJson,Encoding.UTF8,"application/json"));
            await context.Response.WriteAsync(await response.Content.ReadAsStringAsync());
        });
        app.MapGet("/plain",async context=>await context.Response.WriteAsync("plain"));
        await app.StartAsync();
        string address=Address(app),ai,plain;
        using(var caller=Caller()) { ai=await caller.GetStringAsync(address+"/ai"); plain=await caller.GetStringAsync(address+"/plain"); }
        await app.StopAsync();
        Console.WriteLine(JsonSerializer.Serialize(new { ai,plain }));
    }
    private static async Task PlainHeaders(string url)
    {
        // No ambient Activity: a console application or background job.
        using var client=new HttpClient();
        using(var plain=await client.GetAsync(url+"plain")) await plain.Content.ReadAsByteArrayAsync();
        using(var model=await client.PostAsync(url+"v1/chat/completions",new StringContent(RequestJson,Encoding.UTF8,"application/json")))
            await model.Content.ReadAsByteArrayAsync();
        Console.WriteLine("headers-complete");
    }
    private static void Layout()
    {
        var runtime=AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a=>a.GetName().Name=="TraceAI.AutoInstrumentation.Runtime");
        var layout=runtime?.GetType("TraceAI.AutoInstrumentation.RuntimeLayout");
        var missing=layout?.GetMethod("Missing",BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic)?.Invoke(null,null) as string[];
        Console.WriteLine(JsonSerializer.Serialize(new { runtime=Environment.Version.Major,loaded=runtime!=null,layout=missing!=null,missing=missing??Array.Empty<string>() }));
    }
    private sealed class CountingExporter : BaseExporter<Activity>
    {
        private int _count;
        internal int Count=>Volatile.Read(ref _count);
        public override ExportResult Export(in Batch<Activity> batch) { Interlocked.Add(ref _count,(int)batch.Count); return ExportResult.Success; }
    }
}
