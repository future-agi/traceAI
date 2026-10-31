using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using FIInstrumentation;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using TraceAI.AutoInstrumentation.Wire;

namespace TraceAI.AutoInstrumentation;

public sealed record IntegrationStatus(string Name, string Status, string Reason);

/// <summary>Zero-code agent entry point. Does not call TraceAI.Register or replace application providers.</summary>
public static class TraceAIAuto
{
    private const string InitStateKey="TraceAI.AutoInstrumentation.InitState";
    private const string InitGateKey="TraceAI.AutoInstrumentation.InitGate";
    // AppDomain data contains BCL objects only, shared across the isolated hook and
    // an accidental in-process/manual entry point. Do not create two providers.
    private static readonly object Gate = ProcessGate();
    private static object ProcessGate()
    {
        lock(AppDomain.CurrentDomain)
        {
            if(AppDomain.CurrentDomain.GetData(InitGateKey) is {} gate) return gate;
            var created=new object(); AppDomain.CurrentDomain.SetData(InitGateKey,created); return created;
        }
    }
    private static readonly ConcurrentDictionary<string, byte> Warnings = new();
    private static Agent? s_agent;
    private static AutoConfig? s_config;
    private static bool s_initialized;
    private static bool s_exitRegistered;
    private static PosixSignalRegistration? s_termination;
    private static bool s_failed;
    internal static readonly ActivitySource Source = new("TraceAI.AutoInstrumentation", "0.1.0");

    public static void Init(Action<AutoOptions>? configure = null)
    {
        lock (Gate)
        {
            if(s_initialized || AppDomain.CurrentDomain.GetData(InitStateKey) is true)
            {
                if(AppDomain.CurrentDomain.GetData(InitStateKey+".RepeatLogged") is not true)
                {
                    AppDomain.CurrentDomain.SetData(InitStateKey+".RepeatLogged",true);
                    Warn("init-once","Agent already initialized; configuration changes require a new process.");
                }
                return;
            }
            s_initialized=true; AppDomain.CurrentDomain.SetData(InitStateKey,true);
            try
            {
                var options = new AutoOptions(); configure?.Invoke(options);
                var config = s_config = new AutoConfig(options.Copy());
                if(!config.Disabled && (config.HasCredentials || config.Options.Exporter!=null || config.Options.Provider!=null))
                {
                    s_agent = new Agent(config); s_failed=false;
                    if(!s_exitRegistered)
                    {
                        AppDomain.CurrentDomain.ProcessExit += (_, _) => Shutdown(); s_exitRegistered=true;
                        if(!OperatingSystem.IsWindows()) try
                        {
                            // .NET 10 need not raise ProcessExit for default SIGTERM.
                            // Join the runtime's signal dispatcher, never cancel/re-raise
                            // or replace application handlers. Flush, not Shutdown: an
                            // application handler may cancel SIGTERM and keep running.
                            s_termination=PosixSignalRegistration.Create(PosixSignal.SIGTERM,static _=>Flush(3000));
                        }
                        catch { Warn("sigterm","SIGTERM flush registration unavailable; normal-exit flushing remains enabled."); }
                    }
                    if(config.Bool("AUTO_DEBUG")) Console.Error.WriteLine(Doctor());
                }
                else if(!config.Disabled) Warn("credentials", "FI_API_KEY and FI_SECRET_KEY are required for the default collector; no exporter or provider was supplied, so auto-instrumentation is disabled.");
            }
            catch { s_failed=true; Warn("init", "Agent initialization failed; application continues without tracing."); }
        }
    }
    /// <summary>Registers an additional credential for process-wide export redaction.
    /// Values shorter than eight characters are ignored. Never logs credential values.</summary>
    public static void RegisterSecret(string? value) => KnownSecrets.Register(value);

