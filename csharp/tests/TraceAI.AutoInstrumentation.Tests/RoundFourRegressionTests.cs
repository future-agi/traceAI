using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class RoundFourRegressionTests : InitIsolation
{
    private static AutoOptions Options(BaseExporter<Activity>? exporter=null,TraceConfig? mask=null)=>new() {
        ApiKey="explicit-api-key",SecretKey="explicit-secret-key",Exporter=exporter,TraceConfig=mask,
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") };
    [Fact]
    public async Task FrameworkLayoutsAreRecognized()
    {
        await using var server=new MockServer("{}"u8.ToArray(),"application/json");
        using var agent=new Agent(new AutoConfig(Options(new MemoryExporter())));
        using var client=new HttpClient(); using var request=new HttpRequestMessage(HttpMethod.Post,server.Url+"v1/chat/completions") { Content=new StringContent("{\"messages\":[]}",Encoding.UTF8,"application/json") };
        using var response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead);
        var content=response.Content; var fields=content.GetType().GetFields(BindingFlags.Instance|BindingFlags.NonPublic);
        Assert.True(fields.Any(f=>f.Name=="_stream"),content.GetType().FullName+" "+string.Join(";",fields.Select(f=>f.Name+"="+f.FieldType.FullName)));
        Assert.IsType<ReadTapStream>(fields.Single(f=>f.Name=="_stream").GetValue(content));
        var links=typeof(Activity).GetField("_links",BindingFlags.Instance|BindingFlags.NonPublic)!.FieldType;
        Assert.True(links.GetConstructor(BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic,null,Type.EmptyTypes,null)!=null &&
            links.GetMethod("Add",BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic)!=null,
            links.FullName+" "+string.Join(";",links.GetConstructors(BindingFlags.Instance|BindingFlags.NonPublic|BindingFlags.Public).Select(c=>c.ToString())));
    }
}
