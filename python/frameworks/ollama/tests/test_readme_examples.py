"""Execute the documented RAG example with a real SDK and offline transport."""
import json
from pathlib import Path

import httpx
import ollama


def test_rag_example_defines_context(monkeypatch):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    example = readme.split("### RAG Pipeline", 1)[1].split("```python", 1)[1].split("```", 1)[0]
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "model": "nomic-embed-text"})
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={
            "model": "llama3.2", "done": True,
            "message": {"role": "assistant", "content": "Synthetic example response"},
        })

    client = ollama.Client(host="http://offline.invalid", transport=httpx.MockTransport(respond), trust_env=False)
    monkeypatch.setattr(ollama, "embed", client.embed)
    monkeypatch.setattr(ollama, "chat", client.chat)
    namespace = {}
    try:
        exec(compile(example, "README.md:RAG Pipeline", "exec"), namespace)
    finally:
        client._client.close()
    assert [path for path, _ in requests] == ["/api/embed", "/api/chat"]
    assert namespace["query_embedding"]["embeddings"][0] == [0.1, 0.2]
    assert isinstance(namespace["context"], str) and namespace["context"]
    assert requests[1][1]["messages"] == [
        {"role": "system", "content": f"Use this context: {namespace['context']}"},
        {"role": "user", "content": namespace["query"]},
    ]
    assert namespace["response"]["message"]["content"] == "Synthetic example response"
