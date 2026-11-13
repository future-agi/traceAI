"""Run customer examples with a local exporter instead of Cloud credentials."""

from pathlib import Path
import re
import runpy

import fi_instrumentation
import pytest
from qdrant_client import QdrantClient

from traceai_qdrant import QdrantInstrumentor


@pytest.mark.skipif(not hasattr(QdrantClient, "query_points"), reason="Examples require qdrant-client>=1.10.1")
@pytest.mark.parametrize("example", ["readme", "similarity_search"])
def test_examples(example, tracer_provider, in_memory_span_exporter, monkeypatch):
    monkeypatch.setattr(fi_instrumentation, "register", lambda **kwargs: tracer_provider)
    package = Path(__file__).resolve().parents[1]
    try:
        if example == "readme":
            namespace = {}
            for source in re.findall(r"```python\n(.*?)```", (package / "README.md").read_text(), re.DOTALL):
                exec(compile(source, str(package / "README.md"), "exec"), namespace)
        else:
            namespace = runpy.run_path(str(package / "examples" / "similarity_search.py"), run_name="__main__")
            namespace["client"].close()
        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 4
        assert all(s.status.status_code.name == "OK" for s in spans)
    finally:
        QdrantInstrumentor().uninstrument()
