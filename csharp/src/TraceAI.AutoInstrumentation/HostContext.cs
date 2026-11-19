using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using FIInstrumentation.Context;
using TraceAI.AutoInstrumentation.Wire;

namespace TraceAI.AutoInstrumentation;

/// <summary>Only framework values cross the hook's private dependency boundary. The host's
/// suppression getters and FI context fields are resolved once, and again only after an
/// OpenTelemetry or FIInstrumentation assembly loads into the default context; lookups on
/// each activity or request do no reflection and allocate nothing when no context is set.</summary>
internal static class HostContext
{
    private static readonly (string Field,string Key,bool Json)[] ContextFields = [
        ("s_sessionId","session.id",false),("s_userId","user.id",false),("s_metadata","metadata",true),
        ("s_tags","tag.tags",true),("s_promptTemplate","gen_ai.prompt.template.name",false),
        ("s_promptTemplateLabel","gen_ai.prompt.template.label",false),("s_promptTemplateVersion","gen_ai.prompt.template.version",false),
        ("s_promptTemplateVariables","gen_ai.prompt.template.variables",true)
    ];
    internal static IEnumerable<string> FieldNames=>ContextFields.Select(entry=>entry.Field);
    private static readonly KeyValuePair<string,string>[] None=[];
    private sealed class Accessors(Func<bool>[] suppressed,Func<object?>?[][] context)
    {
        internal readonly Func<bool>[] Suppressed=suppressed;
        internal readonly Func<object?>?[][] Context=context;
    }
    private static readonly bool Isolated=AssemblyLoadContext.GetLoadContext(typeof(HostContext).Assembly)!=AssemblyLoadContext.Default;
    private static Accessors s_accessors;
    private static int s_stale;
    static HostContext()
    {
        // Subscribe before the first scan, so no load can fall between the two.
        if(Isolated)
            try { AppDomain.CurrentDomain.AssemblyLoad+=(_,args)=> { try { if(Relevant(args.LoadedAssembly)) Volatile.Write(ref s_stale,1); } catch { } }; }
            catch { }
        s_accessors=Resolve();
    }
    private static bool Relevant(Assembly assembly)=>AssemblyLoadContext.GetLoadContext(assembly)==AssemblyLoadContext.Default &&
        assembly.GetName().Name is "OpenTelemetry" or "FIInstrumentation";
    private static Accessors Current
    {
        get
        {
            if(Volatile.Read(ref s_stale)!=0 && Interlocked.Exchange(ref s_stale,0)!=0) Volatile.Write(ref s_accessors,Resolve());
            return Volatile.Read(ref s_accessors);
        }
    }
    private static Accessors Resolve()
    {
        var suppressed=new List<Func<bool>>();
        var context=new List<Func<object?>?[]> { Getters(typeof(ContextAttributes)) };
        if(Isolated)
            foreach(var assembly in AssemblyLoadContext.Default.Assemblies)
            {
                try
                {
                    switch(assembly.GetName().Name)
                    {
                        case "OpenTelemetry":
                            if(Getter(assembly.GetType("OpenTelemetry.Sdk"),"SuppressInstrumentation") is {} sdk) suppressed.Add(sdk);
                            break;
                        case "FIInstrumentation":
                            if(Getter(assembly.GetType("FIInstrumentation.Context.SuppressTracing"),"IsSuppressed") is {} legacy) suppressed.Add(legacy);
                            if(assembly.GetType("FIInstrumentation.Context.ContextAttributes") is {} type) context.Add(Getters(type));
                            break;
                    }
                }
                catch { }
            }
        return new(suppressed.ToArray(),context.ToArray());
    }
    private static Func<bool>? Getter(Type? type,string name)
    {
        try { return type?.GetProperty(name,BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic)?.GetGetMethod(true)?.CreateDelegate<Func<bool>>(); }
        catch { return null; }
    }
    private static Func<object?>?[] Getters(Type type)
    {
        var getters=new Func<object?>?[ContextFields.Length];
        for(int i=0;i<getters.Length;i++) getters[i]=ContextGetter(type,ContextFields[i].Field);
        return getters;
    }
    // Only the SDK's known AsyncLocal shapes, whose framework types every load context shares.
    // Unrecognized layouts are skipped, fail-open.
    private static Func<object?>? ContextGetter(Type type,string field)
    {
        try
        {
            object? local=type.GetField(field,BindingFlags.Static|BindingFlags.NonPublic)?.GetValue(null);
            if(local is AsyncLocal<string> text) return ()=>text.Value;
            if(local is AsyncLocal<Dictionary<string,object>> map) return ()=>map.Value;
            if(local is AsyncLocal<List<string>> list) return ()=>list.Value;
        }
        catch { }
        return null;
    }
    internal static bool Suppressed
    {
        get
        {
            foreach(var getter in Current.Suppressed)
                try { if(getter()) return true; } catch { }
            return false;
        }
    }
    internal static KeyValuePair<string,string>[] Attributes()
    {
        List<KeyValuePair<string,string>>? found=null;
        foreach(var getters in Current.Context)
            for(int i=0;i<getters.Length;i++)
            {
                if(getters[i] is not {} getter) continue;
                string? text=null;
                try
                {
                    // Do not call GetAttributesFromContext: its general JSON serializer
                    // executes arbitrary metadata getters synchronously before transport.
                    var value=getter();
                    if(value!=null) text=ContextFields[i].Json?Serialize(value):value as string;
                }
                catch { }
                if(string.IsNullOrEmpty(text)) continue;
                found??=new List<KeyValuePair<string,string>>();
                found.Add(new(ContextFields[i].Key,text));
            }
        return found?.ToArray()??None;
    }
    internal static string Serialize(object value)
    {
        int remaining=WirePrivacy.MaxAttributeNodes,characters=1024*1024;
        object? Copy(object? item,int depth)
        {
            if(depth>32 || --remaining<0) throw new InvalidDataException("Context budget");
            if(item is string text)
            { if((characters-=text.Length)<0) throw new InvalidDataException("Context text budget"); return text; }
            if(item is Dictionary<string,object> dictionary)
            {
                if(dictionary.Count>remaining) throw new InvalidDataException("Context dictionary budget");
                var result=new Dictionary<string,object?>();
                foreach(var pair in dictionary)
                {
                    if((characters-=pair.Key.Length)<0) throw new InvalidDataException("Context key budget");
                    result[pair.Key]=Copy(pair.Value,depth+1);
                }
                return result;
            }
            if(item is List<string> list)
            {
                if(list.Count>remaining) throw new InvalidDataException("Context list budget");
                return list.Select(v=>Copy(v,depth+1)).ToArray();
            }
            if(item is object[] array)
            {
                if(array.Length>remaining) throw new InvalidDataException("Context array budget");
                return array.Select(v=>Copy(v,depth+1)).ToArray();
            }
            return item==null?null:WirePrivacy.Scrub(item);
        }
        try { return JsonSerializer.Serialize(Copy(value,0)); }
        catch { return WirePrivacy.Redacted; }
    }
}
