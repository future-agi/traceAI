using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;
using TraceAI.AutoInstrumentation.HostingStartupSupport;
using Xunit;

namespace TraceAI.AutoInstrumentation.HostingStartup.Tests;

/// <summary>These tests set a process-wide environment variable, so nothing else runs beside them.</summary>
[CollectionDefinition("ProcessEnvironment", DisableParallelization = true)]
public class ProcessEnvironmentCollection { }

[Collection("ProcessEnvironment")]
public class ContentVariableTests
{
    private const string Variable = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT";

    [Theory]
    [InlineData("false")]
    [InlineData("False")]
    [InlineData("FALSE")]
    [InlineData("0")]
    public void TheStandardContentVariableSetToAnythingButTrueIsNotOverriddenToTrue(string value)
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        try
        {
            Environment.SetEnvironmentVariable(Variable, value);
            var services = new ServiceCollection(); services.AddChatClient(new StubChat());
            Decorator.Apply(services, new Settings(Chat: true, Agent: true, Sensitive: true));
            using var provider = services.BuildServiceProvider();
            var wrapper = provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>()!;
            Assert.False(wrapper.EnableSensitiveData);
        }
        finally { Environment.SetEnvironmentVariable(Variable, previous); }
    }

    [Theory]
    [InlineData(null)]
    [InlineData("true")]
    [InlineData("TRUE")]
    public void WithTheOptInAndNoContraryVariableContentIsOn(string? value)
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        try
        {
            Environment.SetEnvironmentVariable(Variable, value);
            var services = new ServiceCollection(); services.AddChatClient(new StubChat());
            Decorator.Apply(services, new Settings(Chat: true, Agent: true, Sensitive: true));
            using var provider = services.BuildServiceProvider();
            Assert.True(provider.GetRequiredService<IChatClient>().GetService<OpenTelemetryChatClient>()!.EnableSensitiveData);
        }
        finally { Environment.SetEnvironmentVariable(Variable, previous); }
    }
}
