using System.Diagnostics;
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
    private const string Version = "0.1.0";
    private const string InstrumentationName = "fi-instrumentation";

    private static readonly object s_lock = new();
    private static TracerProvider? s_tracerProvider;
    private static FITracer? s_tracer;
    private static ActivitySource? s_activitySource;

    /// <summary>
    /// Primary entry point. Creates a TracerProvider, configures exporters, and returns an FITracer.
    /// </summary>
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

        // Build TracerProvider
        var builder = Sdk.CreateTracerProviderBuilder()
            .SetResourceBuilder(resourceBuilder)
            .AddSource(InstrumentationName)
            .AddProcessor(new FISpanProcessor());

        // Add exporter based on transport
        var endpoint = options.Endpoint
            ?? (options.Transport == Transport.Grpc
                ? Settings.GetGrpcEndpoint()
                : Settings.GetCollectorEndpoint());

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
    public bool Verbose { get; set; } = true;
    public bool EnableConsoleExporter { get; set; }
}
