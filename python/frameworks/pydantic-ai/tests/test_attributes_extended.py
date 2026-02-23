"""Extended tests for Pydantic AI semantic attributes."""

import pytest


class TestPydanticAISpanKindCompleteness:
    """Test PydanticAISpanKind enum completeness."""

    def test_all_span_kinds_have_unique_values(self):
        """Test all span kinds have unique values."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        values = [kind.value for kind in PydanticAISpanKind]
        assert len(values) == len(set(values))

    def test_span_kinds_are_strings(self):
        """Test span kinds are strings."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        for kind in PydanticAISpanKind:
            assert isinstance(kind.value, str)

    def test_span_kinds_are_lowercase(self):
        """Test span kinds are lowercase."""
        from traceai_pydantic_ai._attributes import PydanticAISpanKind

        for kind in PydanticAISpanKind:
            assert kind.value == kind.value.lower()


class TestPydanticAIAttributesCompleteness:
    """Test PydanticAIAttributes class completeness."""

    def test_all_attributes_are_strings(self):
        """Test all attribute values are strings."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        for attr_name in dir(PydanticAIAttributes):
            if not attr_name.startswith("_"):
                value = getattr(PydanticAIAttributes, attr_name)
                assert isinstance(value, str), f"{attr_name} is not a string"

    def test_pydantic_ai_attributes_have_prefix(self):
        """Test pydantic_ai specific attributes have correct prefix."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        pydantic_attrs = [
            PydanticAIAttributes.SPAN_KIND,
            PydanticAIAttributes.GEN_AI_AGENT_NAME,
            PydanticAIAttributes.AGENT_INSTRUCTIONS,
            PydanticAIAttributes.RUN_ID,
            PydanticAIAttributes.RUN_PROMPT,
            PydanticAIAttributes.GEN_AI_TOOL_NAME,
            PydanticAIAttributes.TOOL_ARGS,
        ]

        for attr in pydantic_attrs:
            assert attr.startswith("pydantic_ai.")

    def test_genai_attributes_follow_convention(self):
        """Test GenAI attributes follow semantic convention."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        genai_attrs = [
            PydanticAIAttributes.USAGE_INPUT_TOKENS,
            PydanticAIAttributes.USAGE_OUTPUT_TOKENS,
            PydanticAIAttributes.USAGE_TOTAL_TOKENS,
            PydanticAIAttributes.MODEL_NAME,
            PydanticAIAttributes.MODEL_PROVIDER,
        ]

        for attr in genai_attrs:
            assert attr.startswith("gen_ai.")

    def test_cost_attributes_exist(self):
        """Test cost tracking attributes exist."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert hasattr(PydanticAIAttributes, "COST_TOTAL_USD")
        assert hasattr(PydanticAIAttributes, "COST_INPUT_USD")
        assert hasattr(PydanticAIAttributes, "COST_OUTPUT_USD")

    def test_performance_attributes_exist(self):
        """Test performance tracking attributes exist."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert hasattr(PydanticAIAttributes, "DURATION_MS")
        assert hasattr(PydanticAIAttributes, "TIME_TO_FIRST_TOKEN_MS")

    def test_validation_attributes_exist(self):
        """Test validation attributes exist."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert hasattr(PydanticAIAttributes, "VALIDATION_IS_VALID")
        assert hasattr(PydanticAIAttributes, "VALIDATION_ERROR")
        assert hasattr(PydanticAIAttributes, "VALIDATION_RETRIES")

    def test_metadata_attributes_exist(self):
        """Test metadata attributes exist."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        assert hasattr(PydanticAIAttributes, "METADATA")
        assert hasattr(PydanticAIAttributes, "METADATA_TAGS")


class TestGetModelProviderExtended:
    """Extended tests for get_model_provider function."""

    def test_openai_variants(self):
        """Test various OpenAI model names."""
        from traceai_pydantic_ai._attributes import get_model_provider

        openai_models = [
            "gpt-4",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4-turbo-preview",
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-16k",
            "o1-preview",
            "o1-mini",
            "o3-mini",
        ]
        for model in openai_models:
            assert get_model_provider(model) == "openai", f"Failed for {model}"

    def test_anthropic_variants(self):
        """Test various Anthropic model names."""
        from traceai_pydantic_ai._attributes import get_model_provider

        anthropic_models = [
            "claude-3-opus",
            "claude-3-sonnet",
            "claude-3-haiku",
            "claude-3-5-sonnet",
            "claude-3-5-sonnet-20240620",
            "claude-2.1",
            "claude-2.0",
            "claude-instant-1.2",
        ]
        for model in anthropic_models:
            assert get_model_provider(model) == "anthropic", f"Failed for {model}"

    def test_google_variants(self):
        """Test various Google model names."""
        from traceai_pydantic_ai._attributes import get_model_provider

        google_models = [
            "gemini-pro",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-ultra",
            "models/gemini-pro",
            "models/gemini-1.5-pro-latest",
        ]
        for model in google_models:
            assert get_model_provider(model) == "google", f"Failed for {model}"

    def test_mistral_variants(self):
        """Test various Mistral model names."""
        from traceai_pydantic_ai._attributes import get_model_provider

        mistral_models = [
            "mistral-large",
            "mistral-medium",
            "mistral-small",
            "mistral-7b",
            "mistral-8x7b",
        ]
        for model in mistral_models:
            assert get_model_provider(model) == "mistral", f"Failed for {model}"

    def test_groq_variants(self):
        """Test various Groq model names."""
        from traceai_pydantic_ai._attributes import get_model_provider

        groq_models = [
            "groq-llama-3",
            "groq-mixtral",
        ]
        for model in groq_models:
            assert get_model_provider(model) == "groq", f"Failed for {model}"

    def test_provider_prefix_extraction(self):
        """Test provider:model format extraction."""
        from traceai_pydantic_ai._attributes import get_model_provider

        test_cases = [
            ("openai:gpt-4o", "openai"),
            ("anthropic:claude-3-opus", "anthropic"),
            ("google-gla:gemini-1.5-pro", "google-gla"),
            ("azure:gpt-4", "azure"),
            # bedrock: matches the "bedrock" prefix in MODEL_PROVIDERS -> "aws"
            ("bedrock:anthropic.claude-3", "aws"),
            # vertex: matches the "vertex" prefix in MODEL_PROVIDERS -> "google"
            ("vertex:gemini-pro", "google"),
            ("ollama:llama3", "ollama"),
            ("together:meta-llama/Llama-3", "together"),
        ]
        for model, expected in test_cases:
            assert get_model_provider(model) == expected, f"Failed for {model}"

    def test_case_insensitivity(self):
        """Test model name case insensitivity."""
        from traceai_pydantic_ai._attributes import get_model_provider

        assert get_model_provider("GPT-4o") == "openai"
        assert get_model_provider("CLAUDE-3-opus") == "anthropic"
        assert get_model_provider("GEMINI-pro") == "google"

    def test_deepseek_models(self):
        """Test DeepSeek model detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        deepseek_models = [
            "deepseek-chat",
            "deepseek-coder",
            "deepseek-reasoner",
        ]
        for model in deepseek_models:
            assert get_model_provider(model) == "deepseek", f"Failed for {model}"

    def test_cohere_models(self):
        """Test Cohere model detection."""
        from traceai_pydantic_ai._attributes import get_model_provider

        cohere_models = [
            "cohere-command",
            "cohere-command-r",
            "cohere-embed",
        ]
        for model in cohere_models:
            assert get_model_provider(model) == "cohere", f"Failed for {model}"

    def test_special_characters_in_model_name(self):
        """Test handling of special characters."""
        from traceai_pydantic_ai._attributes import get_model_provider

        # Should handle without crashing
        get_model_provider("model/with/slashes")
        get_model_provider("model-with-dashes")
        get_model_provider("model_with_underscores")
        get_model_provider("model.with.dots")


