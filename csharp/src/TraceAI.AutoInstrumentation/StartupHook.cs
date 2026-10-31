using System.Reflection;
using System.Runtime.Loader;

// The CLR requires this exact global type and signature. Bootstrap references
// framework types only: no OTel/SDK types can be bound in the default context.
internal class StartupHook
{
    private static readonly object Gate=new();
    private static MethodInfo? s_initialize;
    private static bool s_initialized,s_repeatLogged;
    public static void Initialize()
    {
        lock(Gate)
        {
            if(s_initialized)
            {
                try
                {
                    if(s_initialize!=null) s_initialize.Invoke(null,new object?[] { null }); // Runtime's process-wide once guard/log.
                    else if(!s_repeatLogged)
                    { s_repeatLogged=true; Console.Error.WriteLine("[traceAI auto] Bootstrap already attempted; a new process is required to retry."); }
                }
                catch { }
                return;
            }
            s_initialized=true;
            try
            {
                var path=typeof(StartupHook).Assembly.Location;
                var directory=Path.GetDirectoryName(path)!;
                var diagnostics=SharedDiagnostics();
                var context=new AgentLoadContext(directory,diagnostics);
                s_initialize=context.LoadFromAssemblyPath(Path.Combine(directory,"TraceAI.AutoInstrumentation.Runtime.dll")).GetType("TraceAI.AutoInstrumentation.TraceAIAuto")!
                    .GetMethod("Init",BindingFlags.Static|BindingFlags.Public)!;
                s_initialize.Invoke(null,new object?[] { null });
            }
            catch(Exception error)
            {
                try
                {
                    Console.Error.WriteLine("[traceAI auto] Agent could not load; application continues without tracing.");
                    if(Environment.GetEnvironmentVariable("FI_AUTO_DEBUG")=="true")
                        for(Exception? e=error;e!=null;e=e.InnerException)
                            Console.Error.WriteLine(e.GetType().Name+"\n"+e.StackTrace);
                }
                catch { }
            }
        }
    }
    private static Assembly SharedDiagnostics()
    {
        const string name="System.Diagnostics.DiagnosticSource";
        // Respect the application's deps/runtime binding, including an earlier hook.
        // A private Load override can explicitly unify to this assembly even when
        // OTel references a newer version. Our .NET 8/10 capture path uses the shared
        // Activity/DiagnosticListener API available since DiagnosticSource 8.
        // Never try to replace a TPA binding or load a second Activity universe.
        var shared=AssemblyLoadContext.Default.LoadFromAssemblyName(new AssemblyName(name));
        if(shared.GetName().Version!.Major is <8 or >10)
            throw new NotSupportedException("Unsupported DiagnosticSource major version");
        return shared;
    }
    private sealed class AgentLoadContext(string directory,Assembly diagnostics) : AssemblyLoadContext("traceai-auto",isCollectible:false)
    {
        protected override Assembly? Load(AssemblyName name)
        {
            if(name.Name==diagnostics.GetName().Name) return diagnostics;
            if(name.Name==null || name.Name.Contains('/') || name.Name.Contains('\\')) return null;
            // Private OTel/Extensions/SDK dependencies never bind to the app's versions.
            var path=Path.Combine(directory,name.Name+".dll");
            return File.Exists(path)?LoadFromAssemblyPath(path):null;
        }
    }
}
