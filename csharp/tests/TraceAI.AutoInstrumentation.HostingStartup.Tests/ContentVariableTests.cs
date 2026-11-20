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

    private static string Activate(Settings settings, int times)
    {
        typeof(TraceHostingStartup).GetField("s_contentNoted", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!.SetValue(null, 0);
        var saved = Console.Error; var writer = new StringWriter(); Console.SetError(writer);
        try
        {
            for (var i = 0; i < times; i++)
            {
                var services = new ServiceCollection(); services.AddChatClient(new StubChat());
                Decorator.Apply(services, settings);
                using var provider = services.BuildServiceProvider();
                provider.GetRequiredService<IChatClient>();
            }
        }
        finally { Console.SetError(saved); }
        return writer.ToString();
    }

    [Fact]
    public void TheContentLineIsPrintedOncePerProcessAcrossActivationsAndNamesOnlyTheSetting()
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        try
        {
            Environment.SetEnvironmentVariable(Variable, null);
            var output = Activate(new Settings(Chat: true, Agent: true, Sensitive: true), 2);
            Assert.Single(output.Split('\n', StringSplitOptions.RemoveEmptyEntries));
            Assert.Contains("FI_AUTO_CAPTURE_CONTENT", output);
            Assert.DoesNotContain("OTEL_", output);
        }
        finally { Environment.SetEnvironmentVariable(Variable, previous); }
    }

    [Fact]
    public void NoContentLineIsPrintedWhenTheSwitchStaysOff()
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        try
        {
            Environment.SetEnvironmentVariable(Variable, null);
            Assert.Equal("", Activate(new Settings(Chat: true, Agent: true, Sensitive: false), 2));
            Environment.SetEnvironmentVariable(Variable, "false");
            Assert.Equal("", Activate(new Settings(Chat: true, Agent: true, Sensitive: true), 2));
        }
        finally { Environment.SetEnvironmentVariable(Variable, previous); }
    }

    [Fact]
    public void ARegistrationThatOwnershipDeclinesPrintsNoContentLine()
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        var cache = (System.Collections.Concurrent.ConcurrentDictionary<(Type, bool), System.Reflection.ConstructorInfo?>)typeof(Ownership)
            .GetField("Cache", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!.GetValue(null)!;
        var key = (typeof(DelegatingChatClient), true);
        try
        {
            Environment.SetEnvironmentVariable(Variable, null);
            cache[key] = null; // ownership cannot build its object for chat clients: the registration is left untraced
            Assert.Equal("", Activate(new Settings(Chat: true, Agent: true, Sensitive: true), 1));
        }
        finally { cache.TryRemove(key, out _); Environment.SetEnvironmentVariable(Variable, previous); }
    }

    [Fact]
    public void AKeptRegistrationStillPrintsTheContentLineOnce()
    {
        var previous = Environment.GetEnvironmentVariable(Variable);
        try
        {
            Environment.SetEnvironmentVariable(Variable, null);
            Assert.Single(Activate(new Settings(Chat: true, Agent: true, Sensitive: true), 1).Split('\n', StringSplitOptions.RemoveEmptyEntries));
        }
        finally { Environment.SetEnvironmentVariable(Variable, previous); }
    }
}
