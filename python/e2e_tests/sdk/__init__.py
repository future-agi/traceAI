"""SDK instrumentation E2E tests.

Covers 46 instrumentors across 4 categories:
- LLM Providers: OpenAI, Anthropic, Groq, Google GenAI, LiteLLM, XAI,
  Fireworks, DeepSeek, Cerebras, Cohere, Mistral, Together, Ollama,
  HuggingFace, Bedrock, Vertex AI
- Agent Frameworks: LangChain, LlamaIndex, CrewAI, AutoGen, PydanticAI,
  Instructor, DSPy, OpenAI Agents, Haystack, Smolagents, Google ADK,
  Agno, Strands, BeeAI, Claude Agent SDK
- Vector DBs: ChromaDB, LanceDB, Qdrant, Pinecone, Weaviate, Milvus,
  pgvector, Redis, MongoDB
- Infrastructure: Guardrails, MCP, vLLM, Portkey, LiveKit, Pipecat
"""
