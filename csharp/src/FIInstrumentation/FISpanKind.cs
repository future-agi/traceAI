namespace FIInstrumentation;

/// <summary>
/// Semantic span kinds for FI tracing.
/// </summary>
public enum FISpanKind
{
    Llm,
    Chain,
    Agent,
    Tool,
    Embedding,
    Retriever,
    Reranker,
    Guardrail,
    Evaluator,
    Conversation,
    VectorDb,
    Unknown,
}

public static class FISpanKindExtensions
{
    public static string ToValue(this FISpanKind kind) => kind switch
    {
        FISpanKind.Llm => "LLM",
        FISpanKind.Chain => "CHAIN",
        FISpanKind.Agent => "AGENT",
        FISpanKind.Tool => "TOOL",
        FISpanKind.Embedding => "EMBEDDING",
        FISpanKind.Retriever => "RETRIEVER",
        FISpanKind.Reranker => "RERANKER",
        FISpanKind.Guardrail => "GUARDRAIL",
        FISpanKind.Evaluator => "EVALUATOR",
        FISpanKind.Conversation => "CONVERSATION",
        FISpanKind.VectorDb => "VECTOR_DB",
        FISpanKind.Unknown => "UNKNOWN",
        _ => "UNKNOWN",
    };

    public static FISpanKind FromValue(string value) => value.ToUpperInvariant() switch
    {
        "LLM" => FISpanKind.Llm,
        "CHAIN" => FISpanKind.Chain,
        "AGENT" => FISpanKind.Agent,
        "TOOL" => FISpanKind.Tool,
        "EMBEDDING" => FISpanKind.Embedding,
        "RETRIEVER" => FISpanKind.Retriever,
        "RERANKER" => FISpanKind.Reranker,
        "GUARDRAIL" => FISpanKind.Guardrail,
        "EVALUATOR" => FISpanKind.Evaluator,
        "CONVERSATION" => FISpanKind.Conversation,
        "VECTOR_DB" => FISpanKind.VectorDb,
        _ => FISpanKind.Unknown,
    };
}
