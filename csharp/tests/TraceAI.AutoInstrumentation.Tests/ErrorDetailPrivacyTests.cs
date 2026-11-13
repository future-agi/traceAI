using System.Diagnostics;
using System.Text;
using System.Text.Json;
using FIInstrumentation;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Provider error text can echo the prompt. When content is hidden, or error
/// details are hidden explicitly, exports keep the error type but drop error messages,
/// stack traces and status descriptions, as the SDK's TraceConfig documents.</summary>
public class ErrorDetailPrivacyTests : InitIsolation
{
    private const string Echo="provider said PRIVATE_PROMPT_ECHO";
    private static string AbsentConfig=>Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml");
    private static AutoConfig Config(MemoryExporter exporter,string? configFile=null,TraceConfig? mask=null)=>new(new AutoOptions {
        ApiKey="error-detail-key",SecretKey="error-detail-secret",Exporter=exporter,ConfigFile=configFile??AbsentConfig,TraceConfig=mask });
    private static void FailingLibraryCall()
    {
        using var source=new ActivitySource("OpenAI.ErrorDetails");
        using var activity=source.StartActivity("chat failure",ActivityKind.Client);
        Assert.NotNull(activity);
        activity.SetTag("error.type","400"); activity.SetTag("error.message",Echo); activity.SetTag("otel.status_description",Echo);
        activity.AddEvent(new ActivityEvent("exception",tags:new ActivityTagsCollection {
            ["exception.type"]="System.Net.Http.HttpRequestException",["exception.message"]=Echo,["exception.stacktrace"]="at "+Echo }));
        activity.SetStatus(ActivityStatusCode.Error,Echo);
    }
    [Fact]
    public void HiddenContentAlsoHidesErrorDetailsOnLibrarySpans()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter,mask:new TraceConfig { HideInputs=true }));
        FailingLibraryCall();
        Assert.True(agent.Flush(3000));
        var span=Assert.Single(exporter.Spans);
        Assert.Equal(ActivityStatusCode.Error,span.Status); Assert.Null(span.StatusDescription);
        Assert.Equal("400",span.GetTagItem("error.type"));
        Assert.Null(span.GetTagItem("error.message")); Assert.Null(span.GetTagItem("otel.status_description"));
        var exception=Assert.Single(span.Events).Tags.ToDictionary(pair=>pair.Key,pair=>pair.Value);
        Assert.Equal("System.Net.Http.HttpRequestException",exception["exception.type"]);
        Assert.False(exception.ContainsKey("exception.message")); Assert.False(exception.ContainsKey("exception.stacktrace"));
    }
    [Fact]
    public void ErrorDetailsStayWhenNothingIsHidden()
    {
        var exporter=new MemoryExporter(); using var agent=new Agent(Config(exporter));
        FailingLibraryCall();
        Assert.True(agent.Flush(3000));
        var span=Assert.Single(exporter.Spans);
        Assert.Equal(Echo,span.StatusDescription); Assert.Equal(Echo,span.GetTagItem("error.message"));
        Assert.Equal(Echo,Assert.Single(span.Events).Tags.Single(pair=>pair.Key=="exception.message").Value);
    }
    [Theory]
    [InlineData("hide_inputs = true",true)]
    [InlineData("hide_output_text = true",true)]
    [InlineData("hide_error_details = true",true)]
    [InlineData("hide_inputs = true\nhide_error_details = false",false)]
    [InlineData("hide_input_images = true",false)]
    public void ErrorDetailFlagFollowsContentFlagsUnlessSetExplicitly(string settings,bool hidden)
    {
        string path=Path.Combine(Path.GetTempPath(),"traceai-error-details-"+Guid.NewGuid()+".toml");
        try
        {
            File.WriteAllText(path,settings+"\n");
            var processor=new AutoProcessor(Config(new MemoryExporter(),path));
            foreach(var key in new[] { "exception.message","exception.stacktrace","error.message" })
            {
                if(hidden) Assert.Null(processor.Mask(key,Echo));
                else Assert.Equal(Echo,processor.Mask(key,Echo));
            }
            Assert.Equal("400",processor.Mask("error.type","400"));
        }
        finally { File.Delete(path); }
    }
    public static IEnumerable<object[]> EchoCases=>from framework in new[] { "net8.0","net10.0" }
        from flag in new[] { "FI_HIDE_INPUTS","FI_HIDE_ERROR_DETAILS" } select new object[] { framework,flag };
    [Theory]
    [MemberData(nameof(EchoCases))]
    public async Task ProviderErrorEchoingThePromptIsNotExported(string framework,string flag)
    {
        byte[] body=Encoding.UTF8.GetBytes("{\"error\":{\"message\":\"Invalid request for prompt: hello PROMPT_ECHO_9876\"}}");
        await using var server=new MockServer(body,"application/json") { StatusCode=400 };
        var child=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","json",new Dictionary<string,string?> {
            ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url,[flag]="true" },framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
        Assert.Equal(body,Convert.FromBase64String(child.Output.Trim()));
        byte[] export=Assert.Single(server.Exports);
        Assert.DoesNotContain("PROMPT_ECHO_9876",Encoding.UTF8.GetString(export));
        var span=Assert.Single(Otlp.Spans(export));
        Assert.Equal("wire",span.Attributes["traceai.capture.layer"]); Assert.Equal("400",span.Attributes["error.type"].ToString());
        using var status=JsonDocument.Parse(Assert.Single(Otlp.Extras(export,15)));
        Assert.Equal(2,status.RootElement.GetProperty("Code").GetInt32());
        Assert.Equal("",status.RootElement.GetProperty("Description").GetString());
    }
}
