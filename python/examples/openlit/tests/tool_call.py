"""A test-only fixture: one tool-using model call after the recipe's ``init_tracing()``.

It runs ``init_tracing()`` from ``src/app.py`` as written (content off), then
makes one Chat Completions call with the recipe's messages, a ``tools``
list and ``user`` set to ``RECIPE_USER``. The fake answers with a tool call,
so the contract test can show what OpenLIT exports for tool calls and for
``user`` with content off. Not part of the recipe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openai import OpenAI  # noqa: E402

from app import _POLICY, MODEL, init_tracing  # noqa: E402

LOOKUP_ORDER = {
    "type": "function",
    "function": {
        "name": "lookup_order",
        "description": "Find a customer's latest order.",
        "parameters": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
}

init_tracing()
response = OpenAI().chat.completions.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": "Answer using only this policy: " + _POLICY},
        {"role": "user", "content": sys.argv[1]},
    ],
    tools=[LOOKUP_ORDER],
    user=os.environ["RECIPE_USER"],
)
print(response.choices[0].message.tool_calls[0].function.name)