    /// <summary>No network unless testExport is explicitly true. Secrets are never printed.</summary>
    public static string Doctor(bool testExport = false)
    {
        try
        {
            if(testExport && s_agent != null)
            {
                using (var span=Source.StartActivity("traceai.doctor",ActivityKind.Internal)) span?.SetTag("traceai.doctor",true);
                Flush();
            }
            var report=JsonSerializer.Serialize(new {
                enabled=s_agent!=null,
                configuration=s_config?.Masked(),
                integrations=GetStatus(),
                collector=s_agent?.CollectorStatus??"not_tested",
                provider=s_config?.Options.Provider!=null?"supplied host provider reused; source subscriptions and lifetime remain application-owned":"independent additive .NET provider; existing providers remain installed"
            }, new JsonSerializerOptions { WriteIndented=true });
            return WirePrivacy.ScrubString(report,KnownSecrets.Snapshot());
        }
        catch { return "{\"enabled\":false,\"error\":\"diagnostics unavailable\"}"; }
    }
    public static IReadOnlyList<IntegrationStatus> GetStatus()
    {
        try { return Agent.Integrations.Select(i=>s_agent?.Status(i)??new IntegrationStatus(i.Name,s_failed?"failed":"disabled",s_failed?"agent initialization failed":s_config?.Disabled==true?"OTEL_SDK_DISABLED":"agent inactive")).ToArray(); }
        catch { return []; }
    }
    /// <summary>Reports installed libraries and their built-in ActivitySource coverage. Does not modify the app.</summary>
    public static string Bootstrap() => string.Join(Environment.NewLine,GetStatus().Select(s=>$"{s.Name}: {s.Status} — {s.Reason}"));
    public static bool Flush(int timeoutMilliseconds = 3000)
    { try { return s_agent?.Flush(timeoutMilliseconds)??true; } catch { Warn("export","Collector unavailable; tracing data may be dropped."); return false; } }
    public static void Shutdown(int timeoutMilliseconds = 3000)
    {
        Agent? agent;
        lock(Gate) { agent=s_agent; s_agent=null; }
        try { agent?.Shutdown(timeoutMilliseconds); } catch { }
    }
    internal static void Warn(string key,string message)
    { try { if(Warnings.TryAdd(key,0)) Console.Error.WriteLine("[traceAI auto] "+message); } catch { } }
}

