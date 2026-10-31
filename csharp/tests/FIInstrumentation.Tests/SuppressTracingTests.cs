using System.Diagnostics;
using System.Net;
using System.Text;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Trace;

namespace FIInstrumentation.Tests;

public class SuppressTracingTests : IDisposable
{
    private readonly ActivitySource _source = new($"suppression-{Guid.NewGuid()}");
    private readonly ActivityListener _listener;
    private readonly FITracer _tracer;
    private readonly List<Activity> _completed = new();

    public SuppressTracingTests()
    {
        _tracer = new FITracer(_source, TraceConfig.Builder().Build());
        _listener = new ActivityListener
        {
            ShouldListenTo = source => source == _source,
            Sample = (ref ActivityCreationOptions<ActivityContext> _) =>
                ActivitySamplingResult.AllDataAndRecorded,
            ActivityStopped = activity => _completed.Add(activity),
        };
        ActivitySource.AddActivityListener(_listener);
    }

    public void Dispose()
    {
        _listener.Dispose();
        _source.Dispose();
    }

    [Fact]
    public void SuppressedOperations_ExecuteWithoutCreatingActivities()
    {
        _tracer.Chain("before", _ => { });
        using (new SuppressTracing())
        {
            var executed = false;
            _tracer.Trace("hidden-void", FISpanKind.Chain, span =>
            {
                executed = true;
                span.SetInput("synthetic-private-input");
                span.SetOutput("synthetic-private-output");
            });
            Assert.True(executed);
            Assert.Equal(42, _tracer.Trace("hidden-result", FISpanKind.Tool, _ => 42));
            using var manual = _tracer.StartSpan("hidden-manual", FISpanKind.Llm);
            using var active = _tracer.StartActiveSpan("hidden-active", FISpanKind.Agent);
        }
        _tracer.Chain("after", _ => { });

        Assert.Equal(new[] { "before", "after" }, _completed.Select(activity => activity.DisplayName));
    }

    [Fact]
    public void NestedScopes_RestoreOuterScopeAndSupportRepeatedDispose()
    {
        Assert.False(SuppressTracing.IsSuppressed);
        using (new SuppressTracing())
        {
            var inner = new SuppressTracing();
            inner.Dispose();
            inner.Dispose();
            Assert.True(SuppressTracing.IsSuppressed);
            Assert.True(Sdk.SuppressInstrumentation);
            _tracer.Tool("hidden-after-inner", _ => { });
        }

        Assert.False(SuppressTracing.IsSuppressed);
        Assert.False(Sdk.SuppressInstrumentation);
        Assert.Empty(_completed);
    }

    [Fact]
    public void NativeSuppression_IsRespectedAndRestored()
    {
        using (SuppressInstrumentationScope.Begin())
        {
            using (new SuppressTracing())
            {
                _tracer.Tool("hidden-fi-scope", _ => { });
            }
            Assert.True(Sdk.SuppressInstrumentation);
            _tracer.Tool("hidden-native-scope", _ => { });
        }

        Assert.False(Sdk.SuppressInstrumentation);
        Assert.Empty(_completed);
    }

    [Fact]
    public void SuppressedSpan_DoesNotChangeParentOrCollectValues()
    {
        using var parent = _tracer.StartActiveSpan("parent", FISpanKind.Chain);
        FISpan suppressed;
        using (new SuppressTracing())
        {
            using var context = ContextAttributes.UsingSession("synthetic-session");
            suppressed = _tracer.StartActiveSpan("hidden-child", FISpanKind.Tool);
            using (suppressed)
            {
                Assert.Same(parent.Activity, Activity.Current);
                suppressed.SetInput(new MustNotSerialize());
                suppressed.SetOutput(new MustNotSerialize());
                suppressed.SetInputMessages(new() { new() { ["content"] = "synthetic-input" } });
                suppressed.SetOutputMessages(new() { new() { ["content"] = "synthetic-output" } });
                suppressed.SetTool("synthetic-tool", "synthetic-description", new MustNotSerialize());
                suppressed.SetTokenCounts(1, 2, 3);
                suppressed.SetAttribute("string", "synthetic-value");
                suppressed.SetAttribute("integer", 1L);
                suppressed.SetAttribute("double", 1.0);
                suppressed.SetAttribute("boolean", true);
                suppressed.SetAttribute("array", new[] { "synthetic-value" });
                suppressed.SetError(new InvalidOperationException("synthetic-error"));
                Assert.Empty(suppressed.Activity.TagObjects);
                Assert.Empty(suppressed.Activity.Events);
                Assert.Equal(ActivityStatusCode.Unset, suppressed.Activity.Status);
                Assert.Null(suppressed.Activity.Id);
            }
            Assert.Same(parent.Activity, Activity.Current);
        }
        // Retained wrappers must not start recording when the scope ends.
        suppressed.SetInput("synthetic-late-input");
        Assert.Empty(suppressed.Activity.TagObjects);
        Assert.Empty(_completed);
    }

