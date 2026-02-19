"""
E2E Test Configuration

Loads API keys and configuration from environment or .env file.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Try to load from core-backend .env
ENV_FILE = Path(__file__).parent.parent.parent.parent / "core-backend" / ".env"


def load_env():
    """Load environment variables from .env file."""
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key not in os.environ:
                        os.environ[key] = value


# Load env on import
load_env()


@dataclass
class TestConfig:
    """Test configuration."""

    # Backend URL
    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:8000")
    tracer_base_url: str = f"{backend_url}/tracer"
    otlp_url: str = f"{backend_url}/v1/traces"

    # LLM API Keys
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    groq_api_key: Optional[str] = os.getenv("GROQ_API_KEY")
    xai_api_key: Optional[str] = os.getenv("XAI_API_KEY")
    openrouter_api_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    perplexity_api_key: Optional[str] = os.getenv("PERPLEXITY_API_KEY")

    # AWS (Bedrock)
    aws_access_key_id: Optional[str] = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_bedrock_region: str = os.getenv("AWS_BEDROCK_REGION", "us-west-2")

    # Google (Vertex AI)
    google_cloud_project: Optional[str] = os.getenv("GOOGLE_CLOUD_PROJECT")
    google_application_credentials: Optional[str] = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    # Database
    pg_host: str = os.getenv("PG_HOST", "localhost")
    pg_port: str = os.getenv("PG_PORT", "5432")
    pg_db: str = os.getenv("PG_DB", "tfc")
    pg_user: str = os.getenv("PG_USER", "user")
    pg_password: str = os.getenv("PG_PASSWORD", "password")

    # ClickHouse
    ch_host: Optional[str] = os.getenv("CH_HOST")
    ch_port: str = os.getenv("CH_PORT", "9000")
    ch_database: str = os.getenv("CH_DATABASE", "default")

    # Test settings
    test_project_prefix: str = "e2e_test_"
    cleanup_after_tests: bool = True
    request_timeout: int = 30

    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    def has_groq(self) -> bool:
        return bool(self.groq_api_key)

    def has_bedrock(self) -> bool:
        return bool(self.aws_access_key_id and self.aws_secret_access_key)

    def has_vertexai(self) -> bool:
        return bool(self.google_cloud_project)


# Global config instance
config = TestConfig()


# Skip markers for pytest
import pytest

skip_if_no_openai = pytest.mark.skipif(
    not config.has_openai(), reason="OPENAI_API_KEY not set"
)

skip_if_no_anthropic = pytest.mark.skipif(
    not config.has_anthropic(), reason="ANTHROPIC_API_KEY not set"
)

skip_if_no_groq = pytest.mark.skipif(
    not config.has_groq(), reason="GROQ_API_KEY not set"
)

skip_if_no_bedrock = pytest.mark.skipif(
    not config.has_bedrock(), reason="AWS credentials not set"
)

skip_if_no_vertexai = pytest.mark.skipif(
    not config.has_vertexai(), reason="Google Cloud not configured"
)
