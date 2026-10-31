using System.Diagnostics;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;

// A stock console application with only the shared runtime's DiagnosticSource.
using var client=new HttpClient();
using var response=await client.PostAsync(args[0],new StringContent("{\"model\":\"local-test\",\"messages\":[]}",Encoding.UTF8,"application/json"));
var bytes=await response.Content.ReadAsByteArrayAsync();
var diagnostics=typeof(Activity).Assembly;
Console.WriteLine(JsonSerializer.Serialize(new {
    runtime=Environment.Version.Major,body=Convert.ToBase64String(bytes),
    diagnosticSourceCount=AppDomain.CurrentDomain.GetAssemblies().Count(a=>a.GetName().Name==diagnostics.GetName().Name),
    diagnosticSourceVersion=diagnostics.GetName().Version!.ToString(),
    diagnosticSourceContext=AssemblyLoadContext.GetLoadContext(diagnostics)!.Name
}));
