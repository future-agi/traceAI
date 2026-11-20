using System.Globalization;
using System.Text.Json;
using OpenTelemetry;
using System.Diagnostics;

namespace TraceAI.AutoInstrumentation;

/// <summary>FI_BASE_URL is set but blank: the agent does not start.</summary>
internal sealed class BlankBaseUrlException : ArgumentException
{
    internal BlankBaseUrlException() : base("FI_BASE_URL is set but empty") { }
}

/// <summary>Unset values inherit environment, then futureagi.toml, then defaults.</summary>
public sealed class AutoOptions
{
    public string? ApiKey { get; set; }
    public string? SecretKey { get; set; }
    public string? BaseUrl { get; set; }
    public string? ProjectName { get; set; }
    public string? ProjectType { get; set; }
    public string? ProjectVersionName { get; set; }
    public string? ProjectVersionId { get; set; }
    public string[]? EvalTags { get; set; }
    public Dictionary<string,object>? Metadata { get; set; }
    public string? ConfigFile { get; set; }
    public string[]? Instrumentations { get; set; }
    public string[]? DisabledInstrumentations { get; set; }
    public bool? WireCapture { get; set; }
    public int? WireMaxBodyBytes { get; set; }
    public double? SampleRate { get; set; }
    public bool? Debug { get; set; }
    /// <summary>Optional exporter for embedding or tests; otherwise OTLP HTTP/protobuf is used.</summary>
    public BaseExporter<Activity>? Exporter { get; set; }
    /// <summary>Optional caller-owned provider. Configure its source subscriptions
    /// before Build (TraceAI.AutoInstrumentation for wire and the desired library
    /// sources). Its sampler, resources, processors and lifetime remain host-owned.</summary>
    public OpenTelemetry.Trace.TracerProvider? Provider { get; set; }
    public FIInstrumentation.TraceConfig? TraceConfig { get; set; }
    internal AutoOptions Copy()
    {
        var copy=(AutoOptions)MemberwiseClone();
        copy.Instrumentations=Instrumentations?.ToArray(); copy.DisabledInstrumentations=DisabledInstrumentations?.ToArray();
        copy.EvalTags=EvalTags?.ToArray(); copy.Metadata=Metadata==null?null:new(Metadata);
        return copy;
    }
}

