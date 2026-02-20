"""Tests for fi_instrumentation.instrumentation.context_attributes module."""

import pytest
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock
from opentelemetry.context import get_current, get_value

from fi_instrumentation.instrumentation.context_attributes import (
    using_session,
    using_user,
    using_metadata,
    using_tags,
    using_prompt_template,
    using_attributes,
    get_attributes_from_context,
    CONTEXT_ATTRIBUTES,
)
from fi_instrumentation.fi_types import SimulatorAttributes, SpanAttributes


class TestUsingSession:
    """Test using_session context manager."""

    def test_using_session_sync_context(self):
        """Test using_session as synchronous context manager."""
        session_id = "test-session-123"
        
        # Initially, session ID should not be in context
        initial_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
        assert initial_value is None
        
        with using_session(session_id):
            # Inside context, session ID should be set
            current_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
            assert current_value == session_id
        
        # After context, session ID should be cleared
        final_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
        assert final_value is None

    @pytest.mark.asyncio
    async def test_using_session_async_context(self):
        """Test using_session as asynchronous context manager."""
        session_id = "test-session-async-456"
        
        # Initially, session ID should not be in context
        initial_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
        assert initial_value is None
        
        async with using_session(session_id):
            # Inside context, session ID should be set
            current_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
            assert current_value == session_id
        
        # After context, session ID should be cleared
        final_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
        assert final_value is None

    def test_using_session_nested(self):
        """Test nested using_session contexts."""
        session_id1 = "session-1"
        session_id2 = "session-2"
        
        with using_session(session_id1):
            assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id1
            
            with using_session(session_id2):
                assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id2
            
            # Should revert to outer session
            assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id1
        
        # Should be cleared after all contexts
        assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) is None

    def test_using_session_exception_handling(self):
        """Test using_session properly handles exceptions."""
        session_id = "test-session-exception"
        
        try:
            with using_session(session_id):
                assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id
                raise ValueError("Test exception")
        except ValueError:
            pass
        
        # Should still clear session ID after exception
        final_value = get_value(SpanAttributes.GEN_AI_CONVERSATION_ID)
        assert final_value is None


class TestUsingUser:
    """Test using_user context manager."""

    def test_using_user_sync_context(self):
        """Test using_user as synchronous context manager."""
        user_id = "user-123"
        
        with using_user(user_id):
            current_value = get_value(SpanAttributes.USER_ID)
            assert current_value == user_id
        
        final_value = get_value(SpanAttributes.USER_ID)
        assert final_value is None

    @pytest.mark.asyncio
    async def test_using_user_async_context(self):
        """Test using_user as asynchronous context manager."""
        user_id = "user-async-456"
        
        async with using_user(user_id):
            current_value = get_value(SpanAttributes.USER_ID)
            assert current_value == user_id
        
        final_value = get_value(SpanAttributes.USER_ID)
        assert final_value is None

    def test_using_user_nested(self):
        """Test nested using_user contexts."""
        user_id1 = "user-1"
        user_id2 = "user-2"
        
        with using_user(user_id1):
            assert get_value(SpanAttributes.USER_ID) == user_id1
            
            with using_user(user_id2):
                assert get_value(SpanAttributes.USER_ID) == user_id2
            
            assert get_value(SpanAttributes.USER_ID) == user_id1
        
        assert get_value(SpanAttributes.USER_ID) is None


class TestUsingMetadata:
    """Test using_metadata context manager."""

    def test_using_metadata_sync_context(self):
        """Test using_metadata as synchronous context manager."""
        metadata = {"key1": "value1", "key2": "value2"}
        
        with using_metadata(metadata):
            current_value = get_value(SpanAttributes.METADATA)
            # Should be JSON serialized
            assert current_value == '{"key1": "value1", "key2": "value2"}'
        
        final_value = get_value(SpanAttributes.METADATA)
        assert final_value is None

    @pytest.mark.asyncio
    async def test_using_metadata_async_context(self):
        """Test using_metadata as asynchronous context manager."""
        metadata = {"async_key": "async_value", "number": 42}
        
        async with using_metadata(metadata):
            current_value = get_value(SpanAttributes.METADATA)
            assert '{"async_key": "async_value", "number": 42}' in current_value or \
                   '{"number": 42, "async_key": "async_value"}' in current_value
        
        final_value = get_value(SpanAttributes.METADATA)
        assert final_value is None

    def test_using_metadata_complex_data(self):
        """Test using_metadata with complex data structures."""
        metadata = {
            "string": "value",
            "number": 123,
            "boolean": True,
            "list": [1, 2, 3],
            "nested": {"inner": "data"}
        }
        
        with using_metadata(metadata):
            current_value = get_value(SpanAttributes.METADATA)
            assert current_value is not None
            # Should be valid JSON
            import json
            parsed = json.loads(current_value)
            assert parsed["string"] == "value"
            assert parsed["number"] == 123
            assert parsed["boolean"] is True
            assert parsed["list"] == [1, 2, 3]
            assert parsed["nested"]["inner"] == "data"


