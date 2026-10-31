using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;

static string Env(string key) => Environment.GetEnvironmentVariable(key)!;
static void Emit(object value) { Console.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush(); }
Emit(new { kind = "ready" });
if (Env("BENCH_STARTUP_ONLY") == "1") return;
using var client = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(20) };
int n = int.Parse(Env("BENCH_N")), warmup = int.Parse(Env("BENCH_WARMUP"));
foreach (bool stream in new[] { false, true }) {
    for (int i = 0; i < n + warmup; i++) {
        string id = $"{Env("BENCH_PAIR")}-{(stream ? 1 : 0)}-{i}";
        string request = JsonSerializer.Serialize(new { model = "gpt-4o-mini", messages = new[] { new { role = "user", content = id } }, stream, stream_options = new { include_usage = true } });
        using var message = new HttpRequestMessage(HttpMethod.Post, Env("BENCH_PROVIDER") + "/v1/chat/completions") {
            Content = new StringContent(request, Encoding.UTF8, "application/json"), Version = HttpVersion.Version11
        };
        long start = Stopwatch.GetTimestamp(), first = 0;
        using var response = await client.SendAsync(message, HttpCompletionOption.ResponseHeadersRead);
        response.EnsureSuccessStatusCode();
        using var input = await response.Content.ReadAsStreamAsync();
        using var result = new MemoryStream(); byte[] buffer = new byte[4096]; int count;
        while ((count = await input.ReadAsync(buffer)) > 0) {
            if (first == 0) first = Stopwatch.GetTimestamp();
            result.Write(buffer, 0, count);
        }
        long end = Stopwatch.GetTimestamp();
        Emit(new { kind = "call", id, stream, warmup = i < warmup,
            latency_us = (end-start)*1000000.0/Stopwatch.Frequency, ttfc_us = (first-start)*1000000.0/Stopwatch.Frequency,
            body_b64 = Convert.ToBase64String(result.ToArray()) });
    }
}
(await client.GetAsync(Env("BENCH_PROVIDER") + "/health")).EnsureSuccessStatusCode();
(await client.PostAsync(Env("BENCH_PROVIDER") + "/v1/chat/completions", new StringContent("{\"not_ai\":true}", Encoding.UTF8, "application/json"))).EnsureSuccessStatusCode();
Emit(new { kind = "done" });
await Task.Delay(TimeSpan.FromSeconds(double.Parse(Env("BENCH_SETTLE_SECONDS"), System.Globalization.CultureInfo.InvariantCulture)));
