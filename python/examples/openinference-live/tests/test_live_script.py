"""Static checks for live_e2e.py. The script itself needs a Future AGI stack and an LLM key, so CI never runs it."""
import ast
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "live_e2e.py").read_text()


def test_compiles():
    ast.parse(SRC)


def test_exports_uncompressed_with_project_and_key_headers_from_env():
    assert "Compression.NoCompression" in SRC
    assert '"project_name": project' in SRC
    for name in ("FI_BASE_URL", "FI_API_KEY", "FI_SECRET_KEY"):
        assert f'os.environ["{name}"]' in SRC


def test_no_hardcoded_secret_or_local_path():
    assert not re.search(r"sk-[A-Za-z0-9]{10,}|/Users/|127\.0\.0\.1", SRC)


def test_has_success_tool_retriever_and_two_failure_cases():
    for needle in ('openinference_span_kind="agent"', 'openinference_span_kind="retriever"',
                   "@tracer.tool", "broken_tool", "no-such-model"):
        assert needle in SRC
