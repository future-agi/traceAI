"""Run the customer example against a real local Temporal server and OTLP sink."""

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment


@pytest.mark.integration
def test_customer_example(tmp_path, receiver):
    binary = os.getenv("TEMPORAL_CLI") or shutil.which("temporal")
    if not binary:
        if os.getenv("REQUIRE_TEMPORAL_INTEGRATION") == "1":
            pytest.fail("TEMPORAL_CLI is required")
        pytest.skip("Temporal CLI is needed for the customer example")
    page = Path(
        os.getenv("TEMPORAL_DOCS_DRAFT", Path(__file__).parents[1] / "README.md")
    )
    blocks = re.findall(r"```python\n(.*?)\n```", page.read_text(), flags=re.DOTALL)
    workflow = next(block for block in blocks if "class OrderWorkflow" in block)
    app = next(block for block in blocks if "asyncio.run(main())" in block)
    (tmp_path / "workflows.py").write_text(workflow)
    base, received = receiver

    async def run():
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary, ui=False
        ) as env:
            # Only the server port changes; the example itself is otherwise verbatim.
            target = env.client.service_client.config.target_host
            (tmp_path / "app.py").write_text(app.replace("localhost:7233", target))
            child_env = {
                **os.environ,
                "FI_BASE_URL": base,
                "FI_PROJECT_NAME": "synthetic-docs",
            }
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(tmp_path / "app.py"),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
                assert child.returncode == 0, stderr.decode()
                assert stdout.strip() == b"EUR 42"
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    asyncio.run(run())
    spans = [
        span
        for _, _, body in received
        for resource in body["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert {span["name"] for span in spans} == {
        "StartWorkflow:OrderWorkflow",
        "RunWorkflow:OrderWorkflow",
        "StartActivity:price_order",
        "RunActivity:price_order",
        "calculate-price",
    }
