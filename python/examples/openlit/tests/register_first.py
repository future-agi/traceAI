"""A test-only fixture: traceAI's ``register()`` before the recipe's ``init_tracing()``.

``register()`` runs first: with ``set_global_tracer_provider=True`` when
``REGISTER_GLOBAL=1``, else with its default (``False``). Then
``init_tracing()`` from ``src/app.py`` runs as written, and the recipe's
model call runs inside one span from ``register()``'s provider, standing in
for a traceAI instrumentor's span. ``register()`` exports to ``FI_BASE_URL``
under the project ``REGISTER_PROJECT``. traceAI is imported from this
repository's ``python/`` directory. Not part of the recipe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3]))  # python/, for fi_instrumentation
sys.path.insert(0, str(HERE.parents[1] / "src"))

from fi_instrumentation import register  # noqa: E402
from fi_instrumentation.fi_types import ProjectType  # noqa: E402

from app import ask, init_tracing  # noqa: E402

provider = register(
    project_name=os.environ["REGISTER_PROJECT"],
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=os.environ.get("REGISTER_GLOBAL") == "1",
    verbose=False,
)
init_tracing()
with provider.get_tracer("register_first").start_as_current_span("traceai_span"):
    print(ask(sys.argv[1]))
