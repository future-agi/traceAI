using FIInstrumentation.Context;

namespace FIInstrumentation.Tests;

public class ContextAttributesTests
{
    [Fact]
    public void UsingSession_SetsAndClearsSessionId()
    {
        // Before: no session
        var before = ContextAttributes.GetAttributesFromContext().ToList();
        Assert.DoesNotContain(before, kv => kv.Key == SemanticConventions.SessionId);

        using (ContextAttributes.UsingSession("session-abc"))
        {
            var during = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("session-abc", during[SemanticConventions.SessionId]);
        }

        // After: session cleared
        var after = ContextAttributes.GetAttributesFromContext().ToList();
        Assert.DoesNotContain(after, kv => kv.Key == SemanticConventions.SessionId);
    }

    [Fact]
    public void UsingUser_SetsAndClearsUserId()
    {
        using (ContextAttributes.UsingUser("user-xyz"))
        {
            var attrs = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("user-xyz", attrs[SemanticConventions.UserId]);
        }

        var after = ContextAttributes.GetAttributesFromContext().ToList();
        Assert.DoesNotContain(after, kv => kv.Key == SemanticConventions.UserId);
    }

    [Fact]
    public void UsingMetadata_SetsAndClearsMetadata()
    {
        var metadata = new Dictionary<string, object> { ["env"] = "test", ["version"] = 1 };
        using (ContextAttributes.UsingMetadata(metadata))
        {
            var attrs = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.True(attrs.ContainsKey(SemanticConventions.Metadata));
            Assert.Contains("env", attrs[SemanticConventions.Metadata]);
        }

        var after = ContextAttributes.GetAttributesFromContext().ToList();
        Assert.DoesNotContain(after, kv => kv.Key == SemanticConventions.Metadata);
    }

    [Fact]
    public void UsingTags_SetsAndClearsTags()
    {
        var tags = new List<string> { "tag1", "tag2" };
        using (ContextAttributes.UsingTags(tags))
        {
            var attrs = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.True(attrs.ContainsKey(SemanticConventions.TagTags));
            Assert.Contains("tag1", attrs[SemanticConventions.TagTags]);
        }
    }

    [Fact]
    public void UsingPromptTemplate_SetsAllFields()
    {
        var variables = new Dictionary<string, object> { ["name"] = "Claude" };
        using (ContextAttributes.UsingPromptTemplate(
            "Answer {name}'s question",
            label: "v1-prompt",
            version: "1.0",
            variables: variables))
        {
            var attrs = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("Answer {name}'s question", attrs[SemanticConventions.GenAiPromptTemplateName]);
            Assert.Equal("v1-prompt", attrs[SemanticConventions.GenAiPromptTemplateLabel]);
            Assert.Equal("1.0", attrs[SemanticConventions.GenAiPromptTemplateVersion]);
            Assert.Contains("Claude", attrs[SemanticConventions.GenAiPromptTemplateVariables]);
        }
    }

    [Fact]
    public void UsingAttributes_SetsMultipleAttributes()
    {
        using (ContextAttributes.UsingAttributes(
            sessionId: "session-1",
            userId: "user-1",
            tags: new List<string> { "prod" }))
        {
            var attrs = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("session-1", attrs[SemanticConventions.SessionId]);
            Assert.Equal("user-1", attrs[SemanticConventions.UserId]);
            Assert.Contains("prod", attrs[SemanticConventions.TagTags]);
        }

        // All cleared
        var after = ContextAttributes.GetAttributesFromContext().ToList();
        Assert.Empty(after);
    }

    [Fact]
    public void NestedScopes_RestorePreviousValues()
    {
        using (ContextAttributes.UsingSession("outer"))
        {
            var outer = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("outer", outer[SemanticConventions.SessionId]);

            using (ContextAttributes.UsingSession("inner"))
            {
                var inner = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
                Assert.Equal("inner", inner[SemanticConventions.SessionId]);
            }

            // Restored to outer
            var restored = ContextAttributes.GetAttributesFromContext().ToDictionary(kv => kv.Key, kv => kv.Value);
            Assert.Equal("outer", restored[SemanticConventions.SessionId]);
        }
    }
}
