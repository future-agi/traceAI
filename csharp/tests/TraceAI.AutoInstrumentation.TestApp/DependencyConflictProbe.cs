using System.Diagnostics;
using System.Reflection;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Trace;

internal static class DependencyConflictProbe
{
    internal static async Task Run(string url)
    {
        // DI/plugin scanners commonly inspect every default-context assembly. The
        // hook must remain inspectable without binding its private OTel types here.
        var hook=AssemblyLoadContext.Default.Assemblies.Single(a=>a.GetName().Name=="TraceAI.AutoInstrumentation");
        var hookTypes=hook.GetTypes();
        if(hook.GetReferencedAssemblies().Any(a=>a.Name!.StartsWith("OpenTelemetry")||a.Name=="FIInstrumentation"))
            throw new InvalidOperationException("Bootstrap leaked runtime dependencies");
        using var exporter=new HostExporter();
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource("Host.Conflict")
            .AddProcessor(new SimpleActivityExportProcessor(exporter)).Build();
        using var source=new ActivitySource("Host.Conflict");
        using var client=new HttpClient();
        async Task<byte[]> Request(bool suppressReader=false)
        {
            using var request=new HttpRequestMessage(HttpMethod.Post,url) {
                Content=new StringContent("{\"model\":\"local-test\",\"messages\":[]}",Encoding.UTF8,"application/json") };
            using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
            using var suppression=suppressReader?SuppressInstrumentationScope.Begin():default;
            var bytes=await response.Content.ReadAsByteArrayAsync();
            if(suppressReader && !Sdk.SuppressInstrumentation) throw new InvalidOperationException("Reader suppression was changed");
            return bytes;
        }
        // Private OTel must honor the host's distinct OTel suppression AsyncLocal.
        using(SuppressInstrumentationScope.Begin()) await Request();
        string parent; byte[] bytes;
        using(var activity=source.StartActivity("host-parent"))
        {
            if(activity==null) throw new InvalidOperationException("Host provider was replaced");
            parent=activity.SpanId.ToHexString().ToUpperInvariant();
            // Request accepted outside suppression, body consumed in the HOST's
            // separate scope: completion must retain the request-time decision.
            bytes=await Request(suppressReader:true);
        }
        using(var activity=source.StartActivity("after-request"))
            if(activity==null) throw new InvalidOperationException("Host provider stopped working");
        var host=typeof(Sdk).Assembly;
        var agent=AppDomain.CurrentDomain.GetAssemblies().Single(a=>a.GetName().Name==host.GetName().Name && a!=host);
        var diagnostics=AppDomain.CurrentDomain.GetAssemblies().Where(a=>a.GetName().Name=="System.Diagnostics.DiagnosticSource").ToArray();
        Console.WriteLine(JsonSerializer.Serialize(new {
            runtime=Environment.Version.Major,body=Convert.ToBase64String(bytes),parent,hostSpans=exporter.Count,
            hostVersion=host.GetCustomAttribute<AssemblyInformationalVersionAttribute>()!.InformationalVersion,
            agentVersion=agent.GetCustomAttribute<AssemblyInformationalVersionAttribute>()!.InformationalVersion,
            hostContext=AssemblyLoadContext.GetLoadContext(host)!.Name,agentContext=AssemblyLoadContext.GetLoadContext(agent)!.Name,
            diagnosticSourceCount=diagnostics.Length,diagnosticSourceVersion=diagnostics.Single().GetName().Version!.ToString(),
            diagnosticSourceContext=AssemblyLoadContext.GetLoadContext(diagnostics.Single())!.Name
        }));
    }
    private sealed class HostExporter : BaseExporter<Activity>
    {
        internal int Count;
        public override ExportResult Export(in Batch<Activity> batch) { Count+=(int)batch.Count; return ExportResult.Success; }
    }
}
