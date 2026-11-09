"""Real OpenAI/Ollama clients and OTLP serialization, without external providers."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec('cognee') is None,
    reason='install cognee[tracing] for the native contract',
)


def run_native(provider, pii, hidden, capture, model=None):
    tests = Path(__file__).resolve().parent
    harness_parent = tests.parents[2] / 'tests'
    with tempfile.TemporaryDirectory() as directory:
        env = {
            'PATH': os.environ.get('PATH', ''),
            'HOME': directory,
            'COGNEE_TRACING_ENABLED': 'true',
            'TELEMETRY_DISABLED': 'true',
            'MONITORING_TOOL': 'none',
            'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
            'FI_PII_REDACTION': str(pii).lower(),
            'FI_HIDE_INPUTS': str(hidden == 'input').lower(),
            'FI_HIDE_OUTPUTS': str(hidden == 'output').lower(),
        }
        result = subprocess.run(
            [sys.executable, '-I', str(tests / '_native_content_policy.py'),
             str(harness_parent), provider, str(capture).lower(),
             *([model] if model is not None else [])],
            env=env, cwd=directory, capture_output=True, text=True, timeout=120,
        )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('provider', ['openai', 'ollama'])
@pytest.mark.parametrize('pii', [False, True])
@pytest.mark.parametrize('hidden', [None, 'input', 'output'])
def test_native_pii_policy(provider, pii, hidden):
    run_native(provider, pii, hidden, capture=True)


@pytest.mark.parametrize('pii', [False, True])
def test_native_tagged_model_without_content(pii):
    run_native('ollama', pii, hidden=None, capture=False)


@pytest.mark.parametrize('capture', [False, True])
@pytest.mark.parametrize('model', [
    'openai//models/llama-3.2',
    'openai//opt/.cache/llama-3.2.gguf',
    'openai/./models/llama-3.2',
    pytest.param('openai/模型', id='unicode-model'),
    pytest.param('openai//models/模型/llama-3.2', id='unicode-prefixed-path'),
    pytest.param('openai//home/andré/models/llama-3.2', id='unicode-home-path'),
])
def test_native_model_identifiers(capture, model):
    # Real OpenAIAdapter requests (including SSE) must keep their attribution.
    run_native('openai', pii=False, hidden=None, capture=capture, model=model)
