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
                var auto=context.LoadFromAssemblyPath(Path.Combine(directory,"TraceAI.AutoInstrumentation.Runtime.dll")).GetType("TraceAI.AutoInstrumentation.TraceAIAuto")!;
                s_initialize=auto.GetMethod("Init",BindingFlags.Static|BindingFlags.Public)!;
                s_initialize.Invoke(null,new object?[] { null });
                EnableHostingStartup(auto,directory);
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
    private const string HostingStartupName="TraceAI.AutoInstrumentation.HostingStartup";
    private const string HostingStartupSettings="TraceAI.AutoInstrumentation.HostingStartup";
    /// <summary>ASP.NET Core web hosts only: lets the hosting startup wrap the application's registered
    /// chat clients, embedding generators and agents. Runs after the agent started and only when the agent
    /// decided to (running, integration enabled). Appends to ASPNETCORE_HOSTINGSTARTUPASSEMBLIES, never replaces it.
    /// A non-web application never reads the variable. Fail-open.</summary>
    private static void EnableHostingStartup(Type auto,string directory)
    {
        try
        {
            var settings=auto.GetMethod("HostingStartupPlan",BindingFlags.Static|BindingFlags.Public)?.Invoke(null,null) as string;
            var path=Path.Combine(directory,HostingStartupName+".dll");
            if(string.IsNullOrEmpty(settings) || !File.Exists(path)) return;
            AppContext.SetData(HostingStartupSettings,settings);
            // The host loads the assembly by name in the default context; it is not on the application's probing path.
            AssemblyLoadContext.Default.Resolving+=(context,name)=>name.Name==HostingStartupName?context.LoadFromAssemblyPath(path):null;
            const string variable="ASPNETCORE_HOSTINGSTARTUPASSEMBLIES";
            var existing=Environment.GetEnvironmentVariable(variable);
            var entries=(existing??"").Split(';',StringSplitOptions.RemoveEmptyEntries|StringSplitOptions.TrimEntries);
            if(!entries.Contains(HostingStartupName,StringComparer.OrdinalIgnoreCase))
                Environment.SetEnvironmentVariable(variable,string.IsNullOrWhiteSpace(existing)?HostingStartupName:existing.TrimEnd(';')+";"+HostingStartupName);
        }
        catch { }
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
        if(shared.GetName().Version!.Major is <8 or >11)
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
