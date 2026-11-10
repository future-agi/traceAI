"""A test-only fixture: one OpenAI call made outside any Traceloop decorator.

It shows which attributes a bare model call carries, so the contract test
can check the README's claim that such a span has no ``traceloop.*`` key.
Not part of the recipe.
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI
from traceloop.sdk import Traceloop

Traceloop.init(
    app_name="openllmetry-recipe",
    resource_attributes={
        "project_name": os.environ["FI_PROJECT_NAME"],
        "project_type": "observe",
    },
    telemetry_enabled=False,
)
response = OpenAI().chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": sys.argv[1]}],
)
print(response.choices[0].message.content)