internal sealed record Integration(string Name,string[] Sources,string[] Assemblies,bool Manual=false);
internal sealed class Agent : IDisposable
{
    // Source patterns are intentionally prefixes, not hard package references; late assembly loads are covered.
    internal static readonly Integration[] Integrations = [
        new("openai",["OpenAI."],["OpenAI"]),
        new("extensions_ai",["Experimental.Microsoft.Extensions.AI","Microsoft.Extensions.AI"],["Microsoft.Extensions.AI"],true),
        new("semantic_kernel",["Microsoft.SemanticKernel"],["Microsoft.SemanticKernel"]),
        new("azure_inference",["Azure.AI.Inference"],["Azure.AI.Inference"]),
        new("azure_openai",["Azure.AI.OpenAI"],["Azure.AI.OpenAI"]),
        new("agents",["Experimental.Microsoft.Agents.AI","Microsoft.Agents.AI"],["Microsoft.Agents.AI"],true),
        new("aspnetcore",["Microsoft.AspNetCore"],["Microsoft.AspNetCore"]),
        new("wire",[],[])
    ];
    private readonly AutoConfig _config;
    private readonly TracerProvider _provider;
    private readonly HttpCapture? _wire;
    private readonly ActivityListener _discovery;
    private readonly ModelCoverage _coverage=new();
    private readonly ConcurrentDictionary<string,byte> _seen = new();
    private readonly HashSet<string> _installed;
    private readonly TrackingOtlpExporter? _otlp;
    private readonly ExportCompletion _completion=new();
    private readonly PrivacyBatchProcessor? _batch;
    private readonly bool _ownsProvider;
    private int _disposed;
    private readonly object _lifecycle=new();
    private Task<bool>? _flush;
    private Task? _shutdown;
    internal string CollectorStatus => _otlp?.LastResult ?? (_config.Options.Exporter!=null?"custom_exporter":!_ownsProvider?"host_provider":"not_tested");
    internal Agent(AutoConfig config)
    {
        _config=config; _installed=InstalledAssemblies();
        if(config.Enabled("openai")) AppContext.SetSwitch("OpenAI.Experimental.EnableOpenTelemetry",true);
        if(config.Enabled("semantic_kernel")) AppContext.SetSwitch("Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnostics",true);
        if(config.Enabled("azure_inference")||config.Enabled("azure_openai")) AppContext.SetSwitch("Azure.Experimental.EnableActivitySource",true);
        var sampler=new ParentBasedSampler(new TraceIdRatioBasedSampler(config.SampleRate));
        var privacy=new AutoProcessor(config,()=>Volatile.Read(ref _disposed)==0);
        _ownsProvider=config.Options.Provider==null;
        TracerProviderBuilder? builder=null;
        if(_ownsProvider)
        {
            // Normal OTel detection preserves arbitrary application resource identity;
            // FI project routing is separate, never an AddService override.
            var resource=privacy.WithRouting(ResourceBuilder.CreateDefault().Build());
            builder=Sdk.CreateTracerProviderBuilder()
                .SetResourceBuilder(ResourceBuilder.CreateEmpty().AddAttributes(privacy.SanitizeResource(resource).Attributes))
                .SetSampler(sampler).AddSource(TraceAIAuto.Source.Name).AddProcessor(privacy);
            foreach(var integration in Integrations.Where(i=>config.Enabled(i.Name))) foreach(var prefix in integration.Sources) builder.AddSource(prefix+"*");
        }
        BaseExporter<Activity>? exporter=null;
        if(config.Options.Exporter!=null) exporter=new BorrowedExporter(config.Options.Exporter,privacy,_completion);
        else if(_ownsProvider) exporter=_otlp=new TrackingOtlpExporter(new OtlpExporterOptions {
            Endpoint=new Uri(config.Get("BASE_URL").TrimEnd('/')+"/tracer/v1/traces"), Protocol=OtlpExportProtocol.HttpProtobuf,
            Headers="X-Api-Key="+Uri.EscapeDataString(config.Get("API_KEY"))+",X-Secret-Key="+Uri.EscapeDataString(config.Get("SECRET_KEY"))+",fi-project-name="+Uri.EscapeDataString(config.Get("PROJECT_NAME")),
            TimeoutMilliseconds=2000
        },privacy,_completion);
        if(exporter!=null)
        {
            _batch=new PrivacyBatchProcessor(exporter,sampler,()=>Volatile.Read(ref _disposed)==0);
            builder?.AddProcessor(_batch);
        }
        _provider=builder?.Build()??config.Options.Provider!;
        // Existing host destinations and sampler remain untouched. Supplying only
        // a provider never constructs an FI destination, even if FI keys exist.
        if(!_ownsProvider && _batch!=null) _provider.AddProcessor(_batch);
        _discovery=new ActivityListener {
            ShouldListenTo=source=> {
                var i=IntegrationForSource(source.Name); if(i!=null) _seen.TryAdd(i.Name,0);
                // Observe activities another listener already requested, including
                // native intermediate sources; never request recording or export them.
                return true;
            },
            Sample=(ref ActivityCreationOptions<ActivityContext> _)=>ActivitySamplingResult.None,
            SampleUsingParentId=(ref ActivityCreationOptions<string> _)=>ActivitySamplingResult.None,
            ActivityStarted=activity=> {
                try
                {
                    if(InstrumentationSuppression.Active) activity.SetCustomProperty("traceai.suppressed",true);
                    _coverage.Start(activity);
                }
                catch { }
            },
            ActivityStopped=activity=> { try { _coverage.End(activity); } catch { } }
        };
        try
        {
            ActivitySource.AddActivityListener(_discovery);
            // Credential echo privacy also protects library spans when wire body
            // capture is disabled; HttpCapture checks the wire flag before capture.
            _wire=new HttpCapture(config,_coverage,privacy);
        }
        catch { _discovery.Dispose(); if(_ownsProvider) _provider.Dispose(); else _batch?.Dispose(); throw; }
    }
    internal static Integration? IntegrationForSource(string source) => Integrations.FirstOrDefault(i=>i.Sources.Any(p=>source.StartsWith(p,StringComparison.Ordinal)));
    internal IntegrationStatus Status(Integration i)
    {
        if(!_config.Enabled(i.Name)) return new(i.Name,"disabled","configuration");
        if(i.Name=="wire") return new(i.Name,"instrumented","HttpClient DiagnosticListener with bounded response tee");
        bool loaded=AppDomain.CurrentDomain.GetAssemblies().Any(a=>i.Assemblies.Any(p=>a.GetName().Name?.StartsWith(p,StringComparison.Ordinal)==true));
        bool installed=loaded||_installed.Any(a=>i.Assemblies.Any(p=>a.StartsWith(p,StringComparison.Ordinal)));
        if(i.Manual) return new(i.Name,installed?"manual":"not_installed", "Listener ready; library must opt in with UseOpenTelemetry()/OpenTelemetryChatClient; custom source names are not detected");
        if(_seen.ContainsKey(i.Name)) return new(i.Name,"instrumented","built-in ActivitySource listener attached");
        if(loaded) return new(i.Name,"pending","assembly loaded; waiting for a supported ActivitySource");
        return new(i.Name,installed?"pending":"not_installed",installed?"built-in listener ready for late load":"library absent from loaded assemblies and app dependency manifest");
    }
    private static HashSet<string> InstalledAssemblies()
    {
        var result=new HashSet<string>();
        try
        {
            foreach(var path in ((string?)AppContext.GetData("APP_CONTEXT_DEPS_FILES")??"").Split(Path.PathSeparator,StringSplitOptions.RemoveEmptyEntries))
            {
                using var doc=JsonDocument.Parse(File.ReadAllText(path));
                if(doc.RootElement.TryGetProperty("libraries",out var libs)) foreach(var lib in libs.EnumerateObject()) result.Add(lib.Name.Split('/')[0]);
            }
        } catch { }
        return result;
    }
    internal bool Flush(int timeout)
    {
        timeout=Math.Max(0,timeout);
        var clock=Stopwatch.StartNew();
        if(ExportCallback.Active || Volatile.Read(ref _disposed)!=0) return false;
        long target=_batch?.Submitted??0;
        Task<bool> task;
        lock(_lifecycle)
        {
            if(_flush==null || _flush.IsCompleted) _flush=Task.Run(()=> { try { return _provider.ForceFlush(timeout); } catch { return false; } });
            task=_flush;
        }
        try
        {
            // OTel's flush may acknowledge a dequeued batch at the timeout boundary,
            // before Export returns. Only our completion watermark establishes that.
            return task.Wait(Math.Max(0,timeout-(int)clock.ElapsedMilliseconds)) && task.Result &&
                _completion.Wait(target,Math.Max(0,timeout-(int)clock.ElapsedMilliseconds));
        }
        catch { return false; }
    }
    internal void Disable()
    {
        if(Interlocked.Exchange(ref _disposed,1)!=0) return;
        _wire?.Dispose(); _discovery.Dispose(); _coverage.Clear();
    }
    internal void Shutdown(int timeout)
    {
        Disable(); timeout=Math.Max(0,timeout);
        Task task;
        lock(_lifecycle)
        {
            _shutdown??=Task.Run(()=> {
                try
                {
                    if(_ownsProvider) { _provider.Shutdown(timeout); _provider.Dispose(); }
                    else
                    {
                        // A supplied provider outlives the agent. Drain without
                        // shutting down its existing destinations or resources.
                        _provider.ForceFlush(timeout);
                        _batch?.Shutdown(timeout); _batch?.Dispose();
                    }
                }
                catch { }
            });
            task=_shutdown;
        }
        // Exporters may call back into shutdown. Never wait on the thread which is exporting.
        if(!ExportCallback.Active) try { task.Wait(timeout); } catch { }
    }
    public void Dispose() => Shutdown(3000);
}

