"""A test-only probe: build a Gradio app with Gradio's default analytics setting.

Run under ``_guarded_run.py`` with ``GRADIO_ANALYTICS_ENABLED`` unset, the
guard log shows whether building the app tries to reach Gradio's analytics
host. That is why ``src/app.py`` passes ``analytics_enabled=False``.
Not part of the recipe.
"""

from __future__ import annotations

import gradio as gr

gr.ChatInterface(lambda message, history: message)
# Gradio sends its analytics from background threads; the interpreter waits
# for them before exiting.
