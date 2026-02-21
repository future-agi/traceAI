namespace FIInstrumentation.Tests;

public class TraceConfigTests
{
    [Fact]
    public void DefaultConfig_NothingHidden()
    {
        var config = TraceConfig.Builder().Build();

        Assert.False(config.HideInputs);
        Assert.False(config.HideOutputs);
        Assert.False(config.HideInputMessages);
        Assert.False(config.HideOutputMessages);
        Assert.False(config.HideInputImages);
        Assert.False(config.HideInputText);
        Assert.False(config.HideOutputText);
        Assert.False(config.HideEmbeddingVectors);
        Assert.False(config.HideLlmInvocationParameters);
        Assert.Equal(32_000, config.Base64ImageMaxLength);
    }

    [Fact]
    public void Builder_SetsHideInputs()
    {
        var config = TraceConfig.Builder()
            .HideInputs()
            .Build();

        Assert.True(config.HideInputs);
    }

    [Fact]
    public void Builder_SetsHideOutputs()
    {
        var config = TraceConfig.Builder()
            .HideOutputs()
            .Build();

        Assert.True(config.HideOutputs);
    }

    [Fact]
    public void Builder_SetsMultipleOptions()
    {
        var config = TraceConfig.Builder()
            .HideInputs()
            .HideOutputs()
            .HideEmbeddingVectors()
            .Base64ImageMaxLength(16_000)
            .Build();

        Assert.True(config.HideInputs);
        Assert.True(config.HideOutputs);
        Assert.True(config.HideEmbeddingVectors);
        Assert.Equal(16_000, config.Base64ImageMaxLength);
    }

    [Fact]
    public void Mask_ReturnsValue_WhenNotHidden()
    {
        var config = TraceConfig.Builder().Build();
        var result = config.Mask(SemanticConventions.InputValue, "hello");
        Assert.Equal("hello", result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_WhenInputsHidden()
    {
        var config = TraceConfig.Builder()
            .HideInputs()
            .Build();

        var result = config.Mask(SemanticConventions.InputValue, "secret input");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_WhenOutputsHidden()
    {
        var config = TraceConfig.Builder()
            .HideOutputs()
            .Build();

        var result = config.Mask(SemanticConventions.OutputValue, "secret output");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_ForInputMessages_WhenHideInputMessagesSet()
    {
        var config = TraceConfig.Builder()
            .HideInputMessages()
            .Build();

        var result = config.Mask(SemanticConventions.GenAiInputMessages, "[{\"role\":\"user\"}]");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_ForInputMessages_WhenHideInputsSet()
    {
        var config = TraceConfig.Builder()
            .HideInputs()
            .Build();

        var result = config.Mask(SemanticConventions.GenAiInputMessages, "[{\"role\":\"user\"}]");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_ForEmbeddingVectors()
    {
        var config = TraceConfig.Builder()
            .HideEmbeddingVectors()
            .Build();

        var result = config.Mask(SemanticConventions.GenAiEmbeddingsVectors, "[0.1, 0.2]");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsRedacted_ForLlmInvocationParameters()
    {
        var config = TraceConfig.Builder()
            .HideLlmInvocationParameters()
            .Build();

        var result = config.Mask(SemanticConventions.GenAiRequestTemperature, "0.7");
        Assert.Equal(TraceConfig.RedactedValue, result);

        result = config.Mask(SemanticConventions.GenAiRequestTopP, "0.9");
        Assert.Equal(TraceConfig.RedactedValue, result);
    }

    [Fact]
    public void Mask_ReturnsNull_WhenValueIsNull()
    {
        var config = TraceConfig.Builder().Build();
        var result = config.Mask(SemanticConventions.InputValue, (string?)null);
        Assert.Null(result);
    }

    [Fact]
    public void Mask_WithFactory_ReturnsRedacted_WhenHidden()
    {
        var config = TraceConfig.Builder()
            .HideInputs()
            .Build();

        var factoryCalled = false;
        var result = config.Mask(SemanticConventions.InputValue, () =>
        {
            factoryCalled = true;
            return "expensive value";
        });

        Assert.Equal(TraceConfig.RedactedValue, result);
        Assert.False(factoryCalled); // Factory should not be called when hidden
    }

    [Fact]
    public void RedactedValue_IsCorrectString()
    {
        Assert.Equal("__REDACTED__", TraceConfig.RedactedValue);
    }
}
