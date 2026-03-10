namespace FIInstrumentation.Tests;

public class FISpanKindTests
{
    [Theory]
    [InlineData(FISpanKind.Llm, "LLM")]
    [InlineData(FISpanKind.Chain, "CHAIN")]
    [InlineData(FISpanKind.Agent, "AGENT")]
    [InlineData(FISpanKind.Tool, "TOOL")]
    [InlineData(FISpanKind.Embedding, "EMBEDDING")]
    [InlineData(FISpanKind.Retriever, "RETRIEVER")]
    [InlineData(FISpanKind.Reranker, "RERANKER")]
    [InlineData(FISpanKind.Guardrail, "GUARDRAIL")]
    [InlineData(FISpanKind.Evaluator, "EVALUATOR")]
    [InlineData(FISpanKind.Conversation, "CONVERSATION")]
    [InlineData(FISpanKind.VectorDb, "VECTOR_DB")]
    [InlineData(FISpanKind.Unknown, "UNKNOWN")]
    public void ToValue_ReturnsCorrectString(FISpanKind kind, string expected)
    {
        Assert.Equal(expected, kind.ToValue());
    }

    [Theory]
    [InlineData("LLM", FISpanKind.Llm)]
    [InlineData("llm", FISpanKind.Llm)]
    [InlineData("CHAIN", FISpanKind.Chain)]
    [InlineData("AGENT", FISpanKind.Agent)]
    [InlineData("TOOL", FISpanKind.Tool)]
    [InlineData("VECTOR_DB", FISpanKind.VectorDb)]
    [InlineData("invalid", FISpanKind.Unknown)]
    public void FromValue_ParsesCorrectly(string value, FISpanKind expected)
    {
        Assert.Equal(expected, FISpanKindExtensions.FromValue(value));
    }
}