class TestUsingTags:
    """Test using_tags context manager."""

    def test_using_tags_sync_context(self):
        """Test using_tags as synchronous context manager."""
        tags = ["tag1", "tag2", "tag3"]
        
        with using_tags(tags):
            current_value = get_value(SpanAttributes.TAG_TAGS)
            assert current_value == tags
        
        final_value = get_value(SpanAttributes.TAG_TAGS)
        assert final_value is None

    @pytest.mark.asyncio
    async def test_using_tags_async_context(self):
        """Test using_tags as asynchronous context manager."""
        tags = ["async_tag1", "async_tag2"]
        
        async with using_tags(tags):
            current_value = get_value(SpanAttributes.TAG_TAGS)
            assert current_value == tags
        
        final_value = get_value(SpanAttributes.TAG_TAGS)
        assert final_value is None

    def test_using_tags_empty_list(self):
        """Test using_tags with empty list."""
        tags = []
        
        with using_tags(tags):
            current_value = get_value(SpanAttributes.TAG_TAGS)
            # Empty list doesn't set context value
            assert current_value is None
        
        final_value = get_value(SpanAttributes.TAG_TAGS)
        assert final_value is None


class TestUsingPromptTemplate:
    """Test using_prompt_template context manager."""

    def test_using_prompt_template_full(self):
        """Test using_prompt_template with all parameters."""
        template = "Hello {name}, today is {date}"
        version = "v1.0"
        variables = {"name": "Alice", "date": "2023-01-01"}
        
        with using_prompt_template(
            template=template,
            version=version,
            variables=variables
        ):
            template_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME)
            version_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION)
            variables_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES)
            
            assert template_value == template
            assert version_value == version
            assert variables_value == '{"name": "Alice", "date": "2023-01-01"}'
        
        # Should be cleared after context
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME) is None
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION) is None
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES) is None

    def test_using_prompt_template_partial(self):
        """Test using_prompt_template with partial parameters."""
        template = "Simple template"
        
        with using_prompt_template(template=template):
            template_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME)
            version_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION)
            variables_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES)
            
            assert template_value == template
            assert version_value is None
            assert variables_value is None

    @pytest.mark.asyncio
    async def test_using_prompt_template_async(self):
        """Test using_prompt_template as asynchronous context manager."""
        template = "Async template {param}"
        variables = {"param": "value"}
        
        async with using_prompt_template(template=template, variables=variables):
            template_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME)
            variables_value = get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES)
            
            assert template_value == template
            assert variables_value == '{"param": "value"}'


