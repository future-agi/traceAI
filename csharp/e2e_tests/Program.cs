using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using FIInstrumentation;
using FIInstrumentation.Context;

// ─── Configuration ──────────────────────────────────────────────────
// Required: GOOGLE_API_KEY
// Optional: FI_BASE_URL (default: http://localhost:8000)
//           FI_API_KEY, FI_SECRET_KEY

var googleApiKey = Environment.GetEnvironmentVariable("GOOGLE_API_KEY");
if (string.IsNullOrEmpty(googleApiKey))
{
    Console.Error.WriteLine("ERROR: GOOGLE_API_KEY environment variable is required.");
    Console.Error.WriteLine("Set it with: export GOOGLE_API_KEY=your-key");
    return 1;
}

var baseUrl = Environment.GetEnvironmentVariable("FI_BASE_URL") ?? "http://localhost:8000";

Console.WriteLine("=== FIInstrumentation C# E2E Verification ===");
Console.WriteLine($"Backend: {baseUrl}");
Console.WriteLine($"Google API Key: {googleApiKey[..8]}...");
Console.WriteLine();

// ─── Register Tracer ────────────────────────────────────────────────
var tracer = TraceAI.Register(opts =>
{
    opts.ProjectName = "csharp-e2e-test";
    opts.Endpoint = baseUrl;
    opts.Batch = false; // Simple processor for immediate export
    opts.Verbose = true;
    opts.EnableConsoleExporter = true; // Also print spans to console
});

Console.WriteLine("[OK] Tracer registered");
Console.WriteLine();

var httpClient = new HttpClient();
httpClient.DefaultRequestHeaders.Authorization =
    new AuthenticationHeaderValue("Bearer", googleApiKey);

// Google's OpenAI-compatible endpoint
const string googleEndpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions";
const string model = "gemini-2.0-flash";

var passed = 0;
var failed = 0;

// ─── Test 1: Basic LLM Call ─────────────────────────────────────────
await RunTest("Basic LLM Call (Chain + LLM spans)", async () =>
{
    await tracer.ChainAsync("e2e-basic-chain", async span =>
    {
        span.SetInput("What is 2+2?");

        var response = await tracer.LlmAsync("gemini-chat", async llmSpan =>
        {
            llmSpan.SetAttribute(SemanticConventions.GenAiRequestModel, model);
            llmSpan.SetAttribute(SemanticConventions.GenAiProviderName, "google");

            var messages = new[]
            {
                new { role = "user", content = "What is 2+2? Reply with just the number." }
            };
            llmSpan.SetInputMessages(messages.Select(m =>
                new Dictionary<string, string> { ["role"] = m.role, ["content"] = m.content }).ToList());

            var body = JsonSerializer.Serialize(new { model, messages });
            var content = new StringContent(body, Encoding.UTF8, "application/json");
            var resp = await httpClient.PostAsync(googleEndpoint, content);
            var respBody = await resp.Content.ReadAsStringAsync();

            llmSpan.SetAttribute(SemanticConventions.GenAiResponseId, "e2e-test-1");
            llmSpan.SetOutput(respBody);

            Console.WriteLine($"  Response status: {resp.StatusCode}");
            return respBody;
        });

        span.SetOutput(response);
    });
});

// ─── Test 2: Tool Call ──────────────────────────────────────────────
await RunTest("Tool Call (Agent + Tool spans)", async () =>
{
    await tracer.AgentAsync("e2e-agent", async agentSpan =>
    {
        agentSpan.SetInput("Look up the weather");

        // Simulate a tool call
        var toolResult = tracer.Tool("weather-lookup", toolSpan =>
        {
            toolSpan.SetTool("get_weather", "Gets current weather for a city");
            toolSpan.SetInput("{\"city\": \"San Francisco\"}");
            var result = "{\"temp\": 65, \"condition\": \"sunny\"}";
            toolSpan.SetOutput(result);
            return result;
        });

        // Then call LLM with tool result
        await tracer.LlmAsync("gemini-with-tool-result", async llmSpan =>
        {
            llmSpan.SetAttribute(SemanticConventions.GenAiRequestModel, model);

            var messages = new[]
            {
                new { role = "user", content = "What's the weather in SF?" },
                new { role = "assistant", content = $"Based on the tool: {toolResult}" },
            };
            llmSpan.SetInputMessages(messages.Select(m =>
                new Dictionary<string, string> { ["role"] = m.role, ["content"] = m.content }).ToList());

            var body = JsonSerializer.Serialize(new { model, messages });
            var resp = await httpClient.PostAsync(googleEndpoint,
                new StringContent(body, Encoding.UTF8, "application/json"));

            Console.WriteLine($"  Response status: {resp.StatusCode}");
            llmSpan.SetOutput(await resp.Content.ReadAsStringAsync());
        });

        agentSpan.SetOutput("Weather lookup complete");
    });
});