internal sealed class AutoConfig
{
    internal readonly Dictionary<string, string> Values;
    internal readonly AutoOptions Options;
    internal readonly bool HasSampleRate;
    // Authentication chooses one complete pair, but privacy covers every FI value
    // supplied at initialization, including incomplete and overridden sources.
    internal readonly string[] CredentialValues;
    internal AutoConfig(AutoOptions options)
    {
        Options = options;
        // A blank FI_CONFIG_FILE names no file, so none is read; only an unset one falls back to ./futureagi.toml.
        Values = ReadFile(options.ConfigFile ?? Environment.GetEnvironmentVariable("FI_CONFIG_FILE") ?? Path.Combine(Environment.CurrentDirectory,"futureagi.toml"));
        var fileApi=Values.GetValueOrDefault("FI_API_KEY"); var fileSecret=Values.GetValueOrDefault("FI_SECRET_KEY");
        bool blankBaseUrl=false;
        foreach (System.Collections.DictionaryEntry pair in Environment.GetEnvironmentVariables())
            if (pair.Key is string key && (key.StartsWith("FI_") || key == "OTEL_SDK_DISABLED"))
            {
                var value = pair.Value?.ToString() ?? "";
                // A blank value counts as not set, so the file's value or the default applies, except where not set is not the
                // cautious reading: a blank FI_CONFIG_FILE stays blank (no file is read) and a blank FI_WIRE_CAPTURE is off.
                // A blank FI_BASE_URL is checked below, once the explicit options are known.
                if (key.StartsWith("FI_", StringComparison.Ordinal) && string.IsNullOrWhiteSpace(value))
                {
                    if (key == "FI_CONFIG_FILE") Values[key] = "";
                    else if (key == "FI_WIRE_CAPTURE") Values[key] = "false";
                    else if (key == "FI_BASE_URL") blankBaseUrl = true;
                    continue;
                }
                Values[key] = value;
            }
        Set("CONFIG_FILE",options.ConfigFile); Values.TryAdd("FI_CONFIG_FILE",Path.Combine(Environment.CurrentDirectory,"futureagi.toml"));
        // Resolve each source's credential pair atomically; never combine halves.
        Values.Remove("FI_API_KEY"); Values.Remove("FI_SECRET_KEY");
        (string? Api,string? Secret)? selected=null;
        var sources=new[] {
            (Name:"explicit arguments",Api:options.ApiKey,Secret:options.SecretKey),
            (Name:"environment",Api:Environment.GetEnvironmentVariable("FI_API_KEY"),Secret:Environment.GetEnvironmentVariable("FI_SECRET_KEY")),
            (Name:"config file",Api:fileApi,Secret:fileSecret) };
        CredentialValues=sources.SelectMany(source=>new[] { source.Api,source.Secret })
            .Where(value=>!string.IsNullOrWhiteSpace(value)).Select(value=>value!).Distinct(StringComparer.Ordinal).ToArray();
        foreach(var source in sources)
        {
            bool api=!string.IsNullOrWhiteSpace(source.Api),secret=!string.IsNullOrWhiteSpace(source.Secret);
            if(api!=secret) TraceAIAuto.Warn("credentials-pair-"+source.Name,"Ignoring incomplete FI credentials in "+source.Name+"; missing "+(api?"FI_SECRET_KEY":"FI_API_KEY")+".");
            else if(api&&selected==null) selected=(source.Api,source.Secret);
        }
        if(selected is {} resolvedPair) { Set("API_KEY",resolvedPair.Api); Set("SECRET_KEY",resolvedPair.Secret); }
        Set("BASE_URL",options.BaseUrl);
        Set("PROJECT_NAME",options.ProjectName); Set("PROJECT_TYPE",options.ProjectType);
        Set("PROJECT_VERSION_NAME",options.ProjectVersionName); Set("PROJECT_VERSION_ID",options.ProjectVersionId);
        Set("EVAL_TAGS",options.EvalTags==null?null:JsonSerializer.Serialize(options.EvalTags));
        Set("METADATA",options.Metadata==null?null:HostContext.Serialize(options.Metadata));
        Set("INSTRUMENTATIONS",options.Instrumentations == null ? null : string.Join(',',options.Instrumentations));
        Set("DISABLED_INSTRUMENTATIONS",options.DisabledInstrumentations == null ? null : string.Join(',',options.DisabledInstrumentations));
        Set("WIRE_CAPTURE",options.WireCapture?.ToString()); Set("WIRE_MAX_BODY_BYTES",options.WireMaxBodyBytes?.ToString(CultureInfo.InvariantCulture));
        Set("SAMPLE_RATE",options.SampleRate?.ToString(CultureInfo.InvariantCulture)); Set("AUTO_DEBUG",options.Debug?.ToString());
        // A blank FI_BASE_URL stops the agent, whatever the file or the default says: the collector it names is unknown.
        // It does not apply when the caller gave the URL, or supplied the exporter or provider that sends the spans.
        if(blankBaseUrl && options.BaseUrl==null && options.Exporter==null && options.Provider==null && !Disabled) throw new BlankBaseUrlException();
        Values.TryAdd("FI_BASE_URL",FIInstrumentation.Settings.DefaultBaseUrl);
        Values.TryAdd("FI_PROJECT_NAME",DefaultProjectName());
        Values.TryAdd("FI_PROJECT_VERSION_NAME",FIInstrumentation.Settings.DefaultProjectVersionName);
        Values.TryAdd("FI_PROJECT_VERSION_ID",Guid.NewGuid().ToString());
        Values.TryAdd("FI_EVAL_TAGS","[]"); Values.TryAdd("FI_METADATA","{}");
        Values.TryAdd("FI_PROJECT_TYPE","observe"); Values.TryAdd("FI_WIRE_CAPTURE","true"); Values.TryAdd("FI_WIRE_MAX_BODY_BYTES","4194304");
        HasSampleRate=Values.ContainsKey("FI_SAMPLE_RATE");
        Values.TryAdd("FI_SAMPLE_RATE","1.0"); Values.TryAdd("FI_AUTO_DEBUG","false");
        Values.TryAdd("FI_INSTRUMENTATIONS",""); Values.TryAdd("FI_DISABLED_INSTRUMENTATIONS","");
        Values["FI_SAMPLE_RATE"]=SampleRate.ToString(CultureInfo.InvariantCulture);
        Values["FI_WIRE_MAX_BODY_BYTES"]=MaxBody.ToString(CultureInfo.InvariantCulture);
        if (Options.Exporter==null && Options.Provider==null &&
            (!Uri.TryCreate(Get("BASE_URL"),UriKind.Absolute,out var uri) || uri.Scheme is not ("http" or "https") || !string.IsNullOrEmpty(uri.UserInfo))) throw new ArgumentException("Invalid collector URL");
        if(Get("PROJECT_TYPE") is not ("observe" or "experiment"))
        {
            if(Options.Exporter==null && Options.Provider==null) throw new ArgumentException("Invalid project type");
            // Routing metadata must remain valid without disabling a host-owned
            // destination for an unrelated FI configuration error. Never echo it.
            Values["FI_PROJECT_TYPE"]="observe";
            TraceAIAuto.Warn("project-type","Invalid FI_PROJECT_TYPE; using observe for the supplied destination.");
        }
    }
    private void Set(string key,string? value) { if(value!=null) Values["FI_"+key]=value; }
    internal string Get(string key) => Values.GetValueOrDefault("FI_"+key)??"";
    /// <summary>Switches other than the hide switches (FI_WIRE_CAPTURE, FI_AUTO_DEBUG): true,
    /// 1 or yes is on, trimmed and case-insensitive; anything else, including a blank FI_WIRE_CAPTURE, is off.</summary>
    internal bool Bool(string key) => Get(key).Trim().ToLowerInvariant() is "true" or "1" or "yes";
    /// <summary>FI_AUTO_CAPTURE_CONTENT: on when unset or blank, and for true, 1, yes or on; off for false, 0, no or off.
    /// Any other value is off, the cautious reading of a switch someone set, reported with one warning that names only the setting.</summary>
    internal bool CaptureContent
    {
        get
        {
            switch (Get("AUTO_CAPTURE_CONTENT").Trim().ToLowerInvariant())
            {
                case "" or "true" or "1" or "yes" or "on": return true;
                case "false" or "0" or "no" or "off": return false;
                default:
                    TraceAIAuto.Warn("capture-content","FI_AUTO_CAPTURE_CONTENT has a value that is not recognised, so content is not recorded. Use true, 1, yes or on to record it, or false, 0, no or off to keep it off.");
                    return false;
            }
        }
    }
    /// <summary>A hide switch, from FI_HIDE_* or its file setting, trimmed and case-insensitive:
    /// true, 1, yes or on hides; false, 0, no or off shows. Null when it is not set or blank, so
    /// the caller's default applies. Any other value hides, because a hide switch someone set
    /// must never silently do nothing; it is reported with one warning that names the variable,
    /// never the value.</summary>
    internal bool? Hide(string key)
    {
        switch (Get(key).Trim().ToLowerInvariant())
        {
            case "": return null;
            case "true" or "1" or "yes" or "on": return true;
            case "false" or "0" or "no" or "off": return false;
            default:
                TraceAIAuto.Warn("hide-switch-" + key, "FI_" + key + " has a value that is not recognised, so this content is hidden. Use true, 1, yes or on to hide it, or false, 0, no or off to show it.");
                return true;
        }
    }
    internal bool Disabled => Values.GetValueOrDefault("OTEL_SDK_DISABLED")?.ToLowerInvariant() == "true";
    internal bool HasCredentials => !string.IsNullOrWhiteSpace(Get("API_KEY")) && !string.IsNullOrWhiteSpace(Get("SECRET_KEY"));
    internal int MaxBody => int.TryParse(Get("WIRE_MAX_BODY_BYTES"),out var n) && n >= 0 ? n : 4194304;
    internal double SampleRate => double.TryParse(Get("SAMPLE_RATE"),NumberStyles.Float,CultureInfo.InvariantCulture,out var rate) && double.IsFinite(rate) ? Math.Clamp(rate,0,1) : 1;
    internal bool Enabled(string name)
    {
        var allow=Get("INSTRUMENTATIONS").Split(',',StringSplitOptions.TrimEntries|StringSplitOptions.RemoveEmptyEntries);
        var deny=Get("DISABLED_INSTRUMENTATIONS").Split(',',StringSplitOptions.TrimEntries|StringSplitOptions.RemoveEmptyEntries);
        return (allow.Length==0||allow.Contains(name,StringComparer.OrdinalIgnoreCase))&&!deny.Contains(name,StringComparer.OrdinalIgnoreCase)&&(name!="wire"||Bool("WIRE_CAPTURE"));
    }
    internal Dictionary<string,string> Masked() => Values.ToDictionary(p=>p.Key,p=>Wire.WirePrivacy.CredentialKey(p.Key)?"***":RedactSecrets(p.Value));
    /// <summary>Replaces every secret the agent knows: the FI credentials of this configuration and the process-wide registry
    /// (registered secrets and credentials discovered in requests), the same set the Future AGI copy uses. When the registry is
    /// unavailable the text is replaced entirely.</summary>
    internal string[] KnownValues()=>CredentialValues.Concat(KnownSecrets.Snapshot()).Distinct(StringComparer.Ordinal).OrderByDescending(s=>s.Length).ToArray();
    internal string RedactSecrets(string text)
    {
        try { return Wire.WirePrivacy.ScrubString(text,KnownValues()); }
        catch { return Wire.WirePrivacy.Redacted; }
    }
    private static string DefaultProjectName()
    {
        if(Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME") is { Length:>0 } service) return service;
        try { if(new DirectoryInfo(Environment.CurrentDirectory).Name is { Length:>0 } name) return name; } catch { }
        return FIInstrumentation.Settings.DefaultProjectName;
    }
    internal static Dictionary<string,string> ReadFile(string path)
    {
        var result=new Dictionary<string,string>();
        if(!File.Exists(path)) return result;
        foreach(var line in File.ReadLines(path))
        {
            // A '#' inside a quoted string is not a TOML comment.
            bool quoted=false,escaped=false; int end=line.Length;
            for(int i=0;i<line.Length;i++)
            {
                char c=line[i]; if(c=='"'&&!escaped) quoted=!quoted;
                if(c=='#'&&!quoted) { end=i; break; }
                escaped=c=='\\'&&!escaped;
            }
            var text=line[..end].Trim(); if(text.Length==0||text.StartsWith('[')) continue;
            var pair=text.Split('=',2); if(pair.Length!=2) continue;
            var key=pair[0].Trim(); var value=pair[1].Trim(); if(key.Length==0||value.Length==0) continue;
            var name=key=="debug"?"FI_AUTO_DEBUG":"FI_"+key.ToUpperInvariant();
            // A hide switch or the content switch someone set must never be dropped. When its value is outside the flat
            // subset, keep the text (a single-quoted string without its quotes) and let Hide() decide.
            bool hide=name.StartsWith("FI_HIDE_",StringComparison.Ordinal)||name=="FI_AUTO_CAPTURE_CONTENT";
            try
            {
                if(value.StartsWith('[')) value=string.Join(',',JsonSerializer.Deserialize<string[]>(value)??[]);
                else if(value.StartsWith('"')) value=JsonSerializer.Deserialize<string>(value)??"";
                else if(value is not ("true" or "false")&&!double.TryParse(value,NumberStyles.Float,CultureInfo.InvariantCulture,out _))
                {
                    if(!hide) continue;
                    if(value.Length>=2&&value[0]=='\''&&value[^1]=='\'') value=value[1..^1];
                }
                result[name]=value;
            }
            // Other malformed flat-subset values are ignored.
            catch(JsonException) { if(hide) result[name]=value; }
        }
        return result;
    }
}