class TestModelProvidersMapping:
    """Test MODEL_PROVIDERS mapping coverage."""

    def test_major_providers_covered(self):
        """Test major providers are in mapping."""
        from traceai_pydantic_ai._attributes import MODEL_PROVIDERS

        major_providers = [
            "openai",
            "gpt-",
            "o1-",
            "anthropic",
            "claude-",
            "gemini",
            "mistral",
            "groq",
            "deepseek",
            "cohere",
            "ollama",
        ]
        for provider in major_providers:
            assert provider in MODEL_PROVIDERS, f"Missing provider: {provider}"

    def test_no_empty_values(self):
        """Test no empty values in mapping."""
        from traceai_pydantic_ai._attributes import MODEL_PROVIDERS

        for key, value in MODEL_PROVIDERS.items():
            assert key, "Empty key in MODEL_PROVIDERS"
            assert value, "Empty value in MODEL_PROVIDERS"

    def test_values_are_lowercase(self):
        """Test all values are lowercase."""
        from traceai_pydantic_ai._attributes import MODEL_PROVIDERS

        for value in MODEL_PROVIDERS.values():
            assert value == value.lower()


class TestAttributeCategories:
    """Test attribute categorization and organization."""

    def test_agent_attributes_complete(self):
        """Test agent-level attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        agent_attrs = [
            "GEN_AI_AGENT_NAME",
            "AGENT_MODEL",
            "AGENT_MODEL_PROVIDER",
            "AGENT_INSTRUCTIONS",
            "AGENT_RESULT_TYPE",
            "AGENT_DEPS_TYPE",
        ]
        for attr in agent_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"

    def test_run_attributes_complete(self):
        """Test run-level attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        run_attrs = [
            "RUN_ID",
            "RUN_METHOD",
            "RUN_PROMPT",
            "RUN_MESSAGE_HISTORY_LENGTH",
            "RUN_RESULT",
            "RUN_IS_STRUCTURED",
        ]
        for attr in run_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"

    def test_tool_attributes_complete(self):
        """Test tool-level attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        tool_attrs = [
            "GEN_AI_TOOL_NAME",
            "GEN_AI_TOOL_DESCRIPTION",
            "TOOL_ARGS",
            "TOOL_RESULT",
            "TOOL_IS_ERROR",
            "TOOL_ERROR_MESSAGE",
            "TOOL_RETRY_COUNT",
            "TOOL_DURATION_MS",
        ]
        for attr in tool_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"

    def test_usage_attributes_complete(self):
        """Test usage attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        usage_attrs = [
            "USAGE_INPUT_TOKENS",
            "USAGE_OUTPUT_TOKENS",
            "USAGE_TOTAL_TOKENS",
            "USAGE_CACHE_READ_TOKENS",
            "USAGE_CACHE_CREATION_TOKENS",
        ]
        for attr in usage_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"

    def test_error_attributes_complete(self):
        """Test error attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        error_attrs = [
            "ERROR_TYPE",
            "ERROR_MESSAGE",
            "IS_ERROR",
        ]
        for attr in error_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"

    def test_stream_attributes_complete(self):
        """Test streaming attributes are complete."""
        from traceai_pydantic_ai._attributes import PydanticAIAttributes

        stream_attrs = [
            "STREAM_CHUNK_COUNT",
            "STREAM_IS_STRUCTURED",
        ]
        for attr in stream_attrs:
            assert hasattr(PydanticAIAttributes, attr), f"Missing {attr}"
