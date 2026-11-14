using System.Reflection;
using FIInstrumentation;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>A hide switch someone set must never silently do nothing. For every FI_HIDE_*
/// variable and its futureagi.toml setting: true, 1, yes and on hide; false, 0, no and off
/// show; a blank value is not set; any other value hides, with one warning that names the
/// variable and never prints the value. Other switches keep their own parsing.</summary>
public class HideFlagValueTests : InitIsolation
{
    private const string Text="user said PRIVATE_HIDE_FLAG_TEXT";
    private static readonly Dictionary<string,Func<TraceConfig,bool>> Switches=new()
    {
        ["FI_HIDE_INPUTS"]=mask=>mask.HideInputs,
        ["FI_HIDE_OUTPUTS"]=mask=>mask.HideOutputs,
        ["FI_HIDE_INPUT_MESSAGES"]=mask=>mask.HideInputMessages,
        ["FI_HIDE_OUTPUT_MESSAGES"]=mask=>mask.HideOutputMessages,
        ["FI_HIDE_INPUT_TEXT"]=mask=>mask.HideInputText,
        ["FI_HIDE_OUTPUT_TEXT"]=mask=>mask.HideOutputText,
        ["FI_HIDE_INPUT_IMAGES"]=mask=>mask.HideInputImages,
        ["FI_HIDE_EMBEDDING_VECTORS"]=mask=>mask.HideEmbeddingVectors,
        ["FI_HIDE_LLM_INVOCATION_PARAMETERS"]=mask=>mask.HideLlmInvocationParameters,
        ["FI_HIDE_ERROR_DETAILS"]=mask=>mask.HideErrorDetails,
    };
    private static readonly string[] OtherSwitches=["FI_AUTO_DEBUG","FI_WIRE_CAPTURE"];
    public static IEnumerable<object[]> SwitchNames=>Switches.Keys.Select(name=>new object[] { name });
    private sealed record Reading(AutoConfig Config,AutoProcessor Processor,TraceConfig Mask,string Warnings)
    {
        internal int WarningsNaming(string name)=>Warnings.Split('\n').Count(line=>line.Contains(name,StringComparison.Ordinal));
    }
    /// <summary>Reads the configuration twice, as one process can, with only the given
    /// switches set, and returns the first reading with everything printed as a warning.</summary>
    private static Reading Read((string Name,string? Value)[] environment,string? file=null,TraceConfig? supplied=null)
    {
        var values=Switches.Keys.Concat(OtherSwitches).ToDictionary(name=>name,_=>(string?)null);
        foreach(var (name,value) in environment) values[name]=value;
        var previous=values.Keys.ToDictionary(name=>name,name=>Environment.GetEnvironmentVariable(name));
        string? path=file==null?null:Path.Combine(Path.GetTempPath(),"traceai-hide-flags-"+Guid.NewGuid()+".toml");
        var error=Console.Error; var captured=new StringWriter();
        try
        {
            if(path!=null) File.WriteAllText(path,file+"\n");
            AgentStartupTests.ClearWarnings(); Console.SetError(captured);
            foreach(var pair in values) Environment.SetEnvironmentVariable(pair.Key,pair.Value);
            AutoConfig Config()=>new(new AutoOptions { ApiKey="hide-flag-key",SecretKey="hide-flag-secret",Exporter=new MemoryExporter(),
                ConfigFile=path??Path.Combine(WireFixtureTests.Repo,"csharp","absent.toml"),TraceConfig=supplied });
            var config=Config(); var processor=new AutoProcessor(config);
            // A second read in the same process must not print a warning again.
            _=new AutoProcessor(Config());
            var mask=(TraceConfig)typeof(AutoProcessor).GetField("_mask",BindingFlags.Instance|BindingFlags.NonPublic)!.GetValue(processor)!;
            return new Reading(config,processor,mask,captured.ToString());
        }
        finally
        {
            Console.SetError(error);
            foreach(var pair in previous) Environment.SetEnvironmentVariable(pair.Key,pair.Value);
            if(path!=null) File.Delete(path);
        }
    }
    [Theory]
    [InlineData("on")] [InlineData("yes")] [InlineData("1")] [InlineData(" TRUE ")] [InlineData("On")] [InlineData("true")]
    public void TrueValuesHide(string value)
    {
        var reading=Read([("FI_HIDE_INPUTS",value)]);
        Assert.True(reading.Mask.HideInputs);
        Assert.Equal(TraceConfig.RedactedValue,reading.Processor.Mask("input.value",Text));
        Assert.Equal(0,reading.WarningsNaming("FI_HIDE_INPUTS"));
    }
    [Theory]
    [InlineData("off")] [InlineData("no")] [InlineData("0")] [InlineData("false")] [InlineData(" OFF ")]
    public void FalseValuesShow(string value)
    {
        var reading=Read([("FI_HIDE_INPUTS",value)]);
        Assert.False(reading.Mask.HideInputs);
        Assert.NotEqual(TraceConfig.RedactedValue,reading.Processor.Mask("input.value",Text));
        Assert.Equal(0,reading.WarningsNaming("FI_HIDE_INPUTS"));
    }
    [Theory]
    [InlineData("")] [InlineData("   ")]
    public void BlankValueIsNotSet(string value)
    {
        var unset=Read([("FI_HIDE_INPUTS",value)]);
        Assert.False(unset.Mask.HideInputs); Assert.Equal(0,unset.WarningsNaming("FI_HIDE_INPUTS"));
        // A blank environment value does not switch off what the config file hides.
        var file=Read([("FI_HIDE_INPUTS",value)],"hide_inputs = true");
        Assert.True(file.Mask.HideInputs); Assert.Equal(0,file.WarningsNaming("FI_HIDE_INPUTS"));
    }
    [Theory]
    [MemberData(nameof(SwitchNames))]
    public void EverySwitchReadsOnAndOff(string name)
    {
        var on=Read([(name,"on")]);
        Assert.True(Switches[name](on.Mask)); Assert.Equal(0,on.WarningsNaming(name));
        var off=Read([(name,"off")]);
        Assert.False(Switches[name](off.Mask)); Assert.Equal(0,off.WarningsNaming(name));
    }
    [Theory]
    [MemberData(nameof(SwitchNames))]
    public void UnrecognisedValueHidesWithOneWarningThatNamesTheVariableAndNotTheValue(string name)
    {
        var reading=Read([(name,"maybe")]);
        Assert.True(Switches[name](reading.Mask));
        Assert.Equal(1,reading.WarningsNaming(name));
        Assert.DoesNotContain("maybe",reading.Warnings);
    }
    [Theory]
    // A recognised error-detail value wins over the content switches.
    [InlineData("true","off",false,0)]
    [InlineData("false","on",true,0)]
    // A blank one follows them.
    [InlineData("true","  ",true,0)]
    [InlineData("false","  ",false,0)]
    // Any other value hides error details, with one warning.
    [InlineData("false","enabled",true,1)]
    public void ErrorDetailsFollowTheContentSwitchesUnlessSetToARecognisedValue(string inputs,string details,bool hidden,int warnings)
    {
        var reading=Read([("FI_HIDE_INPUTS",inputs),("FI_HIDE_ERROR_DETAILS",details)]);
        Assert.Equal(hidden,reading.Mask.HideErrorDetails);
        Assert.Equal(warnings,reading.WarningsNaming("FI_HIDE_ERROR_DETAILS"));
        Assert.DoesNotContain("enabled",reading.Warnings);
    }
    [Theory]
    [InlineData("hide_inputs = \"on\"",true,0)]
    [InlineData("hide_inputs = \" Yes \"",true,0)]
    [InlineData("hide_inputs = 1",true,0)]
    [InlineData("hide_inputs = true",true,0)]
    [InlineData("hide_inputs = \"off\"",false,0)]
    [InlineData("hide_inputs = 0",false,0)]
    [InlineData("hide_inputs = false",false,0)]
    [InlineData("hide_inputs = \"\"",false,0)]
    [InlineData("hide_inputs = \"maybe\"",true,1)]
    [InlineData("hide_inputs = 2",true,1)]
    public void ConfigFileValuesFollowTheSameRule(string settings,bool hidden,int warnings)
    {
        var reading=Read([],settings);
        Assert.Equal(hidden,reading.Mask.HideInputs);
        Assert.Equal(warnings,reading.WarningsNaming("FI_HIDE_INPUTS"));
        Assert.DoesNotContain("maybe",reading.Warnings);
    }
    [Theory]
    [InlineData("yes",true)] [InlineData(" TRUE ",true)] [InlineData("1",true)]
    [InlineData("on",false)] [InlineData("maybe",false)] [InlineData("off",false)]
    public void OtherSwitchesKeepTheirOwnParsing(string value,bool enabled)
    {
        var reading=Read([("FI_AUTO_DEBUG",value),("FI_WIRE_CAPTURE",value)]);
        Assert.Equal(enabled,reading.Config.Bool("AUTO_DEBUG")); Assert.Equal(enabled,reading.Config.Bool("WIRE_CAPTURE"));
        Assert.Equal(0,reading.WarningsNaming("FI_AUTO_DEBUG")); Assert.Equal(0,reading.WarningsNaming("FI_WIRE_CAPTURE"));
        Assert.False(reading.Mask.HideInputs); Assert.False(reading.Mask.HideErrorDetails);
    }
    [Fact]
    public void SuppliedTraceConfigIsUsedAsGivenWithoutAWarning()
    {
        var reading=Read([("FI_HIDE_INPUTS","maybe")],supplied:new TraceConfig { HideOutputs=true });
        Assert.False(reading.Mask.HideInputs); Assert.True(reading.Mask.HideOutputs);
        Assert.Equal(0,reading.WarningsNaming("FI_HIDE_INPUTS"));
    }
}
