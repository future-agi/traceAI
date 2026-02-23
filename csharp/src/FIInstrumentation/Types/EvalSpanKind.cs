namespace FIInstrumentation.Types;

public enum EvalSpanKind
{
    Tool,
    Chain,
    Llm,
    Retriever,
    Embedding,
    Agent,
    Reranker,
    Unknown,
    Guardrail,
    Evaluator,
    Conversation,
}

public static class EvalSpanKindExtensions
{
    public static string ToValue(this EvalSpanKind kind) => kind switch
    {
        EvalSpanKind.Tool => "TOOL",
        EvalSpanKind.Chain => "CHAIN",
        EvalSpanKind.Llm => "LLM",
        EvalSpanKind.Retriever => "RETRIEVER",
        EvalSpanKind.Embedding => "EMBEDDING",
        EvalSpanKind.Agent => "AGENT",
        EvalSpanKind.Reranker => "RERANKER",
        EvalSpanKind.Unknown => "UNKNOWN",
        EvalSpanKind.Guardrail => "GUARDRAIL",
        EvalSpanKind.Evaluator => "EVALUATOR",
        EvalSpanKind.Conversation => "CONVERSATION",
        _ => "UNKNOWN",
    };
}
