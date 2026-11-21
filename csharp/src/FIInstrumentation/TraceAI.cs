using System.Diagnostics;
using System.Reflection;
using FIInstrumentation.Export;
using FIInstrumentation.Types;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace FIInstrumentation;

/// <summary>
/// Static entry point for initializing FI tracing.
/// Combines Python register() and Java TraceAI.init().
/// </summary>
public static class TraceAI
{
    private static readonly string Version = typeof(TraceAI).Assembly
        .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion.Split('+')[0]
        ?? typeof(TraceAI).Assembly.GetName().Version?.ToString(3)
        ?? "unknown";
    private const string InstrumentationName = "fi-instrumentation";

    private static readonly object s_lock = new();
    private static TracerProvider? s_tracerProvider;
    private static FITracer? s_tracer;
    private static ActivitySource? s_activitySource;

    /// <summary>
    /// Primary entry point. Creates a TracerProvider, configures exporters, and returns an FITracer.
    /// </summary>
    /// <exception cref="FiKeyRefusedException">The keys may not go to the endpoint (see <see cref="FiKeyTransport.Decide"/>), or
    /// <c>OTEL_EXPORTER_OTLP_HEADERS</c> or <c>OTEL_EXPORTER_OTLP_TRACES_HEADERS</c> carries an <c>x-api-key</c> or <c>x-secret-key</c> header,
    /// or a value that contains one of the Future AGI keys. The message names the setting to change and no value; nothing is sent and no provider is created.</exception>
    public static FITracer Register(Action<RegisterOptions>? configure = null)
    {
        lock (s_lock)
        {
            if (s_tracer != null)
                return s_tracer;

            var options = new RegisterOptions();
            configure?.Invoke(options);

            return Initialize(options);
        }
    }

    /// <summary>
    /// Initialize from environment variables only (FI_BASE_URL, FI_API_KEY, etc.).
    /// </summary>
    public static FITracer RegisterFromEnvironment()
    {
        return Register(opts =>
        {
            opts.ProjectName = Settings.GetProjectName();
            opts.ProjectVersionName = Settings.GetProjectVersionName();
            opts.Endpoint = Settings.GetCollectorEndpoint();
            opts.ApiKey = Settings.GetApiKey();
            opts.SecretKey = Settings.GetSecretKey();
        });
    }

    /// <summary>
    /// Shuts down the TracerProvider, flushing any remaining spans.
    /// </summary>
    public static void Shutdown()
    {
        lock (s_lock)
        {
            s_tracerProvider?.Dispose();
            s_activitySource?.Dispose();
            s_tracerProvider = null;
            s_activitySource = null;
            s_tracer = null;
        }
    }

    /// <summary>
    /// Returns the current FITracer if registered, or null.
    /// </summary>
    public static FITracer? GetTracer()
    {
        lock (s_lock)
        {
            return s_tracer;
        }
    }

