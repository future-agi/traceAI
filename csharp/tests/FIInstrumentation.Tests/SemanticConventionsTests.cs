namespace FIInstrumentation.Tests;

public class SemanticConventionsTests
{
    [Fact]
    public void FiSpanKind_HasCorrectValue()
    {
        Assert.Equal("fi.span.kind", SemanticConventions.FiSpanKind);
    }

    [Fact]
    public void GenAiRequestModel_HasCorrectValue()
    {
        Assert.Equal("gen_ai.request.model", SemanticConventions.GenAiRequestModel);
    }

    [Fact]
    public void GenAiUsageInputTokens_HasCorrectValue()
    {
        Assert.Equal("gen_ai.usage.input_tokens", SemanticConventions.GenAiUsageInputTokens);
    }

    [Fact]
    public void GenAiUsageOutputTokens_HasCorrectValue()
    {
        Assert.Equal("gen_ai.usage.output_tokens", SemanticConventions.GenAiUsageOutputTokens);
    }

    [Fact]
    public void InputOutput_HasCorrectValues()
    {
        Assert.Equal("input.value", SemanticConventions.InputValue);
        Assert.Equal("output.value", SemanticConventions.OutputValue);
        Assert.Equal("input.mime_type", SemanticConventions.InputMimeType);
        Assert.Equal("output.mime_type", SemanticConventions.OutputMimeType);
    }

    [Fact]
    public void GenAiToolAttributes_HasCorrectValues()
    {
        Assert.Equal("gen_ai.tool.name", SemanticConventions.GenAiToolName);
        Assert.Equal("gen_ai.tool.description", SemanticConventions.GenAiToolDescription);
        Assert.Equal("gen_ai.tool.call.id", SemanticConventions.GenAiToolCallId);
        Assert.Equal("gen_ai.tool.definitions", SemanticConventions.GenAiToolDefinitions);
    }

    [Fact]
    public void ErrorAttributes_HasCorrectValues()
    {
        Assert.Equal("error.type", SemanticConventions.ErrorType);
        Assert.Equal("error.message", SemanticConventions.ErrorMessage);
    }

    [Fact]
    public void MetadataAttributes_HasCorrectValues()
    {
        Assert.Equal("metadata", SemanticConventions.Metadata);
        Assert.Equal("tag.tags", SemanticConventions.TagTags);
        Assert.Equal("session.id", SemanticConventions.SessionId);
        Assert.Equal("user.id", SemanticConventions.UserId);
    }

    [Fact]
    public void VoiceAttributes_HasCorrectValues()
    {
        Assert.Equal("gen_ai.voice.call_id", SemanticConventions.GenAiVoiceCallId);
        Assert.Equal("gen_ai.voice.transcript", SemanticConventions.GenAiVoiceTranscript);
        Assert.Equal("gen_ai.voice.call_duration_secs", SemanticConventions.GenAiVoiceCallDurationSecs);
    }

    [Fact]
    public void VectorDbAttributes_HasCorrectValues()
    {
        Assert.Equal("db.system", SemanticConventions.DbSystem);
        Assert.Equal("db.operation.name", SemanticConventions.DbOperationName);
        Assert.Equal("db.vector.query.top_k", SemanticConventions.DbVectorQueryTopK);
    }

    [Fact]
    public void GuardrailAttributes_HasCorrectValues()
    {
        Assert.Equal("gen_ai.guardrail.name", SemanticConventions.GenAiGuardrailName);
        Assert.Equal("gen_ai.guardrail.result", SemanticConventions.GenAiGuardrailResult);
        Assert.Equal("gen_ai.guardrail.score", SemanticConventions.GenAiGuardrailScore);
    }
}
