"""
Shared pytest fixtures for E2E tests.
"""

import pytest
import requests
import uuid
import time
import random
from datetime import datetime
from typing import Generator, Dict, Any, List, Optional

from config import config


@pytest.fixture(scope="session")
def test_config():
    """Return test configuration."""
    return config


@pytest.fixture(scope="session")
def api_session() -> Generator[requests.Session, None, None]:
    """Create a requests session for API calls."""
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    yield session
    session.close()


@pytest.fixture(scope="module")
def test_project(api_session: requests.Session) -> Generator[Dict[str, Any], None, None]:
    """Create a test project and clean up after tests."""
    project_name = f"{config.test_project_prefix}{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # Create project
    response = api_session.post(
        f"{config.tracer_base_url}/project/",
        json={
            "name": project_name,
            "description": "E2E test project",
        },
        timeout=config.request_timeout,
    )

    if response.status_code == 201:
        project = response.json()
    else:
        # Project might already exist or we need auth
        # For now, create a mock project dict
        project = {
            "id": str(uuid.uuid4()),
            "name": project_name,
            "api_key": f"fi-test-{uuid.uuid4().hex}",
        }

    yield project

    # Cleanup
    if config.cleanup_after_tests and "id" in project:
        try:
            api_session.delete(
                f"{config.tracer_base_url}/project/{project['id']}/",
                timeout=config.request_timeout,
            )
        except Exception:
            pass


@pytest.fixture
def unique_trace_id() -> str:
    """Generate a unique trace ID."""
    return f"trace_{uuid.uuid4().hex}"


@pytest.fixture
def unique_span_id() -> str:
    """Generate a unique span ID."""
    return f"span_{uuid.uuid4().hex[:16]}"


# ── Google OpenAI-compat client fixture ──

@pytest.fixture(scope="module")
def google_openai_client():
    """Create an OpenAI client pointed at Google's OpenAI-compatible endpoint.

    This is the primary fixture for testing OpenAI-wrapper instrumentors
    (XAI, Fireworks, DeepSeek, Cerebras, etc.) using only a Google API key.
    """
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from openai import OpenAI

    client = OpenAI(
        base_url=config.google_openai_base_url,
        api_key=config.google_api_key,
    )
    return client


