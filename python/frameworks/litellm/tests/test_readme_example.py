"""Execute the README's first request with only its documented credential names."""

import ast
import re
from pathlib import Path

import httpx
import litellm
import openai


def test_readme_first_request(monkeypatch):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    for name in ("OPENAI_API_KEY", "FI_API_KEY", "FI_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    setup = readme.split("## Quickstart")[0]
    for name in re.findall(r"^export ([A-Z_]+)=", setup, re.MULTILINE):
        monkeypatch.setenv(name, "synthetic-" + name.lower())

    example = None
    for block in re.findall(r"```python\n(.*?)```", readme, re.DOTALL):
        module = ast.parse(block)
        compile(module, "README.md", "exec")
        for node in module.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_examples":
                example = node.body[0].value
    assert example is not None
    kwargs = {
        argument.arg: ast.literal_eval(argument.value) for argument in example.keywords
    }
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-openai_api_key"
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": kwargs["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Beijing"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    with openai.OpenAI(
        http_client=httpx.Client(transport=httpx.MockTransport(respond))
    ) as client:
        response = litellm.completion(**kwargs, client=client)
    assert response.choices[0].message.content == "Beijing"
    assert len(requests) == 1
