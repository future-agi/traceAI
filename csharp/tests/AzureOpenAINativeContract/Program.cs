using System.ClientModel;
using System.ClientModel.Primitives;
using System.Diagnostics;
using System.Text.Json;
using OpenAI;
using OpenAI.Chat;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

// Internal contract consumer: only published SDKs create the model activities.
string scenario = args.Single();
AppContext.SetSwitch("OpenAI.Experimental.EnableOpenTelemetry", scenario != "disabled");
using var provider = Sdk.CreateTracerProviderBuilder()
    .SetResourceBuilder(ResourceBuilder.CreateDefault()
        .AddService("azure-openai-csharp-contract")
        .AddAttributes(new Dictionary<string, object>
        {
            ["project_name"] = "azure-csharp-contract",
            ["project_type"] = "observe",
        }))
    .AddSource("OpenAI.*", "AzureContract.Application")
    .AddOtlpExporter(options =>
    {
        options.Endpoint = new Uri(Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")!);
        options.Protocol = OtlpExportProtocol.HttpProtobuf;
        options.Headers = "X-Api-Key=synthetic-fi-api,X-Secret-Key=synthetic-fi-secret,fi-project-name=azure-csharp-contract";
    })
    .Build();
using var source = new ActivitySource("AzureContract.Application");
using (var parent = source.StartActivity("application"))
{
    var azure = new OpenAIClient(
        new ApiKeyCredential("synthetic-azure-key"),
        new OpenAIClientOptions
        {
            Endpoint = new Uri(Environment.GetEnvironmentVariable("AZURE_OPENAI_ENDPOINT")!),
            RetryPolicy = new ClientRetryPolicy(maxRetries: 2),
            NetworkTimeout = TimeSpan.FromSeconds(5),
        });
    ChatClient chat = azure.GetChatClient("test-deployment");
    ChatMessage[] messages = [new UserChatMessage("PROMPT_PRIVATE_MARKER")];
    var options = new ChatCompletionOptions { MaxOutputTokenCount = 32, Temperature = 0.2f, TopP = 0.9f };
    if (scenario == "tools")
    {
        options.Tools.Add(ChatTool.CreateFunctionTool(
            "get_weather", "TOOL_DESCRIPTION_PRIVATE_MARKER",
            BinaryData.FromString("{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}}}")));
    }
    try
    {
        string output;
        if (scenario == "stream-sync")
        {
            output = string.Concat(chat.CompleteChatStreaming(messages).SelectMany(update => update.ContentUpdate).Select(part => part.Text));
        }
        else if (scenario == "stream-async")
        {
            output = "";
            await foreach (var update in chat.CompleteChatStreamingAsync(messages))
                output += string.Concat(update.ContentUpdate.Select(part => part.Text));
        }
        else if (scenario == "embedding")
        {
            var result = await azure.GetEmbeddingClient("test-deployment").GenerateEmbeddingAsync("PROMPT_PRIVATE_MARKER");
            output = result.Value.ToFloats().Length.ToString();
        }
        else if (scenario == "responses")
        {
#pragma warning disable OPENAI001
            var response = await azure.GetResponsesClient().CreateResponseAsync("test-deployment", "PROMPT_PRIVATE_MARKER");
            output = response.Value.GetOutputText();
#pragma warning restore OPENAI001
        }
        else if (scenario == "protocol")
        {
            using var content = BinaryContent.Create(BinaryData.FromString("{\"model\":\"test-deployment\",\"messages\":[{\"role\":\"user\",\"content\":\"PROMPT_PRIVATE_MARKER\"}]}"));
            var result = await chat.CompleteChatAsync(content);
            output = result.GetRawResponse().Status.ToString();
        }
        else
        {
            using var cancellation = new CancellationTokenSource();
            if (scenario == "cancel-before") cancellation.Cancel();
            ChatCompletion completion;
            if (scenario == "sync")
            {
                completion = chat.CompleteChat(messages, options);
            }
            else
            {
                var pending = chat.CompleteChatAsync(messages, options, cancellation.Token);
                if (scenario == "cancel-during")
                {
                    // Wait for the real request to reach the fixture, not a wall-clock race.
                    using var fixture = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
                    var signal = new Uri(new Uri(Environment.GetEnvironmentVariable("AZURE_OPENAI_ENDPOINT")!), "/request-started");
                    using var response = await fixture.GetAsync(signal);
                    response.EnsureSuccessStatusCode();
                    cancellation.Cancel();
                }
                completion = await pending;
            }
            output = scenario == "tools" ? completion.ToolCalls.Single().FunctionArguments.ToString() : completion.Content[0].Text;
        }
        Console.WriteLine(JsonSerializer.Serialize(new { outcome = "success", output }));
    }
    catch (OperationCanceledException)
    {
        Console.WriteLine(JsonSerializer.Serialize(new { outcome = "cancelled" }));
    }
    catch (ClientResultException error)
    {
        Console.WriteLine(JsonSerializer.Serialize(new { outcome = "error", status = error.Status }));
    }
}
if (!provider.ForceFlush(10000)) throw new InvalidOperationException("Trace export did not flush");