internal static class ExportCallback
{
    [ThreadStatic] internal static bool Active;
}

internal static class InstrumentationSuppression
{
    private static readonly PropertyInfo? Legacy=typeof(SuppressTracing).GetProperty("IsSuppressed",BindingFlags.Static|BindingFlags.NonPublic);
    internal static bool Active
    {
        get { try { return Sdk.SuppressInstrumentation || Legacy?.GetValue(null) is true || HostContext.Suppressed; } catch { return Sdk.SuppressInstrumentation; } }
    }
}

/// <summary>Explicit exporters remain owned by the caller.</summary>
internal sealed class BorrowedExporter(BaseExporter<Activity> inner,AutoProcessor privacy,ExportCompletion completion) : BaseExporter<Activity>
{
    private sealed class Association { internal BaseProvider? Provider; }
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<BaseExporter<Activity>,Association> Associations=new();
    private static readonly PropertyInfo ProviderProperty=typeof(BaseExporter<Activity>).GetProperty(nameof(ParentProvider))!;
    private static readonly PropertyInfo? OtlpResource=typeof(OtlpTraceExporter).GetProperty("Resource",BindingFlags.Instance|BindingFlags.NonPublic);
    public override ExportResult Export(in Batch<Activity> batch)
    {
        bool previous=ExportCallback.Active; ExportCallback.Active=true;
        try
        {
            using var suppress=SuppressInstrumentationScope.Begin();
            var association=Associations.GetOrCreateValue(inner);
            var resource=inner is OtlpTraceExporter && inner.ParentProvider is {} owner && !ReferenceEquals(owner,association.Provider)?
                owner.GetResource():ParentProvider?.GetResource()??Resource.Empty;
            var snapshot=privacy.PrepareExport(in batch,resource,AttributeProjection.Limit(inner));
            if(snapshot.Spans.Length==0) return ExportResult.Success;
            var safe=new Batch<Activity>(snapshot.Spans,snapshot.Spans.Length);
            lock(association)
            {
                // BaseExporter has no public provider-association API. Forward its SDK association
                // without taking ownership, and never replace a caller's existing provider.
                if(ParentProvider!=null && (inner.ParentProvider==null || ReferenceEquals(inner.ParentProvider,association.Provider)))
                {
                    ProviderProperty.SetValue(inner,ParentProvider);
                    association.Provider=ParentProvider;
                }
                if(inner is OtlpTraceExporter)
                {
                    // The caller may share this exporter with another provider. Change only
                    // a detached exporter shell, never its cached resource or association.
                    var shell=(BaseExporter<Activity>)ExportSnapshot.Clone(inner);
                    OtlpResource!.SetValue(shell,snapshot.Resource);
                    return shell.Export(in safe);
                }
                return inner.Export(in safe);
            }
        }
        catch { return ExportResult.Failure; }
        finally { completion.Complete(batch.Count); ExportCallback.Active=previous; }
    }
}

