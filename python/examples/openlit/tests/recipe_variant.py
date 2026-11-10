"""A test-only fixture: ``src/app.py`` as written, with one thing changed.

``RECIPE_INIT_OVERRIDES`` is a JSON object of ``openlit.init()`` keyword
arguments to replace; a ``null`` value removes the argument, so OpenLIT's
own default applies. With ``RECIPE_ADD_TRACEAI_OPENAI=1``, traceAI's
``OpenAIInstrumentor`` is also enabled right after ``openlit.init()``, on the
tracer provider OpenLIT created, so both instrument the same client.
Everything else, including the model call, is the recipe's own code.
Not part of the recipe.
"""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

import openlit

APP = Path(__file__).resolve().parent.parent / "src" / "app.py"
_OVERRIDES = json.loads(os.environ.get("RECIPE_INIT_OVERRIDES", "{}"))
_real_init = openlit.init


def _init(**kwargs):  # type: ignore[no-untyped-def]
    for key, value in _OVERRIDES.items():
        if value is None:
            kwargs.pop(key, None)
        else:
            kwargs[key] = value
    _real_init(**kwargs)
    if os.environ.get("RECIPE_ADD_TRACEAI_OPENAI") == "1":
        from traceai_openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()


openlit.init = _init
runpy.run_path(str(APP), run_name="__main__")
