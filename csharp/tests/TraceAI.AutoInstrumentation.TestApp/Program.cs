using System.Diagnostics;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

// No traceAI reference. Only the conflict probe initializes the app's older OTel.
if(args.Length>1 && args[1].StartsWith("round5_",StringComparison.Ordinal)) { await RoundFiveProbe.Run(args[0],args[1]); return; }
if(args.Length>1 && args[1].StartsWith("round4_",StringComparison.Ordinal)) { await RoundFourProbe.Run(args[0],args[1]); return; }
if (args.Contains("dependency_conflict")) { await DependencyConflictProbe.Run(args[0]); return; }
const string requestJson = "{\"model\":\"local-test\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}";
if (args.Contains("idle")) { Console.WriteLine(""); return; }
if (args.Contains("native_openai"))
{
    var endpoint = new Uri(new Uri(args[0]), "/v1/");
    var chat = new OpenAI.Chat.ChatClient("local-test", new System.ClientModel.ApiKeyCredential("local-provider-key"),
        new OpenAI.OpenAIClientOptions { Endpoint = endpoint });
    var completion = await chat.CompleteChatAsync(new OpenAI.Chat.UserChatMessage("hello"));
    Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(completion.Value.Content[0].Text)));
    return;
}
if (args.Contains("transaction"))
{
    var builder = WebApplication.CreateBuilder();
    builder.WebHost.UseUrls("http://127.0.0.1:0");
    builder.Logging.ClearProviders();
    await using var app = builder.Build();
    app.MapGet("/incoming", async context =>
    {
        using var outgoing = new HttpClient();
        using var response = await outgoing.PostAsync(args[0], new StringContent(requestJson, Encoding.UTF8, "application/json"));
        context.Response.ContentType = response.Content.Headers.ContentType?.ToString();
        await response.Content.CopyToAsync(context.Response.Body);
    });
    await app.StartAsync();
    var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.Single();
    // Simulate an external caller with no distributed parent header.
    using var incoming = new HttpClient(new SocketsHttpHandler { ActivityHeadersPropagator = null });
    Console.WriteLine(Convert.ToBase64String(await incoming.GetByteArrayAsync(address + "/incoming")));
    await app.StopAsync();
    return;
}
using var client = new HttpClient();
using var source = new ActivitySource("OpenAI.Test");
using var parent = args.Contains("dedup") ? source.StartActivity("chat library", ActivityKind.Client,
    default(ActivityContext), new ActivityTagsCollection { ["gen_ai.operation.name"] = "chat", ["gen_ai.span.kind"] = "LLM" }) : null;
using var request = new HttpRequestMessage(HttpMethod.Post, args[0]) { Content = new StringContent(requestJson, Encoding.UTF8, "application/json") };
using var result = args.Contains("sync") ? client.Send(request, HttpCompletionOption.ResponseHeadersRead)
    : await client.SendAsync(request, args.Contains("buffered") ? HttpCompletionOption.ResponseContentRead : HttpCompletionOption.ResponseHeadersRead);
await using var body = await result.Content.ReadAsStreamAsync();
using var bytes = new MemoryStream(); await body.CopyToAsync(bytes);
Console.WriteLine(Convert.ToBase64String(bytes.ToArray()));
