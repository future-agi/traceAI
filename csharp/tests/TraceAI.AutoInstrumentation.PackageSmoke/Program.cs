// A stock application with no traceAI reference. The package check
// (.github/release/check_startup_hook_package.sh) builds it into a clean project, starts it with
// DOTNET_STARTUP_HOOKS pointing at the installed package, and needs these results:
//   exit 2 with HOOK_NOT_LISTENING   no hook is attached (the negative control)
//   exit 0 with INSTALLED_HOOK_EXPORT_PASS   the hook exported a real AI span to the local collector
// BCL only: it starts its own loopback collector and talks to nothing else.
using System.Diagnostics;
using System.Net;
using System.Runtime.Loader;
using System.Text;

using var server = new HttpListener();
server.Prefixes.Add("http://127.0.0.1:18762/");
server.Start();
var received = Task.Run(async () =>
{
    var request = await server.GetContextAsync();
    if (request.Request.Url!.AbsolutePath != "/tracer/v1/traces" || request.Request.HttpMethod != "POST")
        throw new Exception("Unexpected collector request");
    using var body = new MemoryStream();
    await request.Request.InputStream.CopyToAsync(body);
    request.Response.StatusCode = 200;
    request.Response.ContentLength64 = 0;
    request.Response.Close();
    return body.ToArray();
});

using var source = new ActivitySource("OpenAI.PackageSmoke");
using (var span = source.StartActivity("package-proof-chat", ActivityKind.Client))
{
    if (span == null) { Console.WriteLine("HOOK_NOT_LISTENING"); return 2; }
    span.SetTag("gen_ai.operation.name", "chat");
    span.SetTag("gen_ai.request.model", "local-test");
    span.SetTag("gen_ai.provider.name", "openai");
}

var payload = await received.WaitAsync(TimeSpan.FromSeconds(25));
if (!Encoding.UTF8.GetString(payload).Contains("package-proof-chat"))
    throw new Exception("Collector received no expected AI span");

var diagnostics = AppDomain.CurrentDomain.GetAssemblies().Where(a => a.GetName().Name == "System.Diagnostics.DiagnosticSource").ToArray();
if (diagnostics.Length != 1 || AssemblyLoadContext.GetLoadContext(diagnostics[0]) != AssemblyLoadContext.Default)
    throw new Exception("DiagnosticSource is not shared with the stock app");
if (diagnostics[0].GetName().Version!.Major != Environment.Version.Major)
    throw new Exception("The stock app's DiagnosticSource was upgraded");

var privateRuntime = AppDomain.CurrentDomain.GetAssemblies().Single(a => a.GetName().Name == "TraceAI.AutoInstrumentation.Runtime");
if (AssemblyLoadContext.GetLoadContext(privateRuntime)?.Name != "traceai-auto")
    throw new Exception("Agent runtime is not isolated");

Console.WriteLine("INSTALLED_HOOK_EXPORT_PASS runtime=" + Environment.Version.Major + " bytes=" + payload.Length);
return 0;
