using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using FIInstrumentation.Context;
using TraceAI.AutoInstrumentation.Wire;

namespace TraceAI.AutoInstrumentation;

/// <summary>Only framework values cross the hook's private dependency boundary.</summary>
internal static class HostContext
{
    private static IEnumerable<Assembly> OtherAssemblies => AssemblyLoadContext.Default.Assemblies
        .Where(a=>AssemblyLoadContext.GetLoadContext(typeof(HostContext).Assembly)!=AssemblyLoadContext.Default);
    internal static bool Suppressed
    {
        get
        {
            foreach(var assembly in OtherAssemblies)
            {
                try
                {
                    var type=assembly.GetName().Name switch {
                        "OpenTelemetry"=>assembly.GetType("OpenTelemetry.Sdk"),
                        "FIInstrumentation"=>assembly.GetType("FIInstrumentation.Context.SuppressTracing"),
                        _=>null
                    };
                    var property=type?.GetProperty(type.Name=="Sdk"?"SuppressInstrumentation":"IsSuppressed",BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic);
                    if(property?.GetValue(null) is true) return true;
                }
                catch { }
            }
            return false;
        }
    }
    private static readonly (string Field,string Key,bool Json)[] ContextFields = [
        ("s_sessionId","session.id",false),("s_userId","user.id",false),("s_metadata","metadata",true),
        ("s_tags","tag.tags",true),("s_promptTemplate","gen_ai.prompt.template.name",false),
        ("s_promptTemplateLabel","gen_ai.prompt.template.label",false),("s_promptTemplateVersion","gen_ai.prompt.template.version",false),
        ("s_promptTemplateVariables","gen_ai.prompt.template.variables",true)
    ];
    internal static IEnumerable<KeyValuePair<string,string>> Attributes()
    {
        var types=new[] { typeof(ContextAttributes) }.Concat(OtherAssemblies.Where(a=>a.GetName().Name=="FIInstrumentation")
            .Select(a=>a.GetType("FIInstrumentation.Context.ContextAttributes")).OfType<Type>());
        foreach(var type in types) foreach(var entry in ContextFields)
        {
            string? text=null;
            try
            {
                // Do not call GetAttributesFromContext: its general JSON serializer
                // executes arbitrary metadata getters synchronously before transport.
                var local=type.GetField(entry.Field,BindingFlags.Static|BindingFlags.NonPublic)?.GetValue(null);
                if(local==null || !local.GetType().IsGenericType || local.GetType().GetGenericTypeDefinition()!=typeof(AsyncLocal<>)) continue;
                var value=local.GetType().GetProperty("Value")!.GetValue(local);
                if(value==null) continue;
                text=entry.Json?Serialize(value):value as string;
            }
            catch { }
            if(!string.IsNullOrEmpty(text)) yield return new(entry.Key,text);
        }
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