@pytest.fixture(scope="module")
def async_google_openai_client():
    """Create an async OpenAI client pointed at Google's OpenAI-compatible endpoint."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=config.google_openai_base_url,
        api_key=config.google_api_key,
    )
    return client


# ── Vector DB helper fixtures ──

@pytest.fixture
def generate_test_embedding():
    """Generate a deterministic test embedding vector of given dimension."""
    def _generate(dim: int = 128, seed: int = 42) -> List[float]:
        rng = random.Random(seed)
        return [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    return _generate


@pytest.fixture
def sample_documents():
    """Sample documents for vector DB tests."""
    return [
        "The quick brown fox jumps over the lazy dog.",
        "Machine learning is a subset of artificial intelligence.",
        "Python is a popular programming language for data science.",
        "OpenTelemetry provides observability for cloud-native software.",
        "Vector databases store and query high-dimensional embeddings.",
    ]


@pytest.fixture
def sample_embeddings(generate_test_embedding):
    """Pre-generated embeddings for 5 sample documents (dim=128)."""
    return [generate_test_embedding(128, seed=i) for i in range(5)]


# ── Tracer provider fixture ──

@pytest.fixture
def tracer_provider(test_project):
    """Set up OpenTelemetry tracer provider for SDK tests."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # Create provider
    provider = TracerProvider()

    # Add OTLP exporter
    exporter = OTLPSpanExporter(
        endpoint=config.otlp_url,
        headers={
            "fi-api-key": test_project.get("api_key", "test-key"),
        },
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Set as global
    trace.set_tracer_provider(provider)

    yield provider

    # Shutdown
    provider.shutdown()


class TraceVerifier:
    """Helper class to verify traces in the database."""

    def __init__(self, api_session: requests.Session, project_id: str):
        self.session = api_session
        self.project_id = project_id

    def wait_for_trace(
        self,
        trace_id: str,
        timeout: int = 30,
        poll_interval: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        """Wait for a trace to appear in the database."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            response = self.session.get(
                f"{config.tracer_base_url}/trace/{trace_id}/",
                timeout=config.request_timeout,
            )
            if response.status_code == 200:
                return response.json()
            time.sleep(poll_interval)

        return None

    def get_spans_for_trace(self, trace_id: str) -> list:
        """Get all spans for a trace."""
        response = self.session.get(
            f"{config.tracer_base_url}/observation-span/",
            params={"trace_id": trace_id},
            timeout=config.request_timeout,
        )
        if response.status_code == 200:
            return response.json().get("results", [])
        return []

    def verify_span_attributes(
        self,
        span: Dict[str, Any],
        expected_attrs: Dict[str, Any],
    ) -> bool:
        """Verify span has expected attributes."""
        attrs = span.get("attributes", {})
        for key, expected in expected_attrs.items():
            if attrs.get(key) != expected:
                return False
        return True


@pytest.fixture
def trace_verifier(api_session, test_project):
    """Create a TraceVerifier instance."""
    return TraceVerifier(api_session, test_project.get("id", ""))


# ── Markers for conditional test execution ──

def pytest_configure(config):
    """Add custom markers."""
    markers = [
        "openai: mark test as requiring OpenAI API key",
        "anthropic: mark test as requiring Anthropic API key",
        "groq: mark test as requiring Groq API key",
        "bedrock: mark test as requiring AWS Bedrock credentials",
        "vertexai: mark test as requiring Vertex AI credentials",
        "google: mark test as requiring Google API key",
        "google_genai: mark test as requiring Google GenAI SDK",
        "xai: mark test as requiring XAI API key",
        "fireworks: mark test as requiring Fireworks API key",
        "deepseek: mark test as requiring DeepSeek API key",
        "cerebras: mark test as requiring Cerebras API key",
        "cohere: mark test as requiring Cohere API key",
        "mistral: mark test as requiring Mistral API key",
        "together: mark test as requiring Together API key",
        "huggingface: mark test as requiring HuggingFace API key",
        "ollama: mark test as requiring Ollama server",
        "portkey: mark test as requiring Portkey API key",
        "pinecone: mark test as requiring Pinecone API key",
        "litellm: mark test as requiring LiteLLM",
        "langchain: mark test as requiring LangChain",
        "llamaindex: mark test as requiring LlamaIndex",
        "crewai: mark test as requiring CrewAI",
        "autogen: mark test as requiring AutoGen",
        "pydantic_ai: mark test as requiring PydanticAI",
        "instructor: mark test as requiring Instructor",
        "dspy: mark test as requiring DSPy",
        "openai_agents: mark test as requiring OpenAI Agents SDK",
        "haystack: mark test as requiring Haystack",
        "smolagents: mark test as requiring Smolagents",
        "google_adk: mark test as requiring Google ADK",
        "agno: mark test as requiring Agno",
        "strands: mark test as requiring AWS Strands",
        "beeai: mark test as requiring BeeAI",
        "claude_agent_sdk: mark test as requiring Claude Agent SDK",
        "chromadb: mark test as requiring ChromaDB",
        "lancedb: mark test as requiring LanceDB",
        "qdrant: mark test as requiring Qdrant",
        "weaviate: mark test as requiring Weaviate",
        "milvus: mark test as requiring Milvus",
        "pgvector: mark test as requiring pgvector",
        "redis_vector: mark test as requiring Redis + RediSearch",
        "mongodb: mark test as requiring MongoDB",
        "guardrails: mark test as requiring Guardrails AI",
        "mcp_server: mark test as requiring MCP server",
        "vllm: mark test as requiring vLLM server",
        "livekit: mark test as requiring LiveKit",
        "pipecat: mark test as requiring Pipecat",
        "slow: mark test as slow running",
    ]
    for marker in markers:
        config.addinivalue_line("markers", marker)
