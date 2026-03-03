"""
E2E Test Configuration

Loads API keys and configuration from environment or .env file.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from fi_instrumentation.fi_types import ProjectType

# .env file locations (first match wins per key, existing env vars take priority)
LOCAL_ENV = Path(__file__).parent / ".env"
BACKEND_ENV = Path(__file__).parent.parent.parent.parent / "core-backend" / ".env"


def _load_env_file(filepath: Path):
    """Load environment variables from a single .env file. Existing vars are NOT overwritten."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key not in os.environ:
                    os.environ[key] = value


def load_env():
    """Load env vars: shell env > local .env > core-backend .env"""
    _load_env_file(LOCAL_ENV)
    _load_env_file(BACKEND_ENV)


# Load env on import
load_env()


@dataclass
class TestConfig:
    """Test configuration."""

    # Backend URL
    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:8000")
    tracer_base_url: str = f"{backend_url}/tracer"
    otlp_url: str = f"{backend_url}/v1/traces"

    # ── Google (primary — used for OpenAI-compat testing) ──
    google_api_key: Optional[str] = os.getenv("GOOGLE_API_KEY")
    google_openai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    google_model: str = "gemini-2.0-flash"

    # ── LLM API Keys ──
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    groq_api_key: Optional[str] = os.getenv("GROQ_API_KEY")
    xai_api_key: Optional[str] = os.getenv("XAI_API_KEY")
    openrouter_api_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    perplexity_api_key: Optional[str] = os.getenv("PERPLEXITY_API_KEY")
    cohere_api_key: Optional[str] = os.getenv("COHERE_API_KEY")
    mistral_api_key: Optional[str] = os.getenv("MISTRAL_API_KEY")
    together_api_key: Optional[str] = os.getenv("TOGETHER_API_KEY")
    fireworks_api_key: Optional[str] = os.getenv("FIREWORKS_API_KEY")
    deepseek_api_key: Optional[str] = os.getenv("DEEPSEEK_API_KEY")
    cerebras_api_key: Optional[str] = os.getenv("CEREBRAS_API_KEY")
    hf_api_key: Optional[str] = os.getenv("HF_API_KEY")
    portkey_api_key: Optional[str] = os.getenv("PORTKEY_API_KEY")
    pinecone_api_key: Optional[str] = os.getenv("PINECONE_API_KEY")

    # ── Infrastructure ──
    ollama_host: Optional[str] = os.getenv("OLLAMA_HOST")

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
    project_name: str = os.getenv("E2E_PROJECT_NAME", "e2e_test_all")
    project_type: ProjectType = (
        ProjectType.OBSERVE
        if os.getenv("E2E_PROJECT_TYPE", "experiment").lower() == "observe"
        else ProjectType.EXPERIMENT
    )
    # OBSERVE projects don't allow a version name
    project_version_name: Optional[str] = (
        None
        if os.getenv("E2E_PROJECT_TYPE", "experiment").lower() == "observe"
        else os.getenv("E2E_PROJECT_VERSION", "1.0.0")
    )
    test_project_prefix: str = "e2e_test_"
    cleanup_after_tests: bool = True
    request_timeout: int = 30

    # ── has_* methods ──

    def has_google(self) -> bool:
        return bool(self.google_api_key)

    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    def has_groq(self) -> bool:
        return bool(self.groq_api_key)

    def has_xai(self) -> bool:
        return bool(self.xai_api_key)

    def has_cohere(self) -> bool:
        return bool(self.cohere_api_key)

    def has_mistral(self) -> bool:
        return bool(self.mistral_api_key)

    def has_together(self) -> bool:
        return bool(self.together_api_key)

    def has_fireworks(self) -> bool:
        return bool(self.fireworks_api_key)

    def has_deepseek(self) -> bool:
        return bool(self.deepseek_api_key)

    def has_cerebras(self) -> bool:
        return bool(self.cerebras_api_key)

    def has_hf(self) -> bool:
        return bool(self.hf_api_key)

    def has_portkey(self) -> bool:
        return bool(self.portkey_api_key)

    def has_pinecone(self) -> bool:
        return bool(self.pinecone_api_key)

    def has_ollama(self) -> bool:
        return bool(self.ollama_host)

    def has_bedrock(self) -> bool:
        return bool(self.aws_access_key_id and self.aws_secret_access_key)

    def has_vertexai(self) -> bool:
        return bool(self.google_cloud_project)


# Global config instance
config = TestConfig()


# ── Skip markers for pytest ──
import pytest

skip_if_no_google = pytest.mark.skipif(
    not config.has_google(), reason="GOOGLE_API_KEY not set"
)

skip_if_no_openai = pytest.mark.skipif(
    not config.has_openai(), reason="OPENAI_API_KEY not set"
)

skip_if_no_anthropic = pytest.mark.skipif(
    not config.has_anthropic(), reason="ANTHROPIC_API_KEY not set"
)

skip_if_no_groq = pytest.mark.skipif(
    not config.has_groq(), reason="GROQ_API_KEY not set"
)

skip_if_no_xai = pytest.mark.skipif(
    not config.has_xai(), reason="XAI_API_KEY not set"
)

skip_if_no_cohere = pytest.mark.skipif(
    not config.has_cohere(), reason="COHERE_API_KEY not set"
)

skip_if_no_mistral = pytest.mark.skipif(
    not config.has_mistral(), reason="MISTRAL_API_KEY not set"
)

skip_if_no_together = pytest.mark.skipif(
    not config.has_together(), reason="TOGETHER_API_KEY not set"
)

skip_if_no_fireworks = pytest.mark.skipif(
    not config.has_fireworks(), reason="FIREWORKS_API_KEY not set"
)

skip_if_no_deepseek = pytest.mark.skipif(
    not config.has_deepseek(), reason="DEEPSEEK_API_KEY not set"
)

skip_if_no_cerebras = pytest.mark.skipif(
    not config.has_cerebras(), reason="CEREBRAS_API_KEY not set"
)

skip_if_no_hf = pytest.mark.skipif(
    not config.has_hf(), reason="HF_API_KEY not set"
)

skip_if_no_portkey = pytest.mark.skipif(
    not config.has_portkey(), reason="PORTKEY_API_KEY not set"
)

skip_if_no_pinecone = pytest.mark.skipif(
    not config.has_pinecone(), reason="PINECONE_API_KEY not set"
)

skip_if_no_ollama = pytest.mark.skipif(
    not config.has_ollama(), reason="OLLAMA_HOST not set"
)

skip_if_no_bedrock = pytest.mark.skipif(
    not config.has_bedrock(), reason="AWS credentials not set"
)

skip_if_no_vertexai = pytest.mark.skipif(
    not config.has_vertexai(), reason="Google Cloud not configured"
)