class TestUsingAttributes:
    """Test using_attributes context manager."""

    def test_using_attributes_all_parameters(self):
        """Test using_attributes with all parameters."""
        session_id = "session-123"
        user_id = "user-456"
        metadata = {"key": "value"}
        tags = ["tag1", "tag2"]
        prompt_template = "Template {var}"
        prompt_template_version = "v2.0"
        prompt_template_variables = {"var": "test"}
        
        with using_attributes(
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            tags=tags,
            prompt_template=prompt_template,
            prompt_template_version=prompt_template_version,
            prompt_template_variables=prompt_template_variables,
        ):
            assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id
            assert get_value(SpanAttributes.USER_ID) == user_id
            assert get_value(SpanAttributes.METADATA) == '{"key": "value"}'
            assert get_value(SpanAttributes.TAG_TAGS) == tags
            assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME) == prompt_template
            assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION) == prompt_template_version
            assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES) == '{"var": "test"}'
        
        # All should be cleared after context
        assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) is None
        assert get_value(SpanAttributes.USER_ID) is None
        assert get_value(SpanAttributes.METADATA) is None
        assert get_value(SpanAttributes.TAG_TAGS) is None
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME) is None
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION) is None
        assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES) is None

    def test_using_attributes_partial_parameters(self):
        """Test using_attributes with partial parameters."""
        session_id = "session-partial"
        metadata = {"partial": "data"}
        
        with using_attributes(session_id=session_id, metadata=metadata):
            assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) == session_id
            assert get_value(SpanAttributes.METADATA) == '{"partial": "data"}'
            assert get_value(SpanAttributes.USER_ID) is None
            assert get_value(SpanAttributes.TAG_TAGS) is None

    def test_using_attributes_empty_strings(self):
        """Test using_attributes with empty strings (should not set values)."""
        with using_attributes(
            session_id="",
            user_id="",
            prompt_template="",
            prompt_template_version="",
        ):
            # Empty strings should not set values
            assert get_value(SpanAttributes.GEN_AI_CONVERSATION_ID) is None
            assert get_value(SpanAttributes.USER_ID) is None
            assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME) is None
            assert get_value(SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION) is None


class TestGetAttributesFromContext:
    """Test get_attributes_from_context function."""

    def test_get_attributes_from_context_empty(self):
        """Test get_attributes_from_context with empty context."""
        attributes = list(get_attributes_from_context())
        assert attributes == []

    def test_get_attributes_from_context_with_session(self):
        """Test get_attributes_from_context with session in context."""
        session_id = "test-session"
        
        with using_session(session_id):
            attributes = list(get_attributes_from_context())
            assert len(attributes) == 1
            assert attributes[0] == (SpanAttributes.GEN_AI_CONVERSATION_ID, session_id)

    def test_get_attributes_from_context_multiple_attributes(self):
        """Test get_attributes_from_context with multiple attributes."""
        session_id = "test-session"
        user_id = "test-user"
        metadata = {"key": "value"}
        tags = ["tag1", "tag2"]
        
        with using_attributes(
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            tags=tags,
        ):
            attributes = dict(get_attributes_from_context())
            
            assert attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == session_id
            assert attributes[SpanAttributes.USER_ID] == user_id
            assert attributes[SpanAttributes.METADATA] == '{"key": "value"}'
            assert attributes[SpanAttributes.TAG_TAGS] == tags

    def test_get_attributes_from_context_with_prompt_template(self):
        """Test get_attributes_from_context with prompt template attributes."""
        template = "Template {var}"
        version = "v1.0"
        variables = {"var": "value"}
        
        with using_prompt_template(
            template=template,
            version=version,
            variables=variables,
        ):
            attributes = dict(get_attributes_from_context())
            
            assert attributes[SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME] == template
            assert attributes[SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION] == version
            assert attributes[SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES] == '{"var": "value"}'

    def test_get_attributes_from_context_nested_contexts(self):
        """Test get_attributes_from_context with nested contexts."""
        session_id1 = "session-1"
        session_id2 = "session-2"
        user_id = "user-123"
        
        with using_session(session_id1):
            with using_user(user_id):
                with using_session(session_id2):
                    attributes = dict(get_attributes_from_context())
                    
                    # Should have the innermost session and the user
                    assert attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == session_id2
                    assert attributes[SpanAttributes.USER_ID] == user_id


class TestContextAttributesConstants:
    """Test context attributes constants."""

    def test_context_attributes_constant(self):
        """Test CONTEXT_ATTRIBUTES constant contains expected attributes."""
        expected_attributes = {
            SpanAttributes.GEN_AI_CONVERSATION_ID,
            SpanAttributes.USER_ID,
            SpanAttributes.METADATA,
            SpanAttributes.TAG_TAGS,
            SpanAttributes.GEN_AI_PROMPT_TEMPLATE_NAME,
            SpanAttributes.GEN_AI_PROMPT_TEMPLATE_LABEL,
            SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VERSION,
            SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES,
            SimulatorAttributes.RUN_TEST_ID,
            SimulatorAttributes.TEST_EXECUTION_ID,
            SimulatorAttributes.CALL_EXECUTION_ID,
            SimulatorAttributes.IS_SIMULATOR_TRACE,
        }

        assert set(CONTEXT_ATTRIBUTES) == expected_attributes 