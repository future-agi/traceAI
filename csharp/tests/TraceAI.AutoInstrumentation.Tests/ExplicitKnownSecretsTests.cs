using System.Reflection;
using System.Runtime.Loader;
using Xunit;

namespace TraceAI.AutoInstrumentation.Tests;

/// <summary>The secrets the application registered (screened for in ambient exporter headers) live in the same process state as the redaction registry.</summary>
public class ExplicitKnownSecretsTests : InitIsolation
{
    private sealed class OtherContext() : AssemblyLoadContext(isCollectible: true)
    {
        protected override Assembly? Load(AssemblyName name) => Default.Assemblies.FirstOrDefault(a => a.GetName().Name == name.Name);
    }

    [Fact]
    public void ARegistrationFromAnotherLoadContextIsSeenByTheDefaultContextAndTheOtherWayRound()
    {
        var context = new OtherContext();
        try
        {
            var assembly = context.LoadFromAssemblyPath(typeof(TraceAIAuto).Assembly.Location);
            assembly.GetType(typeof(TraceAIAuto).FullName!)!.GetMethod(nameof(TraceAIAuto.RegisterSecret))!.Invoke(null, new object[] { "OTHER_CONTEXT_EXPLICIT_0001" });
            Assert.Contains("OTHER_CONTEXT_EXPLICIT_0001", KnownSecrets.Registered());
            TraceAIAuto.RegisterSecret("DEFAULT_CONTEXT_EXPLICIT_0002");
            var other = (string[])assembly.GetType(typeof(KnownSecrets).FullName!)!.GetMethod("Registered", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, null)!;
            Assert.Contains("DEFAULT_CONTEXT_EXPLICIT_0002", other);
            Assert.Contains("OTHER_CONTEXT_EXPLICIT_0001", other);
        }
        finally { context.Unload(); }
    }

    [Fact]
    public void TheBoundedRegistryKeepsTheNewestValuesAndAReRegistrationMovesToTheBack()
    {
        for (var i = 0; i < KnownSecrets.Capacity; i++) TraceAIAuto.RegisterSecret("EXPLICIT_" + i.ToString("D5"));
        Assert.Equal(KnownSecrets.Capacity, KnownSecrets.Registered().Length);
        TraceAIAuto.RegisterSecret("EXPLICIT_00000");
        TraceAIAuto.RegisterSecret("EXPLICIT_NEWEST_1");
        var kept = KnownSecrets.Registered();
        Assert.Equal(KnownSecrets.Capacity, kept.Length);
        Assert.Contains("EXPLICIT_00000", kept);
        Assert.Contains("EXPLICIT_NEWEST_1", kept);
        Assert.DoesNotContain("EXPLICIT_00001", kept);
        TraceAIAuto.RegisterSecret("EXPLICIT_NEWEST_2");
        kept = KnownSecrets.Registered();
        Assert.Contains("EXPLICIT_00000", kept);
        Assert.DoesNotContain("EXPLICIT_00002", kept);
        for (var i = 0; i < KnownSecrets.Capacity - 1; i++) TraceAIAuto.RegisterSecret("EXPLICIT_LATER_" + i.ToString("D5"));
        kept = KnownSecrets.Registered();
        Assert.DoesNotContain("EXPLICIT_00000", kept);
        Assert.Contains("EXPLICIT_LATER_00000", kept);
    }
}
