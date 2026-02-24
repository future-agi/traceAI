namespace FIInstrumentation.Types;

public enum FIMimeType
{
    Text,
    Json,
}

public static class FIMimeTypeExtensions
{
    public static string ToValue(this FIMimeType type) => type switch
    {
        FIMimeType.Text => "text/plain",
        FIMimeType.Json => "application/json",
        _ => "text/plain",
    };
}
