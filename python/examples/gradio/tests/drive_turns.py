"""Scenario script: run the recipe's ``predict`` for chat turns, then exit.

    python drive_turns.py <message> [--content] [--reload]

Loads ``src/app.py`` as a module, so its ``demo.launch()`` does not run and no
port is opened, then calls ``init_tracing()`` as the app's ``__main__`` block
does and calls ``predict`` twice with one Gradio session hash. Spans leave the
process only through ``register()``'s exit flush (the test sets a long batch
delay). Prints one JSON line with counts only, never the prompt.

``--content`` calls ``init_tracing(trace_content=True)`` instead.

``--reload`` replays Gradio 6.29.1's reload mode. Its watcher thread
(``gradio/utils.py`` ``watchfn``) runs the app's source again in the same
process: once at startup, before any save, and once after each save. Each
run is an ``exec`` of the file into the same module namespace with
``__name__ == "__main__"``, on a non-main thread marked as the reload
thread, so the app's ``__main__`` block runs again and ``demo.launch()``
returns at once (``Blocks.launch``'s reload-thread check). This script does
the startup run before the first turn and a save run between the two turns.
The saved source carries one edit, ``init_tracing()`` ->
``init_tracing(trace_content=True)``, as if the developer had turned content
on and saved. The second turn then uses the re-created ``predict``. This
replays the reload's effect on tracing; it is not a ``gradio app.py`` run.

Prints one JSON line: the turn count and the ``project_version_id`` of the
provider ``init_tracing()`` returned first (a fresh UUID per ``register()``
call), never the prompt.

Not part of the recipe.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import gradio as gr

APP = Path(__file__).resolve().parents[1] / "src" / "app.py"
SESSION_HASH = "drive-session-1"


def load_app():
    spec = importlib.util.spec_from_file_location("gradio_recipe_app", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rerun_like_gradio(module, source: str) -> None:
    """Run ``source`` into ``module`` the way Gradio's reload watcher does."""

    def rerun() -> None:
        from gradio.cli.commands.reload import reload_thread

        reload_thread.running_reload = True
        module.__dict__["__name__"] = "__main__"
        exec(compile(source, str(APP), "exec"), module.__dict__)

    thread = threading.Thread(target=rerun)
    thread.start()
    thread.join()


def main(argv: list[str]) -> int:
    message = argv[1]
    app = load_app()
    provider = app.init_tracing(trace_content="--content" in argv)
    request = gr.Request(session_hash=SESSION_HASH)
    report = {"first_provider": provider.resource.attributes["project_version_id"]}

    source = APP.read_text(encoding="utf-8")
    assert source.count("init_tracing()") == 1, "expected one init_tracing() call in app.py"
    if "--reload" in argv:
        # Gradio's watcher runs the file once at startup, before any save.
        rerun_like_gradio(app, source)

    first = app.predict(message, [], request)
    history = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": first},
    ]
    if "--reload" in argv:
        # A save that turned content on.
        rerun_like_gradio(
            app, source.replace("init_tracing()", "init_tracing(trace_content=True)")
        )
    second = app.predict(message, history, request)

    report.update(turns=2, answered=[bool(first), bool(second)])
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
