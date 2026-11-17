using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using FIInstrumentation.Context;
using OpenTelemetry;
using OpenTelemetry.Exporter;

namespace TraceAI.AutoInstrumentation;

/// <summary>Every non-public runtime and pinned OpenTelemetry member the agent reads or
/// writes, resolved once. Agent start reports any missing member by name in one warning;
/// the paths that need it then omit telemetry instead of failing silently on each span.</summary>
internal static class RuntimeLayout
{
    private const BindingFlags Instance=BindingFlags.Instance|BindingFlags.NonPublic;
    /// <summary>Activity fields the export snapshot replaces on its detached copy.</summary>
    internal static readonly string[] ActivityFields=["_tags","_events","_links","_baggage","_customProperties","<Parent>k__BackingField",
        "_parentId","<Source>k__BackingField","_traceState","<OperationName>k__BackingField"];
    private static readonly ConcurrentDictionary<(Type Type,string Name),FieldInfo?> Fields=new();
    /// <summary>Resolved once per type and name. Callers pass Activity itself for any
    /// Activity, so the private fields also resolve for application subclasses.</summary>
    internal static FieldInfo? Field(Type type,string name)=>Fields.GetOrAdd((type,name),static key=>key.Type.GetField(key.Name,Instance));
    internal static readonly MethodInfo? CloneMethod=typeof(object).GetMethod("MemberwiseClone",Instance);
    // OpenTelemetry's OTLP exporter: its resource (DiagnosticSource 10 export path) and resolved limits.
    internal static readonly PropertyInfo? OtlpResource=typeof(OtlpTraceExporter).GetProperty("Resource",Instance);
    internal static readonly FieldInfo? OtlpLimits=typeof(OtlpTraceExporter).GetField("sdkLimitOptions",Instance);
    internal static readonly PropertyInfo? OtlpSpanAttributeLimit=OtlpLimits?.FieldType.GetProperty("SpanAttributeCountLimit",BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic);
    internal static readonly PropertyInfo? ExporterProvider=typeof(BaseExporter<Activity>).GetProperty(nameof(BaseExporter<Activity>.ParentProvider));
    // The HTTP client's own response stream and buffered request content.
    internal static readonly Type? ResponseContent=typeof(HttpClient).Assembly.GetType("System.Net.Http.HttpConnectionResponseContent");
    internal static readonly FieldInfo? ResponseStream=ResponseContent?.GetField("_stream",Instance);
    internal static readonly FieldInfo? ByteArrayBuffer=typeof(ByteArrayContent).GetField("_content",Instance),
        ByteArrayOffset=typeof(ByteArrayContent).GetField("_offset",Instance),
        ByteArrayCount=typeof(ByteArrayContent).GetField("_count",Instance),
        MemoryBuffer=typeof(ReadOnlyMemoryContent).GetField("_content",Instance);
    // The stored exception text, never an application's Message override.
    internal static readonly FieldInfo? ExceptionMessage=typeof(Exception).GetField("_message",Instance);
    internal static string[] Missing()
    {
        var missing=new List<string>();
        void Need(bool present,string name) { if(!present) missing.Add(name); }
        foreach(var name in ActivityFields) Need(Field(typeof(Activity),name)!=null,"Activity."+name);
        var links=Field(typeof(Activity),"_links")?.FieldType;
        Need(links?.GetMethod("Add",Instance|BindingFlags.Public)!=null && links?.GetConstructor(Instance|BindingFlags.Public,Type.EmptyTypes)!=null,"Activity._links list");
        Need(Field(typeof(ActivitySource),"<Name>k__BackingField")!=null,"ActivitySource.<Name>k__BackingField");
        Need(Field(typeof(ActivitySource),"<Version>k__BackingField")!=null,"ActivitySource.<Version>k__BackingField");
        // Only DiagnosticSource versions that have these properties have their fields.
        foreach(var property in new[] { "Tags","TelemetrySchemaUrl" })
            if(typeof(ActivitySource).GetProperty(property)!=null)
                Need(Field(typeof(ActivitySource),"<"+property+">k__BackingField")!=null,"ActivitySource.<"+property+">k__BackingField");
        Need(CloneMethod!=null,"Object.MemberwiseClone");
        Need(OtlpResource?.GetSetMethod(true)!=null,"OtlpTraceExporter.Resource setter");
        Need(OtlpSpanAttributeLimit?.GetGetMethod(true)!=null,"OtlpTraceExporter.sdkLimitOptions.SpanAttributeCountLimit");
        Need(ExporterProvider?.GetSetMethod(true)!=null,"BaseExporter.ParentProvider setter");
        Need(ResponseStream!=null && typeof(Stream).IsAssignableFrom(ResponseStream.FieldType),"HttpConnectionResponseContent._stream");
        Need(ByteArrayBuffer?.FieldType==typeof(byte[]) && ByteArrayOffset?.FieldType==typeof(int) && ByteArrayCount?.FieldType==typeof(int),"ByteArrayContent buffer fields");
        Need(MemoryBuffer?.FieldType==typeof(ReadOnlyMemory<byte>),"ReadOnlyMemoryContent._content");
        Need(ExceptionMessage?.FieldType==typeof(string),"Exception._message");
        Need(InstrumentationSuppression.LegacyResolved,"SuppressTracing.IsSuppressed");
        foreach(var field in HostContext.FieldNames)
            Need(typeof(ContextAttributes).GetField(field,BindingFlags.Static|BindingFlags.NonPublic)!=null,"ContextAttributes."+field);
        return missing.ToArray();
    }
    /// <summary>One named warning at agent start. Never throws.</summary>
    internal static void Check()
    {
        try
        {
            var missing=Missing();
            if(missing.Length>0)
                TraceAIAuto.Warn("runtime-layout","Unsupported runtime layout ("+string.Join(", ",missing)+"); telemetry that needs these members is omitted.");
        }
        catch { TraceAIAuto.Warn("runtime-layout","Runtime layout check failed; some telemetry may be omitted."); }
    }
}
