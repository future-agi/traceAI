using System.Diagnostics;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Exporter;

namespace FIInstrumentation.Export;

/// <summary>
/// Custom HTTP exporter that sends spans as JSON POST to {baseUrl}/tracer/v1/traces.
/// Uses X-Api-Key and X-Secret-Key headers for authentication.
/// </summary>
public class HTTPSpanExporter : BaseExporter<Activity>
{
    private readonly HttpClient _httpClient;
    private readonly string _endpoint;
    private readonly bool _verbose;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = false,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    public HTTPSpanExporter(
        string baseUrl,
        Dictionary<string, string>? headers = null,
        bool verbose = true)
    {
        _endpoint = baseUrl.TrimEnd('/') + "/v1/traces";
        _verbose = verbose;
        _httpClient = new HttpClient();
        _httpClient.DefaultRequestHeaders.Accept.Add(
            new MediaTypeWithQualityHeaderValue("application/json"));

        if (headers != null)
        {
            foreach (var (key, value) in headers)
            {
                _httpClient.DefaultRequestHeaders.TryAddWithoutValidation(key, value);
            }
        }
    }

    public override ExportResult Export(in Batch<Activity> batch)
    {
        try
        {
            var spans = new List<Dictionary<string, object?>>();
            foreach (var activity in batch)
            {
                spans.Add(SerializeActivity(activity));
            }

            if (spans.Count == 0)
                return ExportResult.Success;

            var payload = new Dictionary<string, object>
            {
                ["resourceSpans"] = new[]
                {
                    new Dictionary<string, object>
                    {
                        ["scopeSpans"] = new[]
                        {
                            new Dictionary<string, object>
                            {
                                ["spans"] = spans,
                            },
                        },
                    },
                },
            };

            var json = JsonSerializer.Serialize(payload, JsonOptions);
            var content = new StringContent(json, Encoding.UTF8, "application/json");

            var response = _httpClient.PostAsync(_endpoint, content).GetAwaiter().GetResult();

            if (_verbose && !response.IsSuccessStatusCode)
            {
                var body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                Console.Error.WriteLine(
                    $"[FI] Export failed: {response.StatusCode} - {body}");
            }

            return response.IsSuccessStatusCode ? ExportResult.Success : ExportResult.Failure;
        }
        catch (Exception ex)
        {
            if (_verbose)
                Console.Error.WriteLine($"[FI] Export error: {ex.Message}");
            return ExportResult.Failure;
        }
    }

    private static Dictionary<string, object?> SerializeActivity(Activity activity)
    {
        var attributes = new List<Dictionary<string, object?>>();
        foreach (var tag in activity.Tags)
        {
            if (tag.Value != null)
            {
                attributes.Add(new Dictionary<string, object?>
                {
                    ["key"] = tag.Key,
                    ["value"] = new Dictionary<string, object?> { ["stringValue"] = tag.Value },
                });
            }
        }

        var events = new List<Dictionary<string, object?>>();
        foreach (var evt in activity.Events)
        {
            var eventAttrs = new List<Dictionary<string, object?>>();
            foreach (var tag in evt.Tags)
            {
                eventAttrs.Add(new Dictionary<string, object?>
                {
                    ["key"] = tag.Key,
                    ["value"] = new Dictionary<string, object?> { ["stringValue"] = tag.Value?.ToString() },
                });
            }
            events.Add(new Dictionary<string, object?>
            {
                ["name"] = evt.Name,
                ["timeUnixNano"] = evt.Timestamp.ToUnixTimeNanoseconds(),
                ["attributes"] = eventAttrs,
            });
        }

        return new Dictionary<string, object?>
        {
            ["traceId"] = activity.TraceId.ToString(),
            ["spanId"] = activity.SpanId.ToString(),
            ["parentSpanId"] = activity.ParentSpanId == default
                ? null : activity.ParentSpanId.ToString(),
            ["name"] = activity.DisplayName,
            ["kind"] = (int)activity.Kind,
            ["startTimeUnixNano"] = activity.StartTimeUtc.ToUnixTimeNanoseconds(),
            ["endTimeUnixNano"] = (activity.StartTimeUtc + activity.Duration).ToUnixTimeNanoseconds(),
            ["attributes"] = attributes,
            ["events"] = events,
            ["status"] = new Dictionary<string, object?>
            {
                ["code"] = (int)activity.Status,
                ["message"] = activity.StatusDescription,
            },
        };
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            _httpClient.Dispose();
        base.Dispose(disposing);
    }
}

internal static class DateTimeOffsetExtensions
{
    public static long ToUnixTimeNanoseconds(this DateTimeOffset dto) =>
        dto.ToUnixTimeMilliseconds() * 1_000_000 + (dto.Ticks % TimeSpan.TicksPerMillisecond) * 100;

    public static long ToUnixTimeNanoseconds(this DateTime dt) =>
        new DateTimeOffset(dt, TimeSpan.Zero).ToUnixTimeNanoseconds();
}
