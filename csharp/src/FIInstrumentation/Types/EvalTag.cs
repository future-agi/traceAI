using System.Text.Json.Serialization;

namespace FIInstrumentation.Types;

public enum EvalTagType
{
    ObservationSpan,
}

public static class EvalTagTypeExtensions
{
    public static string ToValue(this EvalTagType type) => type switch
    {
        EvalTagType.ObservationSpan => "OBSERVATION_SPAN_TYPE",
        _ => "OBSERVATION_SPAN_TYPE",
    };
}

public class EvalTag
{
    public EvalTagType Type { get; set; } = EvalTagType.ObservationSpan;
    public EvalSpanKind Value { get; set; }
    public string EvalNameValue { get; set; } = string.Empty;
    public ModelChoices? Model { get; set; }
    public Dictionary<string, object>? Config { get; set; }
    public string? CustomEvalName { get; set; }
    public Dictionary<string, string>? Mapping { get; set; }

    public EvalTag(EvalSpanKind value, string evalName)
    {
        Value = value;
        EvalNameValue = evalName;
    }

    public EvalTag(EvalSpanKind value, EvalName evalName)
    {
        Value = value;
        EvalNameValue = evalName.ToValue();
    }

    public Dictionary<string, object?> ToDict()
    {
        var result = new Dictionary<string, object?>
        {
            ["type"] = Type.ToValue(),
            ["value"] = Value.ToValue(),
            ["eval_name"] = EvalNameValue,
        };

        if (Model.HasValue)
            result["model"] = Model.Value.ToValue();
        if (Config != null)
            result["config"] = Config;
        if (CustomEvalName != null)
            result["custom_eval_name"] = CustomEvalName;
        if (Mapping != null)
            result["mapping"] = Mapping;

        return result;
    }
}
