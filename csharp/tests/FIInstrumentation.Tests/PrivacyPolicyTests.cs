using System.Diagnostics;
using System.Net;
using System.Text;
using FIInstrumentation;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Trace;
using Xunit;

[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace FIInstrumentation.Tests;

public partial class PrivacyPolicyTests
{
    private const string Secret = "synthetic-sensitive-canary";

    private sealed class Capture : IDisposable
    {
        public readonly ActivitySource Source = new($"review-{Guid.NewGuid()}");
        public readonly WireHandler Handler = new();
        public readonly TracerProvider Provider;
        public readonly FITracer Tracer;
        private readonly HttpClient _client;
        public Capture(TraceConfig? config = null)
        {
            _client = new HttpClient(Handler);
            Provider = Sdk.CreateTracerProviderBuilder().AddSource(Source.Name)
                .SetSampler(new AlwaysOnSampler())
                .AddOtlpExporter(options =>
                {
                    options.Endpoint = new Uri("http://localhost:4318/v1/traces");
                    options.Protocol = OtlpExportProtocol.HttpProtobuf;
                    options.ExportProcessorType = ExportProcessorType.Simple;
                    options.HttpClientFactory = () => _client;
                }).Build();
            Tracer = new FITracer(Source, config ?? TraceConfig.Builder().Build());
        }
        public string Wire => string.Join("\n", Handler.Payloads);
        public void Dispose() { Provider.Dispose(); Source.Dispose(); _client.Dispose(); Handler.Dispose(); }
    }

    private sealed class WireHandler : HttpMessageHandler
    {
        public readonly List<string> Payloads = new();
        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken token)
        {
            using var body = new MemoryStream();
            request.Content!.CopyTo(body, null, token);
            Payloads.Add(Encoding.UTF8.GetString(body.ToArray()));
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent([]) };
        }
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token) => Task.FromResult(Send(request, token));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void TextHidingEnvironmentFlagsMustRedactMessages(bool input)
    {
        string variable = input ? "FI_HIDE_INPUT_TEXT" : "FI_HIDE_OUTPUT_TEXT";
        var previous = Environment.GetEnvironmentVariable(variable);
        Environment.SetEnvironmentVariable(variable, "true");
        try
        {
            var config = TraceConfig.Builder().Build();
            Assert.True(input ? config.HideInputText : config.HideOutputText);
            using var capture = new Capture(config);
            capture.Tracer.Llm("text-privacy", span =>
            {
                List<Dictionary<string, string>> messages = [new() { ["role"] = input ? "user" : "assistant", ["content"] = Secret }];
                if (input) span.SetInputMessages(messages); else span.SetOutputMessages(messages);
            });
            capture.Tracer.Llm("generic-text-privacy", span =>
                span.SetAttribute(input ? SemanticConventions.GenAiInputMessages : SemanticConventions.GenAiOutputMessages, Secret));
            Assert.Equal(2, capture.Handler.Payloads.Count);
            Assert.DoesNotContain(Secret, capture.Wire);
            Assert.Contains(TraceConfig.RedactedValue, capture.Wire);
        }
        finally { Environment.SetEnvironmentVariable(variable, previous); }
    }

    [Theory]
    [InlineData("input.value")]
    [InlineData("output.value")]
    [InlineData("gen_ai.input.messages")]
    [InlineData("gen_ai.output.messages")]
    [InlineData("gen_ai.embeddings.vectors")]
    [InlineData("gen_ai.request.parameters")]
    public void GenericAttributeSetterMustApplyTraceConfig(string key)
    {
        var config = TraceConfig.Builder().HideInputs().HideOutputs().HideEmbeddingVectors().HideLlmInvocationParameters().Build();
        Assert.Equal(TraceConfig.RedactedValue, config.Mask(key, Secret));
        using var capture = new Capture(config);
        capture.Tracer.Llm("attribute-privacy", span => span.SetAttribute(key, Secret));
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void ErrorHidingPolicyMustRedactExportedException(bool explicitErrorFlag)
    {
        var previous = Environment.GetEnvironmentVariable("FI_HIDE_ERROR_DETAILS");
        Environment.SetEnvironmentVariable("FI_HIDE_ERROR_DETAILS", explicitErrorFlag ? "true" : null);
        try
        {
            var config = explicitErrorFlag ? TraceConfig.Builder().Build() : TraceConfig.Builder().HideInputs().Build();
            using var capture = new Capture(config);
            var expected = new InvalidOperationException(Secret);
            Assert.Same(expected, Assert.Throws<InvalidOperationException>(() =>
                capture.Tracer.Chain("error-privacy", span => { if (!explicitErrorFlag) span.SetInput(Secret); throw expected; })));
            Assert.Single(capture.Handler.Payloads);
            Assert.DoesNotContain(Secret, capture.Wire);
        }
        finally { Environment.SetEnvironmentVariable("FI_HIDE_ERROR_DETAILS", previous); }
    }

    private sealed class PoisonValue
    {
        public int Reads;
        public string Content { get { Reads++; throw new InvalidOperationException(Secret); } }
        public override string ToString() => throw new InvalidOperationException(Secret);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void HiddenValueMustNotInvokeSerializer(bool input)
    {
        using var capture = new Capture(new TraceConfig { HideInputs = input, HideOutputs = !input });
        var value = new PoisonValue();
        capture.Tracer.Chain("lazy-privacy", span =>
        {
            if (input) span.SetInput(value); else span.SetOutput(value);
        });
        Assert.Equal(0, value.Reads);
        Assert.DoesNotContain(Secret, capture.Wire);
        Assert.Contains(TraceConfig.RedactedValue, capture.Wire);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void SerializerAndFallbackFailureMustNotEscape(bool input)
    {
        using var capture = new Capture(new TraceConfig());
        capture.Tracer.Chain("safe-serialization", span =>
        {
            if (input) span.SetInput(new PoisonValue()); else span.SetOutput(new PoisonValue());
        });
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Theory]
    [InlineData("string")]
    [InlineData("long")]
    [InlineData("double")]
    [InlineData("bool")]
    [InlineData("array")]
    public void EveryAttributeOverloadMustMaskAndPreserveUnhiddenTypes(string kind)
    {
        using var capture = new Capture(new TraceConfig { HideInputs = true });
        capture.Tracer.Chain("typed-privacy", span =>
        {
            foreach (var key in new[] { "input.value", "custom.value" })
            {
                switch (kind)
                {
                    case "string": span.SetAttribute(key, Secret); break;
                    case "long": span.SetAttribute(key, 934873L); break;
                    case "double": span.SetAttribute(key, 934873.5); break;
                    case "bool": span.SetAttribute(key, true); break;
                    case "array": span.SetAttribute(key, new[] { Secret }); break;
                }
            }
            Assert.Equal(TraceConfig.RedactedValue, span.Activity.GetTagItem("input.value"));
            var visible = span.Activity.GetTagItem("custom.value");
            switch (kind)
            {
                case "string": Assert.Equal(Secret, visible); break;
                case "long": Assert.Equal(934873L, Assert.IsType<long>(visible)); break;
                case "double": Assert.Equal(934873.5, Assert.IsType<double>(visible)); break;
                case "bool": Assert.True(Assert.IsType<bool>(visible)); break;
                case "array": Assert.Equal(new[] { Secret }, Assert.IsType<string[]>(visible)); break;
            }
        });
    }

    [Theory]
    [InlineData("inputs")]
    [InlineData("outputs")]
    [InlineData("input-messages")]
    [InlineData("output-messages")]
    [InlineData("input-text")]
    [InlineData("output-text")]
    public void ContentFlagsMustHideErrorDetailsButKeepErrorIdentity(string flag)
    {
        var config = new TraceConfig
        {
            HideInputs = flag == "inputs", HideOutputs = flag == "outputs",
            HideInputMessages = flag == "input-messages", HideOutputMessages = flag == "output-messages",
            HideInputText = flag == "input-text", HideOutputText = flag == "output-text",
        };
        using var capture = new Capture(config);
        capture.Tracer.Chain("error-identity", span =>
        {
            span.SetError(new InvalidOperationException(Secret));
            Assert.Equal(ActivityStatusCode.Error, span.Activity.Status);
            Assert.Null(span.Activity.StatusDescription);
            Assert.Equal(typeof(InvalidOperationException).FullName, span.Activity.GetTagItem("error.type"));
            Assert.Null(span.Activity.GetTagItem("error.message"));
            var tags = Assert.Single(span.Activity.Events).Tags.ToDictionary(t => t.Key, t => t.Value);
            Assert.Equal(typeof(InvalidOperationException).FullName, tags["exception.type"]);
            Assert.False(tags.ContainsKey("exception.message"));
            Assert.False(tags.ContainsKey("exception.stacktrace"));
        });
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Theory]
    [InlineData("error.message")]
    [InlineData("exception.message")]
    [InlineData("exception.stacktrace")]
    [InlineData("provider.error.message")]
    [InlineData("provider.exception.message")]
    [InlineData("provider.exception.stacktrace")]
    [InlineData("provider.error_message")]
    public void GenericErrorDetailKeysMustBeOmitted(string key)
    {
        using var capture = new Capture(new TraceConfig { HideInputs = true });
        capture.Tracer.Chain("error-key", span =>
        {
            span.SetAttribute(key, Secret);
            Assert.Null(span.Activity.GetTagItem(key));
        });
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Theory]
    [InlineData(null, true)]
    [InlineData("true", true)]
    [InlineData("1", true)]
    [InlineData("yes", true)]
    [InlineData("false", false)]
    [InlineData("0", false)]
    [InlineData("no", false)]
    [InlineData("invalid", true)]
    public void ErrorPolicyEnvironmentAndBuilderPrecedence(string? setting, bool hidden)
    {
        var previous = Environment.GetEnvironmentVariable("FI_HIDE_ERROR_DETAILS");
        Environment.SetEnvironmentVariable("FI_HIDE_ERROR_DETAILS", setting);
        try
        {
            var config = TraceConfig.Builder().HideInputs().Build();
            Assert.Equal(hidden, config.HideErrorDetails);
            Assert.True(TraceConfig.Builder().HideInputs().HideErrorDetails().Build().HideErrorDetails);
            Assert.False(TraceConfig.Builder().HideInputs().HideErrorDetails(false).Build().HideErrorDetails);
            using var capture = new Capture(config);
            capture.Tracer.Chain("error-override", span => span.SetError(new InvalidOperationException(Secret)));
            Assert.Single(capture.Handler.Payloads);
            Assert.Equal(!hidden, capture.Wire.Contains(Secret));
        }
        finally { Environment.SetEnvironmentVariable("FI_HIDE_ERROR_DETAILS", previous); }
    }

    [Fact]
    public void ErrorPolicyObjectInitializerAndNonContentDefaults()
    {
        Assert.False(new TraceConfig().HideErrorDetails);
        Assert.False(new TraceConfig { HideInputs = true, HideErrorDetails = false }.HideErrorDetails);
        Assert.True(new TraceConfig { HideErrorDetails = true }.HideErrorDetails);
        Assert.False(new TraceConfig { HideInputImages = true, HideEmbeddingVectors = true,
            HideLlmInvocationParameters = true }.HideErrorDetails);
    }

    [Fact]
    public void ErrorPolicyDropsLazyValuesWithoutEvaluatingThem()
    {
        var config = new TraceConfig { HideErrorDetails = true };
        Assert.Null(config.Mask("exception.message", () => throw new InvalidOperationException(Secret)));
        Assert.Equal("System.Exception", config.Mask("exception.type", "System.Exception"));
        Assert.Equal("System.Exception", config.Mask("error.type", "System.Exception"));
    }

    [Fact]
    public void HiddenErrorDetailsMustNotReadExceptionMessageOrStack()
    {
        using var capture = new Capture(new TraceConfig { HideErrorDetails = true });
        var original = new PoisonException();
        Assert.Same(original, Assert.Throws<PoisonException>(() =>
            capture.Tracer.Chain("poison-error", _ => { throw original; })));
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    private sealed class PoisonException : Exception
    {
        public override string Message => throw new InvalidOperationException(Secret);
        public override string StackTrace => throw new InvalidOperationException(Secret);
    }

    [Fact]
    public void InstrumentationVersionMustMatchAssemblyMetadata()
    {
        TraceAI.Shutdown();
        try
        {
            var tracer = TraceAI.Register(opts =>
            {
                opts.Endpoint = "http://localhost:4318";
                opts.ApiKey = "synthetic-key";
                opts.SecretKey = "synthetic-secret";
                opts.Verbose = false;
            });
            var metadata = System.Reflection.CustomAttributeExtensions.GetCustomAttribute<System.Reflection.AssemblyInformationalVersionAttribute>(typeof(TraceAI).Assembly)!;
            Assert.Equal(metadata.InformationalVersion.Split('+')[0], tracer.ActivitySource.Version);
        }
        finally { TraceAI.Shutdown(); }
    }
}