// ─── Test 3: Context Attributes ─────────────────────────────────────
await RunTest("Context Attributes (session, user, metadata)", async () =>
{
    using (ContextAttributes.UsingSession("e2e-session-001"))
    using (ContextAttributes.UsingUser("e2e-user-csharp"))
    using (ContextAttributes.UsingMetadata(new Dictionary<string, object>
    {
        ["language"] = "csharp",
        ["test"] = "e2e-verification"
    }))
    using (ContextAttributes.UsingTags(new List<string> { "e2e", "csharp", "verification" }))
    {
        await tracer.ChainAsync("e2e-with-context", async span =>
        {
            span.SetInput("Testing context attributes");

            await tracer.LlmAsync("gemini-context-test", async llmSpan =>
            {
                llmSpan.SetAttribute(SemanticConventions.GenAiRequestModel, model);

                var messages = new[]
                {
                    new { role = "user", content = "Say hello in one word." }
                };
                llmSpan.SetInputMessages(messages.Select(m =>
                    new Dictionary<string, string> { ["role"] = m.role, ["content"] = m.content }).ToList());

                var body = JsonSerializer.Serialize(new { model, messages });
                var resp = await httpClient.PostAsync(googleEndpoint,
                    new StringContent(body, Encoding.UTF8, "application/json"));

                Console.WriteLine($"  Response status: {resp.StatusCode}");
                llmSpan.SetOutput(await resp.Content.ReadAsStringAsync());
            });

            span.SetOutput("Context test complete");
        });
    }
});

// ─── Test 4: Error Handling ─────────────────────────────────────────
await RunTest("Error Handling (bad API key → error span)", async () =>
{
    var badClient = new HttpClient();
    badClient.DefaultRequestHeaders.Authorization =
        new AuthenticationHeaderValue("Bearer", "invalid-key-12345");

    await tracer.ChainAsync("e2e-error-chain", async span =>
    {
        span.SetInput("This should fail with auth error");

        try
        {
            await tracer.LlmAsync("gemini-bad-auth", async llmSpan =>
            {
                llmSpan.SetAttribute(SemanticConventions.GenAiRequestModel, model);
                llmSpan.SetAttribute(SemanticConventions.GenAiProviderName, "google");

                var body = JsonSerializer.Serialize(new
                {
                    model,
                    messages = new[] { new { role = "user", content = "This will fail" } }
                });
                var resp = await badClient.PostAsync(googleEndpoint,
                    new StringContent(body, Encoding.UTF8, "application/json"));

                var respBody = await resp.Content.ReadAsStringAsync();
                Console.WriteLine($"  Response status: {resp.StatusCode} (expected 4xx)");

                if (!resp.IsSuccessStatusCode)
                    throw new HttpRequestException($"API error: {resp.StatusCode} - {respBody}");

                return respBody;
            });
        }
        catch (HttpRequestException ex)
        {
            span.SetError(ex);
            Console.WriteLine($"  Error captured in span: {ex.Message[..Math.Min(80, ex.Message.Length)]}...");
        }
    });
});

// ─── Test 5: Nested Spans (deep hierarchy) ──────────────────────────
await RunTest("Nested Spans (4-level hierarchy)", () =>
{
    tracer.Agent("e2e-root-agent", rootSpan =>
    {
        rootSpan.SetInput("Process a multi-step task");

        tracer.Chain("e2e-step-1", step1 =>
        {
            step1.SetInput("Step 1: Preprocess");
            step1.SetOutput("Preprocessed data");
        });

        tracer.Chain("e2e-step-2", step2 =>
        {
            step2.SetInput("Step 2: Call tool");

            tracer.Tool("e2e-inner-tool", tool =>
            {
                tool.SetTool("database_query", "Queries the database");
                tool.SetInput("{\"query\": \"SELECT * FROM users\"}");
                tool.SetOutput("[{\"id\": 1, \"name\": \"Alice\"}]");
            });

            step2.SetOutput("Tool result processed");
        });

        rootSpan.SetOutput("Multi-step task complete");
    });

    return Task.CompletedTask;
});

// ─── Summary ────────────────────────────────────────────────────────
// Give batch processor a moment to flush
await Task.Delay(2000);
TraceAI.Shutdown();

Console.WriteLine();
Console.WriteLine("=== Results ===");
Console.WriteLine($"Passed: {passed}");
Console.WriteLine($"Failed: {failed}");
Console.WriteLine($"Total:  {passed + failed}");
Console.WriteLine();

if (failed == 0)
    Console.WriteLine("All tests passed! Check your UI for traces from project 'csharp-e2e-test'.");
else
    Console.WriteLine("Some tests failed — check output above.");

return failed > 0 ? 1 : 0;

// ─── Helper ─────────────────────────────────────────────────────────
async Task RunTest(string name, Func<Task> test)
{
    Console.WriteLine($"--- Test: {name} ---");
    try
    {
        await test();
        passed++;
        Console.WriteLine($"  [PASS] {name}");
    }
    catch (Exception ex)
    {
        failed++;
        Console.WriteLine($"  [FAIL] {name}: {ex.Message}");
    }
    Console.WriteLine();
}
