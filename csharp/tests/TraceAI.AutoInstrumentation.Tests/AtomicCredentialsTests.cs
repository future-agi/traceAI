using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

public class AtomicCredentialsTests
{
    public static IEnumerable<object[]> PairCases()=>from explicitPair in Enumerable.Range(0,4)
        from environmentPair in Enumerable.Range(0,4) from filePair in Enumerable.Range(0,4)
        select new object[] { explicitPair,environmentPair,filePair };
    [Theory]
    [MemberData(nameof(PairCases))]
    public void CredentialSourcesNeverCombineHalves(int explicitPair,int environmentPair,int filePair)
    {
        string? api=Environment.GetEnvironmentVariable("FI_API_KEY"),secret=Environment.GetEnvironmentVariable("FI_SECRET_KEY");
        string path=Path.Combine(Path.GetTempPath(),"traceai-atomic-"+Guid.NewGuid()+".toml");
        string? Value(int source,int bit,string value)=>(source&bit)!=0?value:null;
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY",Value(environmentPair,1,"env-api-key"));
            Environment.SetEnvironmentVariable("FI_SECRET_KEY",Value(environmentPair,2,"env-secret-key"));
            File.WriteAllText(path,((filePair&1)!=0?"api_key = \"file-api-key\"\n":"")+((filePair&2)!=0?"secret_key = \"file-secret-key\"\n":""));
            var config=new AutoConfig(new AutoOptions { ConfigFile=path,ApiKey=Value(explicitPair,1,"explicit-api-key"),SecretKey=Value(explicitPair,2,"explicit-secret-key") });
            string expected=explicitPair==3?"explicit":environmentPair==3?"env":filePair==3?"file":"";
            Assert.Equal(expected.Length>0,config.HasCredentials);
            Assert.Equal(expected.Length>0?expected+"-api-key":"",config.Get("API_KEY"));
            Assert.Equal(expected.Length>0?expected+"-secret-key":"",config.Get("SECRET_KEY"));
            Assert.DoesNotContain("-api-key",string.Join('|',config.Masked().Values));
            Assert.DoesNotContain("-secret-key",string.Join('|',config.Masked().Values));
        }
        finally
        {
            Environment.SetEnvironmentVariable("FI_API_KEY",api); Environment.SetEnvironmentVariable("FI_SECRET_KEY",secret);
            File.Delete(path);
        }
    }
    [Theory]
    [InlineData("")] [InlineData(" ")] [InlineData("\t")]
    public void BlankCredentialHalvesDoNotMaskCompleteLowerPriorityPair(string blank)
    {
        string? api=Environment.GetEnvironmentVariable("FI_API_KEY"),secret=Environment.GetEnvironmentVariable("FI_SECRET_KEY");
        try
        {
            Environment.SetEnvironmentVariable("FI_API_KEY","env-api-key"); Environment.SetEnvironmentVariable("FI_SECRET_KEY","env-secret-key");
            var config=new AutoConfig(new AutoOptions { ApiKey="explicit-api-key",SecretKey=blank,ConfigFile=Path.Combine(WireFixtureTests.Repo,"csharp/absent.toml") });
            Assert.Equal("env-api-key",config.Get("API_KEY")); Assert.Equal("env-secret-key",config.Get("SECRET_KEY"));
        }
        finally { Environment.SetEnvironmentVariable("FI_API_KEY",api); Environment.SetEnvironmentVariable("FI_SECRET_KEY",secret); }
    }
}
