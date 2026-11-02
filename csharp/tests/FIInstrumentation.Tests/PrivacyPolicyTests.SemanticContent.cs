using FIInstrumentation.Context;

namespace FIInstrumentation.Tests;

public partial class PrivacyPolicyTests
{
    private static TraceConfig ContentConfig(string flag) => new()
    {
        HideInputs = flag == "inputs", HideOutputs = flag == "outputs",
        HideInputMessages = flag == "input-messages", HideOutputMessages = flag == "output-messages",
        HideInputText = flag == "input-text", HideOutputText = flag == "output-text",
        HideInputImages = flag == "images", HideEmbeddingVectors = flag == "embeddings",
        HideLlmInvocationParameters = flag == "parameters",
    };

    [Theory]
    [InlineData("inputs", "input.value.nested")]
    [InlineData("inputs", "gen_ai.tool.call.arguments")]
    [InlineData("inputs", "gen_ai.system_instructions")]
    [InlineData("inputs", "gen_ai.retrieval.query")]
    [InlineData("inputs", "gen_ai.prompt.template.variables")]
    [InlineData("inputs", "gen_ai.input.messages.0.message.content")]
    [InlineData("outputs", "output.value.nested")]
    [InlineData("outputs", "gen_ai.tool.call.result")]
    [InlineData("outputs", "gen_ai.retrieval.documents")]
    [InlineData("outputs", "gen_ai.output.messages.0.message.content")]
    [InlineData("input-messages", "gen_ai.input.messages.0.message.content")]
    [InlineData("output-messages", "gen_ai.output.messages.0.message.content")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.content")]
    [InlineData("output-text", "gen_ai.output.messages.0.message.content")]
    [InlineData("images", "gen_ai.input.images.0")]
    [InlineData("images", "gen_ai.input.messages.0.message.contents.0.message_content.image.image.url")]
    [InlineData("embeddings", "embedding.embeddings.0.embedding.vector")]
    [InlineData("embeddings", "gen_ai.embeddings.vectors.0")]
    public void SemanticContentPolicyMustCoverDocumentedFields(string flag, string key)
    {
        using var capture = new Capture(ContentConfig(flag));
        capture.Tracer.Chain("semantic-policy", span => span.SetAttribute(key, Secret));
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
        Assert.Contains(TraceConfig.RedactedValue, capture.Wire);
    }

    // Literal keys keep this matrix independent of production policy lists.
    public static IEnumerable<object[]> ContentFamilies()
    {
        string[] inputs = ["input.value", "raw.input", "llm.prompt_template.variables",
            "gen_ai.prompt.template.variables", "gen_ai.system_instructions", "gen_ai.request.parameters",
            "gen_ai.request.stop_sequences", "gen_ai.agent.description", "gen_ai.image.prompt",
            "gen_ai.image.negative_prompt", "gen_ai.input.images", "gen_ai.prompts",
            "gen_ai.tool.definitions", "gen_ai.tool.description", "gen_ai.tool.call.arguments",
            "gen_ai.tool.parameters", "tool.parameters", "tool_call.function.arguments",
            "message.function_call_arguments_json", "gen_ai.computer_use.text",
            "gen_ai.computer_use.element_selector", "tool.json_schema", "gen_ai.retrieval.query",
            "gen_ai.reranker.query", "gen_ai.reranker.input_documents", "reranker.query", "reranker.input_documents"];
        string[] outputs = ["output.value", "raw.output", "gen_ai.tool.call.result", "gen_ai.image.revised_prompt",
            "gen_ai.image.output_urls", "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation",
            "gen_ai.computer_use.result", "retrieval.documents", "gen_ai.retrieval.documents",
            "gen_ai.reranker.output_documents", "reranker.output_documents"];
        string[] mixed = ["gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
            "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url", "gen_ai.audio.url",
            "gen_ai.audio.transcript", "gen_ai.computer_use.screenshot", "gen_ai.computer_use.current_url"];
        foreach (var key in inputs) yield return ["inputs", key, false];
        foreach (var key in outputs) yield return ["outputs", key, false];
        foreach (var key in mixed)
        {
            yield return ["inputs", key, true];
            yield return ["outputs", key, true];
        }
    }

    [Theory]
    [MemberData(nameof(ContentFamilies))]
    public void ContentFamiliesMaskRootsAndDescendantsWithoutCrossingBoundaries(string flag, string root, bool mixed)
    {
        var config = ContentConfig(flag);
        using var capture = new Capture(config);
        foreach (var key in new[] { root, root + ".0.nested" })
        {
            Assert.Equal(TraceConfig.RedactedValue, config.Mask(key, () => throw new Exception("must not evaluate")));
            capture.Tracer.Chain("content-family", span => span.SetAttribute(key, Secret));
            if (!mixed)
                Assert.Equal(Secret, ContentConfig(flag == "inputs" ? "outputs" : "inputs").Mask(key, Secret));
            Assert.Equal(Secret, new TraceConfig().Mask(key, Secret));
        }
        Assert.Equal(2, capture.Handler.Payloads.Count);
        Assert.DoesNotContain(Secret, capture.Wire);
        Assert.Equal(Secret, config.Mask(root + "_custom", Secret));
        Assert.Equal(Secret, config.Mask("custom." + root, Secret));
    }

    [Theory]
    [InlineData("inputs")]
    [InlineData("outputs")]
    public void AggregateToolCallIsPrivateButIdentifiersAndOppositeDirectionSurvive(string flag)
    {
        var config = ContentConfig(flag);
        Assert.Equal(TraceConfig.RedactedValue, config.Mask("gen_ai.tool.call", Secret));
        foreach (var key in new[] { "gen_ai.tool.call.id", "gen_ai.tool.name", "gen_ai.request.model",
            "gen_ai.response.id", "gen_ai.prompt.template.name", "gen_ai.prompt.template.label",
            "gen_ai.prompt.template.version", "gen_ai.usage.input_tokens", "gen_ai.retrieval.top_k" })
            Assert.Equal(Secret, config.Mask(key, Secret));
    }

    [Theory]
    [InlineData("input-messages", "gen_ai.input.messages.0.message.role")]
    [InlineData("output-messages", "gen_ai.output.messages.0.message.role")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.contents.0.message_content.text")]
    [InlineData("output-text", "gen_ai.output.messages.0.message.contents.0.message_content.text")]
    [InlineData("input-text", "gen_ai.input.messages.0")]
    [InlineData("input-text", "gen_ai.input.messages.0.message")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.contents.0.message_content")]
    [InlineData("output-text", "gen_ai.output.messages.0.message.contents.0")]
    [InlineData("images", "gen_ai.input.messages")]
    [InlineData("images", "gen_ai.input.messages.0.message.contents")]
    [InlineData("images", "gen_ai.computer_use.screenshot.0")]
    [InlineData("inputs", "embedding.embeddings.0.embedding.text")]
    [InlineData("input-messages", "embedding.embeddings.0.embedding.text")]
    [InlineData("input-text", "embedding.embeddings.0.embedding.text")]
    [InlineData("embeddings", "embedding.embeddings.0.embedding.vector.0")]
    [InlineData("embeddings", "embedding.vector.0")]
    [InlineData("embeddings", "embedding.embeddings")]
    [InlineData("embeddings", "embedding.embeddings.0")]
    [InlineData("embeddings", "embedding.embeddings.0.embedding")]
    [InlineData("inputs", "embedding.embeddings.0")]
    [InlineData("parameters", "gen_ai.request.parameters.temperature")]
    [InlineData("parameters", "gen_ai.request.stop_sequences.0")]
    public void FlattenedAndOpaqueContentIsMaskedBeforeEvaluation(string flag, string key)
    {
        var config = ContentConfig(flag);
        Assert.Equal(TraceConfig.RedactedValue, config.Mask(key, () => throw new Exception("must not evaluate")));
        using var capture = new Capture(config);
        capture.Tracer.Chain("flattened", span => span.SetAttribute(key, Secret));
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Theory]
    [InlineData("input-text", "gen_ai.input.messages.0.message.role")]
    [InlineData("output-text", "gen_ai.output.messages.0.message.name")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.contents.0.message_content.type")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.contents.0.message_content.image.image.url")]
    [InlineData("images", "gen_ai.input.messages.0.message.content")]
    [InlineData("images", "gen_ai.input.messages.0.message.contents.0.message_content.text")]
    [InlineData("images", "gen_ai.output.messages.0.message.contents.0.message_content.image.image.url")]
    [InlineData("embeddings", "embedding.embeddings.0.embedding.text")]
    [InlineData("embeddings", "embedding.model_name")]
    [InlineData("embeddings", "embedding.embeddings.0.embedding.vector_size")]
    [InlineData("embeddings", "custom.embedding.vector")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.content_type")]
    [InlineData("input-text", "gen_ai.input.messages.0.message.contents.0.message_content.text_length")]
    [InlineData("images", "gen_ai.input.messages.0.message.contents.0.message_content.image_description")]
    [InlineData("embeddings", "gen_ai.embeddings.dimension.count")]
    [InlineData("input-messages", "gen_ai.input.messages_custom")]
    [InlineData("inputs", "gen_ai.output.messages.0.message.content")]
    [InlineData("outputs", "gen_ai.input.messages.0.message.content")]
    public void NarrowFlagsPreserveOtherFields(string flag, string key)
    {
        var config = ContentConfig(flag);
        Assert.Equal(Secret, config.Mask(key, () => Secret));
        using var capture = new Capture(config);
        capture.Tracer.Chain("visible", span => span.SetAttribute(key, Secret));
        Assert.Single(capture.Handler.Payloads);
        Assert.Contains(Secret, capture.Wire);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void HiddenPromptTemplateContextMustNotReachExport(bool usingAttributes)
    {
        using var capture = new Capture(new TraceConfig { HideInputs = true });
        Dictionary<string, object> variables = new() { ["query"] = Secret };
        using (usingAttributes
            ? ContextAttributes.UsingAttributes(promptTemplate: "synthetic-template", variables: variables)
            : ContextAttributes.UsingPromptTemplate("synthetic-template", variables: variables))
            capture.Tracer.Chain("private-template", _ => { });
        Assert.Single(capture.Handler.Payloads);
        Assert.Contains("synthetic-template", capture.Wire);
        Assert.DoesNotContain(Secret, capture.Wire);
        Assert.Contains(TraceConfig.RedactedValue, capture.Wire);
    }

    [Fact]
    public void HiddenPromptTemplateContextMustNotInvokeSerializer()
    {
        using var capture = new Capture(new TraceConfig { HideInputs = true });
        var poison = new PoisonValue();
        using (ContextAttributes.UsingPromptTemplate("synthetic-template", variables: new() { ["query"] = poison }))
            capture.Tracer.Chain("lazy-private-template", _ => { });
        Assert.Equal(0, poison.Reads);
        Assert.Single(capture.Handler.Payloads);
        Assert.DoesNotContain(Secret, capture.Wire);
    }

    [Fact]
    public void ContextMaskingUsesEachTracerPolicyWithoutChangingAmbientValues()
    {
        using var hidden = new Capture(new TraceConfig { HideInputs = true });
        using var visible = new Capture(new TraceConfig { HideOutputs = true });
        using (ContextAttributes.UsingAttributes(sessionId: "session", userId: "user",
            metadata: new() { ["env"] = "test" }, tags: ["tag"], promptTemplate: "template",
            promptTemplateLabel: "label", promptTemplateVersion: "1", variables: new() { ["query"] = Secret }))
        {
            hidden.Tracer.Chain("hidden-context", _ => { });
            visible.Tracer.Chain("visible-context", _ => { });
            Assert.Contains(Secret, ContextAttributes.GetAttributesFromContext()
                .Single(kv => kv.Key == SemanticConventions.GenAiPromptTemplateVariables).Value);
        }
        Assert.DoesNotContain(Secret, hidden.Wire);
        Assert.Contains(Secret, visible.Wire);
        foreach (var value in new[] { "session", "user", "test", "tag", "template", "label" })
            Assert.Contains(value, hidden.Wire);
        Assert.Empty(ContextAttributes.GetAttributesFromContext());
    }
}
