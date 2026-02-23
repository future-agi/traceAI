"""
E2E Tests for LangChain Instrumentation

Tests LangChain chains, agents, and tools with tracing.
"""

import pytest
import os
import time
from typing import Dict, Any

from config import config, skip_if_no_openai


@pytest.fixture(scope="module")
def setup_langchain():
    """Set up LangChain with instrumentation."""
    if not config.has_openai():
        pytest.skip("OpenAI API key required for LangChain tests")

    os.environ["OPENAI_API_KEY"] = config.openai_api_key

    # Import and instrument
    from fi_instrumentation import register
    try:
        from traceai_langchain import LangChainInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_langchain not installed or incompatible")

    # Register tracer
    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
    )

    # Instrument LangChain
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    # Cleanup
    LangChainInstrumentor().uninstrument()


@skip_if_no_openai
class TestLangChainLLM:
    """Test LangChain LLM integration."""

    def test_basic_llm_invoke(self, setup_langchain):
        """Test basic LLM invocation."""
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=50)
        response = llm.invoke("Say hello in one word.")

        assert response.content is not None
        time.sleep(2)
        print(f"Response: {response.content}")

    def test_llm_with_system_message(self, setup_langchain):
        """Test LLM with system message."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=30)
        messages = [
            SystemMessage(content="You respond in exactly one word."),
            HumanMessage(content="What is 2+2?"),
        ]
        response = llm.invoke(messages)

        assert response.content is not None

    def test_llm_streaming(self, setup_langchain):
        """Test LLM streaming."""
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=50, streaming=True)

        chunks = []
        for chunk in llm.stream("Count from 1 to 5."):
            if chunk.content:
                chunks.append(chunk.content)

        assert len(chunks) > 0
        print(f"Streamed: {''.join(chunks)}")


@skip_if_no_openai
class TestLangChainChains:
    """Test LangChain chains."""

    def test_simple_chain(self, setup_langchain):
        """Test simple LCEL chain."""
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        prompt = ChatPromptTemplate.from_messages([
            ("system", "You are a helpful assistant."),
            ("user", "{question}"),
        ])

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=50)
        chain = prompt | llm | StrOutputParser()

        result = chain.invoke({"question": "What is the capital of France?"})

        assert "Paris" in result
        print(f"Chain result: {result}")

    def test_chain_with_multiple_steps(self, setup_langchain):
        """Test chain with multiple steps."""
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        # First step: translate
        translate_prompt = ChatPromptTemplate.from_messages([
            ("user", "Translate to French: {text}"),
        ])

        # Second step: make formal
        formal_prompt = ChatPromptTemplate.from_messages([
            ("user", "Make this more formal: {text}"),
        ])

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=100)
        parser = StrOutputParser()

        # Chain them
        translate_chain = translate_prompt | llm | parser
        formal_chain = formal_prompt | llm | parser

        # Execute
        translated = translate_chain.invoke({"text": "Hello, how are you?"})
        formal = formal_chain.invoke({"text": translated})

        assert len(formal) > 0
        print(f"Translated: {translated}")
        print(f"Formal: {formal}")


@skip_if_no_openai
class TestLangChainTools:
    """Test LangChain tools and agents."""

    def test_tool_definition(self, setup_langchain):
        """Test tool definition and use."""
        from langchain_core.tools import tool

        @tool
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        result = multiply.invoke({"a": 3, "b": 4})
        assert result == 12

    def test_agent_with_tools(self, setup_langchain):
        """Test agent with tools using ReAct agent."""
        from langchain_openai import ChatOpenAI
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent

        @tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        @tool
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        tools = [add, multiply]
        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=200)

        # Use LangGraph's ReAct agent
        agent = create_react_agent(llm, tools)

        result = agent.invoke({"messages": [("human", "What is 3 + 4?")]})

        # Check final message contains the answer
        final_message = result["messages"][-1].content
        assert "7" in str(final_message)
        print(f"Agent result: {final_message}")


@skip_if_no_openai
class TestLangChainMemory:
    """Test LangChain with memory/history."""

    def test_conversation_with_history(self, setup_langchain):
        """Test conversation with chat history."""
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, AIMessage

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=50)

        history = [
            HumanMessage(content="My name is Alice."),
            AIMessage(content="Nice to meet you, Alice!"),
            HumanMessage(content="What's my name?"),
        ]

        response = llm.invoke(history)
        assert "Alice" in response.content
        print(f"Response: {response.content}")


@skip_if_no_openai
class TestLangChainAsync:
    """Test async LangChain operations."""

    @pytest.mark.asyncio
    async def test_async_invoke(self, setup_langchain):
        """Test async LLM invocation."""
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=20)
        response = await llm.ainvoke("Say 'async test' briefly.")

        assert response.content is not None

    @pytest.mark.asyncio
    async def test_async_streaming(self, setup_langchain):
        """Test async streaming."""
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model="gpt-4o-mini", max_tokens=50, streaming=True)

        chunks = []
        async for chunk in llm.astream("Count 1 to 3."):
            if chunk.content:
                chunks.append(chunk.content)

        assert len(chunks) > 0
