"""Run the maintained example with optional content capture in a fresh process."""
import importlib.util
import os
from pathlib import Path
import sys

import traceai_cognee

app_path = Path(__file__).resolve().parents[1] / "src" / "app.py"
spec = importlib.util.spec_from_file_location("cognee_example", app_path)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# Keep the application path unchanged; opt in only for the capture regression.
if os.environ["TEST_CAPTURE_CONTENT"] == "true":
    app.init_tracing = lambda: traceai_cognee.configure(
        project_name=os.environ["FI_PROJECT_NAME"], capture_content=True
    )
sys.exit(app.main([str(app_path), *sys.argv[1:]]))