internal sealed class TrackingOtlpExporter(OtlpExporterOptions options,AutoProcessor privacy,ExportCompletion completion,bool forceCompatibility=false) : OtlpTraceExporter(options)
{
    private readonly CompatibilityOtlpTransport? _compatibility=forceCompatibility || typeof(Activity).Assembly.GetName().Version!.Major<10?new(options):null;
    internal string LastResult = "not_tested";
    public override ExportResult Export(in Batch<Activity> batch)
    {
        try
        {
            using var suppress=SuppressInstrumentationScope.Begin();
            var snapshot=privacy.PrepareExport(in batch,ParentProvider?.GetResource()??Resource.Empty,AttributeProjection.Limit(this));
            if(snapshot.Spans.Length==0) return ExportResult.Success;
            var safe=new Batch<Activity>(snapshot.Spans,snapshot.Spans.Length);
            ExportResult result;
            if(_compatibility!=null) result=_compatibility.Export(snapshot.Resource,snapshot.Spans)?ExportResult.Success:ExportResult.Failure;
            else
            {
                typeof(OtlpTraceExporter).GetProperty("Resource",BindingFlags.Instance|BindingFlags.NonPublic)!.SetValue(this,snapshot.Resource);
                result=base.Export(in safe);
            }
            LastResult=result==ExportResult.Success?"accepted":"failed";
            if(result!=ExportResult.Success) TraceAIAuto.Warn("export","Collector unavailable; tracing data may be dropped.");
            return result;
        }
        catch { LastResult="failed"; TraceAIAuto.Warn("export","Collector unavailable; tracing data may be dropped."); return ExportResult.Failure; }
        finally { completion.Complete(batch.Count); }
    }
    protected override void Dispose(bool disposing)
    {
        if(disposing) _compatibility?.Dispose();
        base.Dispose(disposing);
    }
}

/// <summary>Counts batches only after the actual exporter has returned, not when dequeued.</summary>
internal sealed class ExportCompletion
{
    private readonly object _gate=new();
    private long _completed;
    internal void Complete(long count) { lock(_gate) { _completed+=count; Monitor.PulseAll(_gate); } }
    internal bool Wait(long target,int timeout)
    {
        var clock=Stopwatch.StartNew();
        lock(_gate)
        {
            while(_completed<target)
            {
                int remaining=Math.Max(0,timeout-(int)clock.ElapsedMilliseconds);
                if(remaining==0) return false;
                Monitor.Wait(_gate,remaining);
            }
            return true;
        }
    }
}

internal sealed class PrivacyBatchProcessor(BaseExporter<Activity> exporter,Sampler sampler,Func<bool> enabled)
    : BatchExportProcessor<Activity>(exporter,2048,1000,2500,512)
{
    private long _submitted;
    internal long Submitted => Interlocked.Read(ref _submitted);
    public override void OnEnd(Activity data)
    {
        try
        {
            if(!enabled() || data.GetCustomProperty("traceai.suppressed") is true) return;
            var parent=data.Parent?.Context??default;
            if(parent==default && data.ParentId!=null) ActivityContext.TryParse(data.ParentId,data.TraceStateString,true,out parent);
            var parameters=new SamplingParameters(parent,data.TraceId,data.DisplayName,data.Kind,data.TagObjects,data.Links);
            // Another provider may request recording even when our own listener sampled this trace out.
            if(data.Recorded && sampler.ShouldSample(in parameters).Decision==SamplingDecision.RecordAndSample)
            {
                // Include dropped queue entries: telemetry lost to overload must not
                // turn a later Flush into a false claim that all submissions completed.
                Interlocked.Increment(ref _submitted);
                OnExport(data);
            }
        }
        catch { }
    }
}

