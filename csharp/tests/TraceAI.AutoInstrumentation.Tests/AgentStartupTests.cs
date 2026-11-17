using System.Collections;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>Startup behaviour that must hold on every supported runtime: the private
/// runtime members the agent depends on resolve, a .NET 8 application keeps running
/// with any published hook payload, and credential registry limits are reported.</summary>
public class AgentStartupTests : InitIsolation
{
    private const string JsonResponse="{\"id\":\"local-id\",\"model\":\"local-test\",\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"hello back\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}";
    private static Dictionary<string,string?> Keys(MockServer server)=>new() {
        ["FI_API_KEY"]="test-key",["FI_SECRET_KEY"]="test-secret",["FI_BASE_URL"]=server.Url };
    private static AutoConfig Config()=>new(new AutoOptions { ApiKey="startup-api-key",SecretKey="startup-secret-key",Exporter=new MemoryExporter(),
        ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml") });
    internal static void ClearWarnings()=>((IDictionary)typeof(TraceAIAuto).GetField("Warnings",BindingFlags.Static|BindingFlags.NonPublic)!.GetValue(null)!).Clear();
    [Theory]
    [InlineData("net8.0",8)] [InlineData("net10.0",10)]
    public async Task EveryReflectedRuntimeMemberResolvesOnEachSupportedRuntime(string framework,int runtime)
    {
        await using var server=new MockServer([],"application/json");
        var child=await StartupHookTests.RunApp(server.Url,"host_runtime_layout",Keys(server),framework);
        Assert.True(child.ExitCode==0,child.Error); Assert.Equal("",child.Error);
        using var document=JsonDocument.Parse(child.Output); var root=document.RootElement;
        Assert.Equal(runtime,root.GetProperty("runtime").GetInt32());
        Assert.True(root.GetProperty("loaded").GetBoolean(),"the hook's runtime assembly was not loaded");
        Assert.True(root.GetProperty("layout").GetBoolean(),"the hook has no runtime layout check");
        Assert.Empty(root.GetProperty("missing").EnumerateArray().Select(item=>item.GetString()));
    }
    [Theory]
    [InlineData("published payload")] [InlineData("missing payload")]
    public async Task DotNet8ApplicationKeepsRunningWithAnyPublishedHookOrAMissingPayload(string hook)
    {
        await using var server=new MockServer(Encoding.UTF8.GetBytes(JsonResponse),"application/json");
        var directory=Directory.CreateTempSubdirectory("traceai-hook-");
        try
        {
            string path=StartupHookTests.HookPath();
            if(hook=="missing payload")
            {
                // Only the bootstrap assembly, without the runtime it loads.
                path=Path.Combine(directory.FullName,"TraceAI.AutoInstrumentation.dll");
                File.Copy(StartupHookTests.HookPath(),path);
            }
            var environment=Keys(server); environment["DOTNET_STARTUP_HOOKS"]=path;
            var child=await StartupHookTests.RunApp(server.Url+"v1/chat/completions","json",environment,"net8.0");
            Assert.True(child.ExitCode==0,child.Error);
            Assert.Equal(JsonResponse,Encoding.UTF8.GetString(Convert.FromBase64String(child.Output.Trim())));
            if(hook=="missing payload") Assert.Contains("Agent could not load; application continues without tracing.",child.Error);
            else { Assert.Equal("",child.Error); Assert.Single(server.Exports.SelectMany(Otlp.Spans)); }
        }
        finally { directory.Delete(true); }
    }
    [Fact]
    public void OverlongCredentialEnvironmentValueIsSkippedWithANamedWarning()
    {
        const string name="TRACEAI_TEST_OVERLONG_TOKEN";
        string value=new('v',KnownSecrets.MaxLength+1);
        var previous=Environment.GetEnvironmentVariable(name); var error=Console.Error; var captured=new StringWriter();
        try
        {
            ClearWarnings(); Environment.SetEnvironmentVariable(name,value); Console.SetError(captured);
            var config=Config();
            KnownSecrets.Seed(config);
            Assert.NotNull(KnownSecrets.ForExport(config));
            string text=captured.ToString();
            Assert.Contains(name,text); Assert.DoesNotContain(value,text);
        }
        finally { Console.SetError(error); Environment.SetEnvironmentVariable(name,previous); }
    }
    [Fact]
    public void OverlongRegisteredSecretFailsClosedWithOneWarning()
    {
        string value=new('r',KnownSecrets.MaxLength+1);
        var error=Console.Error; var captured=new StringWriter();
        try
        {
            ClearWarnings(); Console.SetError(captured);
            TraceAIAuto.RegisterSecret(value); TraceAIAuto.RegisterSecret(value);
            Assert.Throws<InvalidDataException>(()=>KnownSecrets.Snapshot());
            string text=captured.ToString();
            Assert.Equal(1,text.Split("Credential redaction is unavailable").Length-1); Assert.DoesNotContain(value,text);
        }
        finally { Console.SetError(error); }
    }
}
