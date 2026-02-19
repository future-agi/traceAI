"""
Shared pytest fixtures for E2E tests.
"""

import pytest
import requests
import uuid
import time
from datetime import datetime
from typing import Generator, Dict, Any, Optional

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


# Markers for conditional test execution
def pytest_configure(config):
    """Add custom markers."""
    config.addinivalue_line(
        "markers", "openai: mark test as requiring OpenAI API key"
    )
    config.addinivalue_line(
        "markers", "anthropic: mark test as requiring Anthropic API key"
    )
    config.addinivalue_line(
        "markers", "groq: mark test as requiring Groq API key"
    )
    config.addinivalue_line(
        "markers", "bedrock: mark test as requiring AWS Bedrock credentials"
    )
    config.addinivalue_line(
        "markers", "vertexai: mark test as requiring Vertex AI credentials"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
