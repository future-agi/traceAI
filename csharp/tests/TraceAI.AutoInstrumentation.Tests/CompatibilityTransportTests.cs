using System.Diagnostics;
using System.Text;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class CompatibilityTransportTests
{
    [Fact]
    public async Task CompatibilityProtobufMatchesPatchedSdkForResourceSpanEventsLinksAndStatus()
    {
        await using var native=new MockServer([],"application/json");
        await using var compatibility=new MockServer([],"application/json");
        var privacy=new AutoProcessor(new AutoConfig(new AutoOptions { ApiKey="test-api-key",SecretKey="test-secret-key" }));
        TrackingOtlpExporter Exporter(MockServer collector,bool legacy)=>new(new OtlpExporterOptions {
            Endpoint=new Uri(collector.Url+"tracer/v1/traces"),Protocol=OtlpExportProtocol.HttpProtobuf,
            Headers="X-Api-Key=key%20with%20spaces,X-Secret-Key=secret",TimeoutMilliseconds=1000 },privacy,new ExportCompletion(),legacy);
        using var provider=Sdk.CreateTracerProviderBuilder().AddSource(TraceAIAuto.Source.Name)
            .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddService("compatibility-test").AddAttributes(new Dictionary<string,object> { ["number"]=-7L,["bool"]=true }))
            .AddProcessor(privacy).AddProcessor(new SimpleActivityExportProcessor(Exporter(native,false)))
            .AddProcessor(new SimpleActivityExportProcessor(Exporter(compatibility,true))).Build();
        var link=new ActivityLink(new ActivityContext(ActivityTraceId.CreateRandom(),ActivitySpanId.CreateRandom(),ActivityTraceFlags.Recorded,isRemote:true),
            new ActivityTagsCollection { ["link.kind"]="test" });
        using(var activity=TraceAIAuto.Source.StartActivity("equivalent",ActivityKind.Client,default(ActivityContext),new ActivityTagsCollection {
            ["traceai.capture.layer"]="wire",["negative"]=-11L,["fraction"]=0.125,["enabled"]=true,["text"]="héllo",
            ["strings"]=new[] { "one","two" },["integers"]=new long[] { -1,0,1 },["booleans"]=new[] { true,false },["doubles"]=new[] { 0.25,2.0 }
        },new[] { link }))
        {
            Assert.NotNull(activity); activity.SetStatus(ActivityStatusCode.Error,"provider error");
            activity.AddEvent(new ActivityEvent("safe event",DateTimeOffset.UtcNow,new ActivityTagsCollection { ["event.kind"]="test",["count"]=2L }));
        }
        byte[] actualBytes=Assert.Single(compatibility.Exports),expectedBytes=Assert.Single(native.Exports);
        var actual=Assert.Single(Otlp.Spans(actualBytes)); var expected=Assert.Single(Otlp.Spans(expectedBytes));
        Assert.Equal(expected.Name,actual.Name); Assert.Equal(expected.Kind,actual.Kind); Assert.Equal(expected.Start,actual.Start); Assert.Equal(expected.End,actual.End);
        Assert.Equal(expected.Id,actual.Id); Assert.Equal(expected.ParentId,actual.ParentId);
        Assert.Equal(WireCore.J(expected.Attributes),WireCore.J(actual.Attributes)); Assert.Equal(WireCore.J(expected.Resource),WireCore.J(actual.Resource));
        foreach(int field in new[] { 11,13,15 }) Assert.Equal(Otlp.Extras(expectedBytes,field),Otlp.Extras(actualBytes,field));
        var headers=Assert.Single(compatibility.ExportHeaders); Assert.Equal("key with spaces",headers["X-Api-Key"]); Assert.Equal("secret",headers["X-Secret-Key"]);
    }
    [Fact]
    public void CompatibilityEncodingHasABoundedMessageBudget()
    {
        using var activity=new Activity("large").Start(); activity.SetTag("large",new string('x',16*1024*1024+1)); activity.Stop();
        Assert.Throws<InvalidDataException>(()=>CompatibilityOtlpTransport.Encode(Resource.Empty,new[] { activity }));
    }
}