    [Fact]
    public async Task AsyncSuppression_FlowsAcrossAwaitAndDoesNotLeakToSibling()
    {
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var hidden = Task.Run(async () =>
        {
            using (new SuppressTracing())
            {
                entered.SetResult();
                await release.Task;
                await _tracer.TraceAsync("hidden-async-void", FISpanKind.Chain, async span =>
                {
                    await Task.Yield();
                    Assert.True(SuppressTracing.IsSuppressed);
                    span.SetInput("synthetic-async-input");
                });
                Assert.Equal(42, await _tracer.ToolAsync("hidden-async-result", async span =>
                {
                    await Task.Yield();
                    span.SetOutput("synthetic-async-output");
                    return 42;
                }));
                await Task.Run(() => _tracer.Agent("hidden-inherited-task", _ => { }));
            }
            Assert.False(SuppressTracing.IsSuppressed);
        });

        try
        {
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.False(SuppressTracing.IsSuppressed);
            _tracer.Chain("visible-sibling", _ => { });
        }
        finally
        {
            release.TrySetResult();
            await hidden.WaitAsync(TimeSpan.FromSeconds(10));
        }
        Assert.Equal("visible-sibling", Assert.Single(_completed).DisplayName);
    }

    [Fact]
    public async Task Exceptions_PropagateWithoutExportAndRestoreSuppression()
    {
        var expected = new InvalidOperationException("synthetic-private-error");
        using (new SuppressTracing())
        {
            Assert.Same(expected, Assert.Throws<InvalidOperationException>(() =>
                _tracer.Trace<int>("hidden-error", FISpanKind.Chain, _ => throw expected)));
            Assert.Same(expected, await Assert.ThrowsAsync<InvalidOperationException>(() =>
                _tracer.TraceAsync<int>("hidden-async-error", FISpanKind.Chain, async _ =>
                {
                    await Task.Yield();
                    throw expected;
                })));
        }
        Assert.False(SuppressTracing.IsSuppressed);
        Assert.Empty(_completed);
    }

    [Fact]
    public async Task ScopeDisposal_RestoresTracingWhenAnExceptionEscapes()
    {
        var expected = new InvalidOperationException("synthetic-error");
        Assert.Same(expected, await Assert.ThrowsAsync<InvalidOperationException>(async () =>
        {
            using (new SuppressTracing())
            {
                await Task.Yield();
                using (new SuppressTracing())
                {
                    throw expected;
                }
            }
        }));

        Assert.False(SuppressTracing.IsSuppressed);
        _tracer.Chain("visible-after-error", _ => { });
        Assert.Equal("visible-after-error", Assert.Single(_completed).DisplayName);
    }

    [Fact]
    public void Suppression_WorksWithoutAnyListener()
    {
        using var source = new ActivitySource($"unobserved-{Guid.NewGuid()}");
        var tracer = new FITracer(source, TraceConfig.Builder().Build());
        using (new SuppressTracing())
        {
            Assert.Equal(42, tracer.Chain("hidden-unobserved", span =>
            {
                span.SetInput("synthetic-input");
                return 42;
            }));
        }
    }

    [Fact]
    public void OtlpExporter_OnlySerializesVisibleSpans()
    {
        using var source = new ActivitySource($"suppression-otlp-{Guid.NewGuid()}");
        using var handler = new OtlpCaptureHandler();
        using var client = new HttpClient(handler);
        using var provider = Sdk.CreateTracerProviderBuilder()
            .AddSource(source.Name)
            .AddOtlpExporter(options =>
            {
                options.Endpoint = new Uri("http://localhost:4318/v1/traces");
                options.Protocol = OtlpExportProtocol.HttpProtobuf;
                options.ExportProcessorType = ExportProcessorType.Simple;
                options.HttpClientFactory = () => client;
            })
            .Build();
        var tracer = new FITracer(source, TraceConfig.Builder().Build());

        tracer.Chain("visible-before", span => span.SetInput("public-before"));
        using (new SuppressTracing())
        {
            // The same scope also suppresses native ActivitySource instrumentation via the SDK.
            using var native = source.StartActivity("hidden-native");
            Assert.Null(native);
            tracer.Chain("hidden-outer", span => span.SetInput("synthetic-private-input"));
            using (new SuppressTracing())
            {
                tracer.Tool("hidden-inner", span => span.SetOutput("synthetic-private-output"));
            }
            tracer.Chain("hidden-after-inner", _ => { });
        }
        tracer.Chain("visible-after", span => span.SetOutput("public-after"));
        Assert.True(provider.ForceFlush());

        Assert.Equal(2, handler.Payloads.Count);
        Assert.Contains("visible-before", handler.Payloads[0]);
        Assert.Contains("public-before", handler.Payloads[0]);
        Assert.Contains("visible-after", handler.Payloads[1]);
        Assert.Contains("public-after", handler.Payloads[1]);
        Assert.All(handler.Payloads, payload =>
        {
            Assert.DoesNotContain("hidden-", payload);
            Assert.DoesNotContain("synthetic-private", payload);
        });
    }

    private sealed class MustNotSerialize
    {
        public string Value => throw new InvalidOperationException("Suppressed input was serialized.");
        public override string ToString() => throw new InvalidOperationException("Suppressed input was formatted.");
    }

    // Capture the real exporter's serialized OTLP HTTP request, without network access.
    private sealed class OtlpCaptureHandler : HttpMessageHandler
    {
        public List<string> Payloads { get; } = new();

        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Assert.Equal("/v1/traces", request.RequestUri!.AbsolutePath);
            Assert.Equal("application/x-protobuf", request.Content!.Headers.ContentType!.MediaType);
            using var body = new MemoryStream();
            request.Content.CopyTo(body, null, cancellationToken);
            Payloads.Add(Encoding.UTF8.GetString(body.ToArray()));
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent([]) };
        }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(Send(request, cancellationToken));
    }
}