internal sealed class AutoProcessor : BaseProcessor<Activity>
{
    private readonly AutoConfig _config;
    private readonly TraceConfig _mask;
    private readonly Func<bool> _enabled;
    internal AutoProcessor(AutoConfig config,Func<bool>? enabled=null)
    {
        _config=config; _enabled=enabled??(()=>true); KnownSecrets.Seed(config);
        _mask=config.Options.TraceConfig??new TraceConfig {
            HideInputs=config.Bool("HIDE_INPUTS"), HideOutputs=config.Bool("HIDE_OUTPUTS"),
            HideInputMessages=config.Bool("HIDE_INPUT_MESSAGES"), HideOutputMessages=config.Bool("HIDE_OUTPUT_MESSAGES"),
            HideInputText=config.Bool("HIDE_INPUT_TEXT"), HideOutputText=config.Bool("HIDE_OUTPUT_TEXT"),
            HideInputImages=config.Bool("HIDE_INPUT_IMAGES"), HideEmbeddingVectors=config.Bool("HIDE_EMBEDDING_VECTORS"),
            HideLlmInvocationParameters=config.Bool("HIDE_LLM_INVOCATION_PARAMETERS"),
            Base64ImageMaxLength=int.TryParse(config.Get("BASE64_IMAGE_MAX_LENGTH"),out var max)?Math.Max(0,max):32000
        };
    }
    public override void OnStart(Activity activity)
    {
        try
        {
            if(!_enabled()) return;
            if(InstrumentationSuppression.Active) { activity.SetCustomProperty("traceai.suppressed",true); return; }
            Map(activity);
            for(var ancestor=activity;ancestor!=null;ancestor=ancestor.Parent)
                if(ancestor.GetCustomProperty("traceai.model.covered") is true || ancestor.GetTagItem("gen_ai.span.kind") is "LLM" or "EMBEDDING" or "RERANKER" or "GUARDRAIL")
                { activity.SetCustomProperty("traceai.model.covered",true); break; }
            if(activity.GetTagItem("traceai.capture.layer") is not string layer || layer!="wire")
                foreach(var (key,value) in HostContext.Attributes()) activity.SetTag(key,value);
        }
        catch { }
    }
    internal static void Map(Activity a)
    {
        var operation=a.GetTagItem("gen_ai.operation.name") as string??a.OperationName.Split(' ')[0];
        string? kind=operation.ToLowerInvariant() switch {
            "chat" or "generate_content" or "text_completion" or "completion" or "responses" or "transcription" or "text_to_speech" => "LLM",
            "embeddings" or "embedding" or "embed" => "EMBEDDING", "rerank" => "RERANKER",
            "invoke_agent" or "create_agent" => "AGENT", "execute_tool" or "invoke_tool" => "TOOL", _=>null };
        if(kind==null&&a.Source.Name.StartsWith("Microsoft.SemanticKernel")) kind="CHAIN";
        if(kind!=null&&a.GetTagItem("gen_ai.span.kind")==null) a.SetTag("gen_ai.span.kind",kind);
        foreach(var (legacy,current) in new[] { ("gen_ai.system","gen_ai.provider.name"),("gen_ai.usage.prompt_tokens","gen_ai.usage.input_tokens"),("gen_ai.usage.completion_tokens","gen_ai.usage.output_tokens") })
            if(a.GetTagItem(current)==null&&a.GetTagItem(legacy) is {} value) a.SetTag(current,value);
        if(a.GetTagItem("gen_ai.usage.total_tokens")==null &&
            a.GetTagItem("gen_ai.usage.input_tokens") is {} input && input is int or long &&
            a.GetTagItem("gen_ai.usage.output_tokens") is {} output && output is int or long)
            a.SetTag("gen_ai.usage.total_tokens",checked(Convert.ToInt64(input)+Convert.ToInt64(output)));
        if(a.Source.Name!=TraceAIAuto.Source.Name) a.SetTag("traceai.capture.layer","library");
    }
    // No privacy mutation on OnEnd: other providers own the same Activity.
    internal ExportSnapshot PrepareExport(in Batch<Activity> batch,Resource resource,int? attributeLimit=null)
    {
        // A queue-backed OTel Batch is single-pass (enumeration dequeues it).
        var activities=new List<Activity>();
        foreach(var activity in batch) activities.Add(activity);
        string[]? secrets;
        try { secrets=KnownSecrets.ForExport(_config); } catch { secrets=null; }
        object? Safe(string key,object value,string? operation) => secrets==null?throw new InvalidDataException("Credential privacy unavailable"):Mask(key,WirePrivacy.Scrub(value,secrets,path:key),operation);
        string Redact(string text) => secrets==null?throw new InvalidDataException("Credential privacy unavailable"):WirePrivacy.ScrubString(text,secrets);
        return ExportSnapshot.Create(activities,WithRouting(resource),Safe,Redact,EventKey,attributeLimit);
    }
    internal Resource WithRouting(Resource resource)=>ResourceBuilder.CreateEmpty()
        .AddAttributes(new Dictionary<string,object> {
            ["eval_tags"]=_config.Get("EVAL_TAGS"),["metadata"]=_config.Get("METADATA"),["semantic_convention"]="fi"
        })
        // Defaults must not erase even unfamiliar application attributes.
        .AddAttributes(resource.Attributes)
        .AddAttributes(new Dictionary<string,object> {
            ["project_name"]=_config.Get("PROJECT_NAME"),["project_type"]=_config.Get("PROJECT_TYPE"),
            ["project_version_name"]=_config.Get("PROJECT_VERSION_NAME"),["project_version_id"]=_config.Get("PROJECT_VERSION_ID")
        }).Build();
    internal Resource SanitizeResource(Resource resource)
    {
        string[]? secrets;
        try { secrets=KnownSecrets.ForExport(_config); } catch { secrets=null; }
        return ExportSnapshot.SanitizeResource(resource,
            (key,value)=>secrets==null?throw new InvalidDataException("Credential privacy unavailable"):Mask(key,WirePrivacy.Scrub(value,secrets,path:key)),
            text=>secrets==null?throw new InvalidDataException("Credential privacy unavailable"):WirePrivacy.ScrubString(text,secrets));
    }
    private static string EventKey(string name,string key) => name switch {
        "gen_ai.user.message" or "gen_ai.system.message" or "gen_ai.tool.message" => "gen_ai.input.messages.0.message.content."+key,
        "gen_ai.assistant.message" or "gen_ai.choice" => "gen_ai.output.messages.0.message.content."+key,
        _=>key
    };
    internal object? Mask(string key,object value,string? operation=null)
    {
        if(WirePrivacy.CredentialKey(key)||IsSensitiveKey(key)) return TraceConfig.RedactedValue;
        bool tool=key.StartsWith("gen_ai.tool.",StringComparison.Ordinal);
        bool nativeOutput=key.StartsWith("gen_ai.tool.call.result",StringComparison.Ordinal)||key.StartsWith("gen_ai.tool.output",StringComparison.Ordinal);
        bool toolInput=key.StartsWith("gen_ai.tool.call.arguments",StringComparison.Ordinal)||key.StartsWith("gen_ai.tool.parameters",StringComparison.Ordinal)||
            key.StartsWith("gen_ai.tool.definitions",StringComparison.Ordinal)||key.StartsWith("gen_ai.tool.input",StringComparison.Ordinal)||
            key is "gen_ai.tool.name" or "gen_ai.tool.description" or "gen_ai.tool.type" or "gen_ai.tool.call.id";
        bool nativeInput=key.StartsWith("gen_ai.system_instructions",StringComparison.Ordinal)||toolInput;
        bool input=key.StartsWith("gen_ai.input.",StringComparison.Ordinal)||key.StartsWith("input.",StringComparison.Ordinal)||nativeInput||key.StartsWith("gen_ai.tool.definitions",StringComparison.Ordinal);
        bool output=key.StartsWith("gen_ai.output.",StringComparison.Ordinal)||key.StartsWith("output.",StringComparison.Ordinal)||nativeOutput;
        bool transcript=key=="gen_ai.audio.transcript";
        bool inputText=_mask.HideInputs||_mask.HideInputText;
        bool outputText=_mask.HideOutputs||_mask.HideOutputText;
        // A whole native tool-call envelope can contain both arguments and results.
        // Unclassified tool payloads fail conservatively when either side is hidden.
        if(tool&&!toolInput&&!nativeOutput&&(inputText||outputText||_mask.HideInputMessages||_mask.HideOutputMessages)) return TraceConfig.RedactedValue;
        if(key.StartsWith("gen_ai.request.parameters",StringComparison.Ordinal)&&_mask.HideLlmInvocationParameters) return TraceConfig.RedactedValue;
        bool textField=nativeInput||nativeOutput||key.Contains(".message.content.",StringComparison.Ordinal)||key is "gen_ai.input.messages" or "gen_ai.output.messages" || key.EndsWith(".text",StringComparison.Ordinal)||key.EndsWith(".content",StringComparison.Ordinal)||key.EndsWith(".function.arguments",StringComparison.Ordinal);
        bool transcriptHidden=operation=="text_to_speech"?inputText:operation is "transcription" or "translation"?outputText:inputText||outputText;
        if((input&&(_mask.HideInputs||_mask.HideInputMessages))||(output&&(_mask.HideOutputs||_mask.HideOutputMessages))||
            (input&&_mask.HideInputText&&textField)||(output&&_mask.HideOutputText&&textField)||
            (transcript&&transcriptHidden)||(key=="input.value"&&(inputText||_mask.HideInputMessages||_mask.HideLlmInvocationParameters))||(key=="output.value"&&(outputText||_mask.HideOutputMessages))||
            ((key=="gen_ai.request.parameters"||key.StartsWith("gen_ai.tool.definitions",StringComparison.Ordinal))&&(inputText||_mask.HideInputMessages))) return TraceConfig.RedactedValue;
        // TraceConfig's decision depends only on the key. Never serialize a value
        // just to make that decision (NaN, cycles and custom serializers are valid inputs).
        if(_mask.Mask(key,"")==TraceConfig.RedactedValue) return TraceConfig.RedactedValue;
        if(_mask.HideInputImages && (key.Contains("image",StringComparison.OrdinalIgnoreCase)||key.Contains("file_uri",StringComparison.OrdinalIgnoreCase)||key.Contains("fileUri",StringComparison.Ordinal)||key.Contains("file_url",StringComparison.OrdinalIgnoreCase)||key.Contains("file.uri",StringComparison.OrdinalIgnoreCase)||key.Contains("file.url",StringComparison.OrdinalIgnoreCase))) return TraceConfig.RedactedValue;
        value=WirePrivacy.Scrub(value,path:key);
        if(value is string str)
        {
            str=_config.RedactSecrets(str);
            if(_mask.HideInputImages && WireCore.Parse(System.Text.Encoding.UTF8.GetBytes(str)) is System.Text.Json.Nodes.JsonNode node && node is System.Text.Json.Nodes.JsonObject or System.Text.Json.Nodes.JsonArray)
            { HideImages(node); str=WireCore.J(node); }
            return str;
        }
        if(value is string[] strings) return strings.Select(item=>item==null?null:(string?)Mask(key,item,operation)).ToArray();
        if(value is object[] objects) return objects.Select(item=>item==null?null:Mask(key,item,operation)).ToArray();
        return value;
    }
    private static bool IsSensitiveKey(string key)
    {
        // Embedding suppression is a separate telemetry policy, not credential or
        // media recognition. S6 names and media are handled only by PrivacyCatalog.
        var normalized=PrivacyCatalog.Normalize(key);
        return normalized is "embedding" or "embeddings" ||
            normalized.Contains("embedding",StringComparison.Ordinal)&&normalized.Contains("vector",StringComparison.Ordinal);
    }
    private static void HideImages(System.Text.Json.Nodes.JsonNode node)
    {
        int remaining=PrivacyCatalog.MaxNodes;
        HideImages(node,0,ref remaining);
    }
    private static void HideImages(System.Text.Json.Nodes.JsonNode node,int depth,ref int remaining)
    {
        if(depth>PrivacyCatalog.MaxDepth || --remaining<0) throw new InvalidDataException("Image privacy budget");
        if(node is System.Text.Json.Nodes.JsonObject obj)
        {
            // Anthropic identifies image blocks by their type, not by the source
            // property's name. Hide the whole payload, including source.url.
            bool imageBlock=WireCore.S(obj["type"]) is "image" or "input_image" or "image_url" ||
                (WireCore.S(obj["mimeType"]??obj["mime_type"])?.StartsWith("image/",StringComparison.OrdinalIgnoreCase)==true);
            foreach(var pair in obj.ToArray())
                if(imageBlock && pair.Key!="type" || pair.Key is "image" or "images" or "image_url" or "input_image" or "inlineData" or "inline_data" or "fileUri" or "file_uri" or "file_url" or "fileData" or "file_data") obj[pair.Key]=TraceConfig.RedactedValue;
                else if(pair.Value!=null)
                {
                    if(WireCore.S(pair.Value) is string text && WireCore.Parse(System.Text.Encoding.UTF8.GetBytes(text)) is System.Text.Json.Nodes.JsonNode nested && nested is System.Text.Json.Nodes.JsonObject or System.Text.Json.Nodes.JsonArray)
                    { HideImages(nested,depth+1,ref remaining); obj[pair.Key]=WireCore.J(nested); }
                    else HideImages(pair.Value,depth+1,ref remaining);
                }
        }
        if(node is System.Text.Json.Nodes.JsonArray arr)
            for(int i=0;i<arr.Count;i++) if(arr[i] is {} item)
            {
                if(WireCore.S(item) is string text && WireCore.Parse(System.Text.Encoding.UTF8.GetBytes(text)) is System.Text.Json.Nodes.JsonNode nested && nested is System.Text.Json.Nodes.JsonObject or System.Text.Json.Nodes.JsonArray)
                { HideImages(nested,depth+1,ref remaining); arr[i]=WireCore.J(nested); }
                else HideImages(item,depth+1,ref remaining);
            }
    }
}
