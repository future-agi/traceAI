namespace FIInstrumentation;

/// <summary>
/// Environment variable helpers for FI configuration.
/// </summary>
public static class Settings
{
    public const string DefaultBaseUrl = "https://api.futureagi.com";
    public const string DefaultGrpcUrl = "https://grpc.futureagi.com";
    public const string DefaultProjectName = "DEFAULT_PROJECT_NAME";
    public const string DefaultProjectVersionName = "DEFAULT_PROJECT_VERSION_NAME";

    public static string GetCollectorEndpoint() =>
        Environment.GetEnvironmentVariable("FI_BASE_URL") ?? DefaultBaseUrl;

    public static string GetGrpcEndpoint() =>
        Environment.GetEnvironmentVariable("FI_GRPC_URL") ?? DefaultGrpcUrl;

    public static string GetProjectName() =>
        Environment.GetEnvironmentVariable("FI_PROJECT_NAME") ?? DefaultProjectName;

    public static string GetProjectVersionName() =>
        Environment.GetEnvironmentVariable("FI_PROJECT_VERSION_NAME") ?? DefaultProjectVersionName;

    public static string? GetApiKey() =>
        Environment.GetEnvironmentVariable("FI_API_KEY");

    public static string? GetSecretKey() =>
        Environment.GetEnvironmentVariable("FI_SECRET_KEY");

    public static Dictionary<string, string>? GetAuthHeaders()
    {
        var apiKey = GetApiKey();
        var secretKey = GetSecretKey();

        if (string.IsNullOrEmpty(apiKey) && string.IsNullOrEmpty(secretKey))
            return null;

        var headers = new Dictionary<string, string>();
        if (!string.IsNullOrEmpty(apiKey))
            headers["X-Api-Key"] = apiKey;
        if (!string.IsNullOrEmpty(secretKey))
            headers["X-Secret-Key"] = secretKey;

        return headers;
    }

    /// <summary>
    /// Parse W3C Baggage-style header string into key-value pairs.
    /// Format: "key1=value1,key2=value2"
    /// </summary>
    public static Dictionary<string, string> ParseHeaders(string headerString)
    {
        var result = new Dictionary<string, string>();
        if (string.IsNullOrWhiteSpace(headerString))
            return result;

        foreach (var pair in headerString.Split(','))
        {
            var parts = pair.Trim().Split('=', 2);
            if (parts.Length == 2)
                result[parts[0].Trim()] = parts[1].Trim();
        }

        return result;
    }
}
