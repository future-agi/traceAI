"""Identical tiny application body for all three adapters (setup markers only)."""
import base64
import hashlib
import ipaddress
import json
import os
import socket
import time

# Defense in depth in addition to explicit URLs and closed proxies; not a sandbox.
_original_connect = socket.socket.connect
_original_getaddrinfo = socket.getaddrinfo


def check_host(host):
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("blocked non-loopback network destination")


def guarded_connect(sock, address):
    if isinstance(address, tuple):
        check_host(address[0])
    return _original_connect(sock, address)


def guarded_resolve(host, *args, **kwargs):
    check_host(host)
    return _original_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = guarded_connect
socket.getaddrinfo = guarded_resolve
# INSTRUMENTATION_SETUP

scenario = os.environ["BENCH_SCENARIO"]
base = os.environ["BENCH_PROVIDER"]
key = os.environ["OPENAI_API_KEY"]
text = "compare-input-canary"
model = "gpt-4o-mini"
client = None
if scenario == "anthropic_stream":
    from anthropic import Anthropic
    client = Anthropic(api_key=key, base_url=base, max_retries=0, timeout=10)
elif scenario == "gemini":
    from google import genai
    client = genai.Client(api_key=key, http_options={"base_url": base, "api_version": "v1beta", "timeout": 10000})
elif scenario == "langchain":
    from langchain_openai import ChatOpenAI
    client = ChatOpenAI(api_key=key, base_url=base + "/v1", model=model, max_retries=0, timeout=10)
elif scenario == "raw_http":
    import httpx
    client = httpx.Client(timeout=10)
elif scenario == "unsupported_sdk":
    from together import Together
    client = Together(api_key=key, base_url=base + "/v1", max_retries=0, timeout=10)
else:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base + "/v1", max_retries=0, timeout=10)


def dump(value):
    return value.model_dump(mode="json", exclude_none=True)


def invoke(prompt):
    first = None
    first_chunk = None
    if scenario == "anthropic_stream":
        chunks = []
        for chunk in client.messages.create(model="claude-sonnet-4-5", max_tokens=20,
                                            messages=[{"role": "user", "content": prompt}], stream=True):
            if first_chunk is None:
                first_chunk = time.perf_counter_ns()
            if getattr(getattr(chunk, "delta", None), "text", None) and first is None:
                first = time.perf_counter_ns()
            chunks.append(dump(chunk))
        return {"chunks": chunks}, first, first_chunk
    if scenario == "gemini":
        return dump(client.models.generate_content(model="gemini-2.0-flash", contents=prompt)), first, first_chunk
    if scenario == "embeddings":
        return dump(client.embeddings.create(model="text-embedding-3-small", input=prompt, encoding_format="float")), first, first_chunk
    if scenario == "transcription":
        result = client.audio.transcriptions.create(model="whisper-1", response_format="verbose_json",
            file=("compare.wav", base64.b64decode(os.environ["BENCH_MEDIA"]), "audio/wav"))
        return dump(result), first, first_chunk
    if scenario == "tts":
        result = client.audio.speech.create(model="tts-1", input=prompt, voice="alloy", response_format="wav")
        body = result.read(); result.close()
        return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}, first, first_chunk
    if scenario == "langchain":
        response = dump(client.invoke(prompt))
        response.pop("id", None)  # LangChain generates a different random run ID each invocation.
        return response, first, first_chunk
    if scenario == "raw_http":
        result = client.post(base + "/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                             json={"model": model, "messages": [{"role": "user", "content": prompt}]})
        result.raise_for_status()
        return result.json(), first, first_chunk
    kwargs = {"model": "compare-error" if scenario == "openai_error" else model,
              "messages": [{"role": "user", "content": prompt}]}
    if scenario == "tool_calls":
        kwargs["tools"] = [{"type": "function", "function": {"name": "lookup_weather", "description": "Look up weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    if scenario == "openai_stream":
        chunks = []
        with client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True}) as stream:
            for chunk in stream:
                if first_chunk is None:
                    first_chunk = time.perf_counter_ns()
                if chunk.choices and getattr(chunk.choices[0].delta, "content", None) and first is None:
                    first = time.perf_counter_ns()
                chunks.append(dump(chunk))
        return {"chunks": chunks}, first, first_chunk
    try:
        return dump(client.chat.completions.create(**kwargs)), first, first_chunk
    except Exception as exc:
        if scenario != "openai_error":
            raise
        return {"error_status": getattr(exc, "status_code", None), "body": getattr(exc, "body", None)}, first, first_chunk


# MANUAL_WRAPPER
print(json.dumps({"kind": "ready"}), flush=True)
for i in range(int(os.environ["BENCH_N"]) + int(os.environ["BENCH_WARMUP"])):
    epoch_start = time.time_ns()
    start = time.perf_counter_ns()
    result, first, first_chunk = invoke(text)
    end = time.perf_counter_ns()
    epoch_end = time.time_ns()
    print(json.dumps({"kind": "call", "i": i, "start_unix_ns": epoch_start, "end_unix_ns": epoch_end,
                      "warmup": i < int(os.environ["BENCH_WARMUP"]),
                      "latency_us": (end - start) / 1000,
                      "ttft_us": None if first is None else (first - start) / 1000,
                      "ttfc_us": None if first_chunk is None else (first_chunk - start) / 1000,
                      "first_text_unix_ns": None if first is None else epoch_start + first - start,
                      "first_chunk_unix_ns": None if first_chunk is None else epoch_start + first_chunk - start,
                      "response": result}, ensure_ascii=False, default=str), flush=True)
time.sleep(float(os.environ["BENCH_SETTLE"]))
# FLUSH_SETUP
print(json.dumps({"kind": "done"}), flush=True)