    private static FITracer Initialize(RegisterOptions options)
    {
        var projectName = options.ProjectName ?? Settings.GetProjectName();
        var traceConfig = options.TraceConfig ?? TraceConfig.Builder().Build();

        // Build resource with project attributes
        var resourceBuilder = ResourceBuilder.CreateDefault()
            .AddService(serviceName: projectName, serviceVersion: Version)
            .AddAttributes(new Dictionary<string, object>
            {
                ["project_name"] = projectName,
                ["project_type"] = options.ProjectType.ToValue(),
            });

        if (!string.IsNullOrEmpty(options.ProjectVersionName))
            resourceBuilder.AddAttributes(new Dictionary<string, object>
            {
                ["project_version_name"] = options.ProjectVersionName!,
            });

        // Build auth headers
        var headers = BuildHeaders(options);

        // The destination of the keys is decided before anything is attached to a request: loopback may be plaintext, a Future AGI host is TLS
        // only, another host in plaintext needs FI_ALLOW_INSECURE_HTTP=1 (or the option), and the client follows no redirect.
        var carriesKeys = headers.Any(h => (h.Key.Equals("X-Api-Key", StringComparison.OrdinalIgnoreCase) || h.Key.Equals("X-Secret-Key", StringComparison.OrdinalIgnoreCase)) && !string.IsNullOrEmpty(h.Value));
        var target = options.Endpoint
            ?? (options.Transport == Transport.Grpc ? Settings.GetGrpcEndpoint() : Settings.GetCollectorEndpoint());
        var decision = FiKeyTransport.Decide(options.Transport == Transport.Grpc ? target : target.TrimEnd('/') + "/tracer/v1/traces",
            options.Transport == Transport.Grpc, carriesKeys, options.AllowInsecureHttp);

        // The exporter merges OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_TRACES_HEADERS into what it sends: refuse when they carry a Future AGI
        // key header or a value that contains one of the keys this registration uses.
        FiKeyTransport.AssertNoFiKeysInAmbientHeaders(headers
            .Where(h => h.Key.Equals("X-Api-Key", StringComparison.OrdinalIgnoreCase) || h.Key.Equals("X-Secret-Key", StringComparison.OrdinalIgnoreCase))
            .Select(h => h.Value));

        // Build TracerProvider
        var builder = Sdk.CreateTracerProviderBuilder()
            .SetResourceBuilder(resourceBuilder)
            .SetSampler(SamplingConfiguration.Wrap(options.Sampler ?? SamplingConfiguration.Create(
                SamplingConfiguration.ParseRate(Environment.GetEnvironmentVariable("FI_SAMPLE_RATE")))))
            .AddSource(InstrumentationName)
            .AddProcessor(new FISpanProcessor());

        // Add exporter based on transport
        var endpoint = options.Transport == Transport.Grpc
            ? decision.Endpoint
            : target;

        if (options.Transport == Transport.Grpc)
        {
            builder.AddOtlpExporter(exporterOptions =>
            {
                exporterOptions.Endpoint = new Uri(endpoint);
                exporterOptions.Protocol = OtlpExportProtocol.Grpc;
                if (headers.Count > 0)
                    exporterOptions.Headers = string.Join(",", headers.Select(h => $"{h.Key}={h.Value}"));
                if (options.Batch)
                {
                    exporterOptions.ExportProcessorType = ExportProcessorType.Batch;
                    exporterOptions.BatchExportProcessorOptions = new BatchExportActivityProcessorOptions
                    {
                        MaxExportBatchSize = 512,
                        ScheduledDelayMilliseconds = 5000,
                    };
                }
                else
                {
                    exporterOptions.ExportProcessorType = ExportProcessorType.Simple;
                }
                // Last, so the client carries the exporter's own timeout (a custom factory replaces the default one that sets it).
                exporterOptions.HttpClientFactory = FiKeyTransport.HttpClientFactory(decision, exporterOptions.TimeoutMilliseconds);
            });
        }
        else
        {
            // HTTP — use OTLP HTTP exporter
            builder.AddOtlpExporter(exporterOptions =>
            {
                exporterOptions.Endpoint = new Uri(endpoint.TrimEnd('/') + "/tracer/v1/traces");
                exporterOptions.Protocol = OtlpExportProtocol.HttpProtobuf;
                if (headers.Count > 0)
                    exporterOptions.Headers = string.Join(",", headers.Select(h => $"{h.Key}={h.Value}"));
                if (options.Batch)
                {
                    exporterOptions.ExportProcessorType = ExportProcessorType.Batch;
                    exporterOptions.BatchExportProcessorOptions = new BatchExportActivityProcessorOptions
                    {
                        MaxExportBatchSize = 512,
                        ScheduledDelayMilliseconds = 5000,
                    };
                }
                else
                {
                    exporterOptions.ExportProcessorType = ExportProcessorType.Simple;
                }
                // Last, so the client carries the exporter's own timeout (a custom factory replaces the default one that sets it).
                exporterOptions.HttpClientFactory = FiKeyTransport.HttpClientFactory(decision, exporterOptions.TimeoutMilliseconds);
            });
        }

        // Console exporter for debugging
        if (options.EnableConsoleExporter)
            builder.AddConsoleExporter();

        s_tracerProvider = builder.Build();
        s_activitySource = new ActivitySource(InstrumentationName, Version);
        s_tracer = new FITracer(s_activitySource, traceConfig);

        if (options.Verbose)
            Console.WriteLine($"[FI] Tracer registered for project '{projectName}' -> {endpoint}");

        return s_tracer;
    }

    private static Dictionary<string, string> BuildHeaders(RegisterOptions options)
    {
        var headers = new Dictionary<string, string>();

        // From RegisterOptions
        if (options.Headers != null)
        {
            foreach (var (key, value) in options.Headers)
                headers[key] = value;
        }

        // Auth headers
        var apiKey = options.ApiKey ?? Settings.GetApiKey();
        var secretKey = options.SecretKey ?? Settings.GetSecretKey();

        if (!string.IsNullOrEmpty(apiKey))
            headers["X-Api-Key"] = apiKey;
        if (!string.IsNullOrEmpty(secretKey))
            headers["X-Secret-Key"] = secretKey;

        // Project name header
        var projectName = options.ProjectName ?? Settings.GetProjectName();
        headers["fi-project-name"] = projectName;

        return headers;
    }
}

/// <summary>
/// Configuration options for TraceAI.Register().
/// </summary>
public class RegisterOptions
{
    public string? ProjectName { get; set; }
    public ProjectType ProjectType { get; set; } = ProjectType.Experiment;
    public string? ProjectVersionName { get; set; }
    public List<EvalTag>? EvalTags { get; set; }
    public Dictionary<string, object>? Metadata { get; set; }
    public bool Batch { get; set; } = true;
    public bool SetGlobalTracerProvider { get; set; } = true;
    public Dictionary<string, string>? Headers { get; set; }
    public Transport Transport { get; set; } = Transport.Http;
    public string? Endpoint { get; set; }
    public string? ApiKey { get; set; }
    public string? SecretKey { get; set; }
    public TraceConfig? TraceConfig { get; set; }
    /// <summary>Sampler for the provider created by Register. Overrides FI_SAMPLE_RATE and
    /// OTEL_TRACES_SAMPLER. A repeated Register call leaves the existing provider unchanged.</summary>
    public Sampler? Sampler { get; set; }
    public bool Verbose { get; set; } = true;
    public bool EnableConsoleExporter { get; set; }
    /// <summary>Allows the Future AGI keys to go in plaintext to a self-hosted collector that is not a Future AGI host (FI_ALLOW_INSECURE_HTTP=1 does the same).
    /// Loopback needs no setting and a Future AGI host is always TLS.</summary>
    public bool AllowInsecureHttp { get; set; }
}
