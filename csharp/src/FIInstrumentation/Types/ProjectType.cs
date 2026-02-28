namespace FIInstrumentation.Types;

public enum ProjectType
{
    Experiment,
    Observe,
}

public static class ProjectTypeExtensions
{
    public static string ToValue(this ProjectType type) => type switch
    {
        ProjectType.Experiment => "experiment",
        ProjectType.Observe => "observe",
        _ => "experiment",
    };
}
