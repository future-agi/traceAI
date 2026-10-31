import ast
import inspect
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Generator, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
import traceai_openwebui
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, SecretStr
from starlette.responses import Response, StreamingResponse


@pytest.fixture
def setup_pipe(monkeypatch):
    modules = {}
    for name in (
        "open_webui",
        "open_webui.models",
        "open_webui.models.users",
        "open_webui.utils",
        "open_webui.utils.chat",
    ):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    user = SimpleNamespace(
        id="synthetic-user",
        role="user",
        model_dump=lambda: {"id": "synthetic-user", "email": "secret@example.invalid"},
    )
    modules["open_webui.models.users"].Users = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=user)
    )
    delegate = AsyncMock(
        return_value={
            "choices": [{"message": {"content": "private-output"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
    )
    modules["open_webui.utils.chat"].generate_chat_completion = delegate
    monkeypatch.setattr(traceai_openwebui, "version", lambda name: "0.12.5")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    pipe = traceai_openwebui.Pipe()
    pipe.valves.target_model = "synthetic"
    pipe.valves.api_key = SecretStr("synthetic-api")
    pipe.valves.secret_key = SecretStr("synthetic-secret")
    monkeypatch.setattr(
        pipe._providers, "acquire", lambda key: (provider, lambda: None)
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                MODELS={
                    "synthetic": {"id": "synthetic", "owned_by": "openai"},
                    "traceai_openwebui": {
                        "id": "traceai_openwebui",
                        "pipe": True,
                        "owned_by": "openai",
                    },
                }
            )
        ),
        state=SimpleNamespace(),
    )
    body = {
        "model": "traceai_openwebui",
        "messages": [{"role": "user", "content": "private-input"}],
    }
    yield SimpleNamespace(
        pipe=pipe,
        request=request,
        body=body,
        user=user,
        delegate=delegate,
        modules=modules,
        provider=provider,
        exporter=exporter,
    )
    provider.shutdown()


@pytest.fixture
def host(setup_pipe, monkeypatch):
    s = setup_pipe
    path = os.environ.get("OPENWEBUI_WHEEL")
    if not path:
        pytest.skip(
            "Set OPENWEBUI_WHEEL to run the real released-host dispatch contract"
        )
    with ZipFile(Path(path)) as wheel:
        metadata_path = next(
            n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")
        )
        version = next(
            line[9:]
            for line in wheel.read(metadata_path).decode().splitlines()
            if line.startswith("Version: ")
        )
        monkeypatch.setattr("traceai_openwebui.version", lambda name: version)
        ns = {
            "Any": Any,
            "Request": Any,
            "Response": Response,
            "StreamingResponse": StreamingResponse,
            "inspect": inspect,
            "time": time,
            "uuid": uuid,
            "BaseModel": BaseModel,
            "JSONCodec": SimpleNamespace(dumps=json.dumps, loads=json.loads),
            "Generator": Generator,
            "AsyncGenerator": AsyncGenerator,
            "Iterator": Iterator,
            "BYPASS_MODEL_ACCESS_CONTROL": False,
            "BYPASS_ADMIN_ACCESS_CONTROL": False,
            "log": logging.getLogger("host_contract"),
            "UserModel": type(s.user),
            "Models": SimpleNamespace(get_model_by_id=AsyncMock(return_value=None)),
            "get_system_oauth_token": AsyncMock(return_value=None),
            "get_event_emitter": AsyncMock(return_value=None),
            "get_event_call": AsyncMock(return_value=None),
            "check_model_access": AsyncMock(),
            "get_function_module_by_id": AsyncMock(return_value=s.pipe),
            "generate_openai_chat_completion": s.delegate,
        }
        for filename, name in (
            ("open_webui/functions.py", "generate_function_chat_completion"),
            ("open_webui/utils/chat.py", "generate_chat_completion"),
            ("open_webui/utils/misc.py", "openai_chat_message_template"),
            ("open_webui/utils/misc.py", "openai_chat_completion_message_template"),
            ("open_webui/utils/misc.py", "openai_chat_chunk_message_template"),
        ):
            module = ast.parse(wheel.read(filename).decode(), filename=filename)
            node = next(
                n
                for n in module.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name
            )
            # Compile the entire unchanged function, never rewritten hook snippets.
            exec(  # noqa: S102 - execute released host contract only in isolated tests
                compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"), ns
            )
    s.request.cookies = {}
    s.modules["open_webui.utils.chat"].generate_chat_completion = ns[
        "generate_chat_completion"
    ]
    return s, ns
