"""E2B only: real Hermes CLI + native installer, loopback model and OTLP.

Requires built unified CLI at /work/typescript and pinned Hermes at /work/host.
Writes synthetic evidence to /work/evidence. No account credentials are used.
"""
import gzip
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pexpect
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path("/work")
OUT = ROOT / "evidence"
OUT.mkdir(exist_ok=True)
HOME = ROOT / "isolated-home"
PROFILE = HOME / "profile"
PROFILE.mkdir(parents=True, exist_ok=True)
PROFILE.chmod(0o700)
PROJECT = ROOT / "sample"
PROJECT.mkdir(exist_ok=True)
(PROJECT / "README.txt").write_text("The sample project ships a tracing plugin.\n")
spans, requests, validation = [], [], []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "hermes-test", "object": "model"}]}).encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        if self.path == "/tracer/v1/traces":
            assert self.headers["X-Api-Key"] == "dummy-fi-api" and self.headers["X-Secret-Key"] == "dummy-fi-secret"
            assert self.headers["X-Project-Name"] == "coding-agents"
            if "json" in self.headers.get("content-type", ""):
                data = json.loads(raw)
            else:
                payload = ExportTraceServiceRequest()
                payload.ParseFromString(raw)
                data = MessageToDict(payload)
            for r in data.get("resourceSpans", []):
                for scope in r.get("scopeSpans", []):
                    spans.extend({**s, "resource": r["resource"]} for s in scope["spans"])
            (OUT / "traces.json").write_text(json.dumps(spans, indent=2))
            if not data.get("resourceSpans"):
                validation.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path == "/api/show":
            # Hermes probes loopback model servers for Ollama metadata. This
            # is not a generation endpoint or a vendor fallback.
            self.send_response(404)
            self.end_headers()
            return
        assert self.path == "/v1/chat/completions", self.path
        body = json.loads(raw)
        requests.append({"path": self.path, "model": body.get("model"), "stream": body.get("stream", False)})
        messages = body.get("messages", [])
        # Each resumed prompt gets a fresh tool loop; earlier tool results do
        # not shortcut the second turn.
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        has_tool = any(m.get("role") == "tool" for m in messages[last_user + 1:])
        message = {"role": "assistant", "content": "The tracing plugin is ready." if has_tool else None}
        latest = str(messages[last_user].get("content", "")) if last_user >= 0 else ""
        if "PROVIDER_ERROR_TEST" in latest or ("RECOVER_TEST" in latest and not getattr(server, "recovered_once", False)):
            retry = "RECOVER_TEST" in latest
            if retry: server.recovered_once = True
            self.send_response(429 if retry else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "synthetic provider failure",
                "type": "rate_limit_error" if retry else "invalid_request_error"}}).encode())
            return
        if "CHILD_ONLY" in latest:
            time.sleep(3)  # prove ancestry survives the parent finishing first
        if not has_tool:
            tool, arguments = "read_file", {"path": str(PROJECT / ("not-found.txt" if "MISSING_FILE_TEST" in latest else "README.txt"))}
            if "DELEGATE_TEST" in latest:
                tool, arguments = "delegate_task", {"tasks": [{"goal": "CHILD_ONLY: Read /work/sample/README.txt and summarize it."}]}
            message["tool_calls"] = [{"id": "call-" + str(len(requests)), "type": "function", "function": {
                "name": tool, "arguments": json.dumps(arguments)}}]
        finish = "stop" if has_tool else "tool_calls"
        usage = {"prompt_tokens": 101, "completion_tokens": 7, "total_tokens": 108}
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
            # A non-content role frame precedes model text. First-chunk timing
            # must remain diagnostic, not silently masquerade as first-token time.
            for d, f, u in (({"role": "assistant"}, None, None), (delta, None, None), ({}, finish, usage)):
                chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": "hermes-test", "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
                if u: chunk["usage"] = u
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.15)  # allow the real asynchronous observer to run
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "chatcmpl-test", "object": "chat.completion", "created": int(time.time()),
                "model": "hermes-test", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "OPENAI_", "ANTHROPIC_", "OPENROUTER_"))}
env.update(HOME=str(HOME), HERMES_HOME=str(PROFILE), XDG_CONFIG_HOME=str(HOME / "config"),
    XDG_STATE_HOME=str(HOME / "state"), FI_API_KEY="dummy-fi-api", FI_SECRET_KEY="dummy-fi-secret",
    FI_BASE_URL=base, FI_PROJECT_NAME="coding-agents", OPENAI_API_KEY="dummy-model-key", OPENAI_BASE_URL=base + "/v1",
    PATH="/work/host/.venv/bin:" + env["PATH"], UV_FIND_LINKS="/work/wheels", PIP_FIND_LINKS="/work/wheels",
    NO_COLOR="1", TERM="dumb")
cli = "/work/consumer/node_modules/@traceai/agents/dist/cli.js"
# Setup inherits a real PTY; only dummy-profile dependency prompts are answered.
bootstrap = "const adapter=require('/work/consumer/node_modules/@traceai/hermes-agent/dist/index.js'); adapter.hermesAdapter=adapter.createHermesAdapter('file:///work/native-origin#python/frameworks/hermes-agent/hermes-plugin/futureagi'); require(" + json.dumps(cli) + ").main(['setup','hermes','--project','coding-agents']).then(c=>{process.exitCode=c});"
child = pexpect.spawn("node", ["-e", bootstrap], env=env, cwd=str(PROJECT), encoding="utf-8", timeout=300)
with (OUT / "setup.log").open("w") as log:
    child.logfile = log
    while True:
        hit = child.expect([r"(?i)\[y/n\]", r"(?i)\[Y/n\]", r"(?i)\[y/N\]", pexpect.EOF])
        if hit == 3: break
        child.sendline("y")
child.close()
assert child.exitstatus == 0, f"setup failed: {child.exitstatus}"


def run(args, name, env_override=None, directory=None):
    result = subprocess.run(args, env=env_override or env, cwd=directory or PROJECT, text=True, capture_output=True, timeout=120)
    (OUT / (name + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, f"{name} failed: {result.returncode}"
    return result


# A real repeat setup must not duplicate or re-consent the native plugin.
run(["node", cli, "setup", "hermes", "--project", "coding-agents", "--json"], "setup-repeat")
assert len(json.loads(run(["hermes", "plugins", "list", "--json"], "plugins").stdout)) >= 1
credentials = HOME / "config/futureagi/agents/credentials.json"
assert credentials.stat().st_mode & 0o077 == 0
# Preparation is finished. Deny all new non-loopback egress, retaining the
# sandbox control connection. This applies to the real host and its tools.
for rule in (["-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
             ["-A", "OUTPUT", "-d", "127.0.0.0/8", "-j", "ACCEPT"],
             ["-A", "OUTPUT", "-j", "REJECT"]):
    subprocess.run(["iptables", *rule], check=True)
subprocess.run(["ip6tables", "-A", "OUTPUT", "!", "-d", "::1", "-j", "REJECT"], check=True)
# Prove runtime needs neither Node nor FI shell credentials: the Python plugin
# reads the shared v1 profile, and only the mock model key remains in env.
runtime = {k: v for k, v in env.items() if not k.startswith("FI_")}
no_node = ROOT / "no-node"
no_node.mkdir(exist_ok=True)
(no_node / "node").write_text("#!/bin/sh\ntouch /work/evidence/node-runtime-called\nexit 89\n")
(no_node / "node").chmod(0o700)
runtime["PATH"] = str(no_node) + ":" + runtime["PATH"]
chat = ["hermes", "chat", "--oneshot", "-Q", "--provider", "custom", "--base-url", base + "/v1", "-m", "hermes-test",
    "-t", "file", "--max-turns", "4", "--run-budget", "30"]
run(chat + ["-q", "Read README.txt and summarize it."], "turn-one", runtime)
run(chat + ["--resume", "latest", "-q", "Read README.txt again and summarize it."], "turn-two", runtime)
# Shutdown is a real terminal boundary; no third prompt is needed to export.
assert len(spans) >= 8, f"only {len(spans)} spans; inspect turn logs"
attrs = lambda s: {a["key"]: next(iter(a["value"].values())) for a in s.get("attributes", [])}
roots = [s for s in spans if s["name"] == "hermes.turn"]
models = [s for s in spans if attrs(s).get("gen_ai.span.kind") == "LLM"]
tools = [s for s in spans if attrs(s).get("gen_ai.span.kind") == "TOOL"]
assert len(roots) == 2 and len(models) == 4 and len(tools) == 2, (len(roots), len(models), len(tools))
assert len({attrs(s)["session.id"] for s in roots}) == 1
assert len({s["traceId"] for s in roots}) == 2
assert all(s["parentSpanId"] in {r["spanId"] for r in roots} for s in models + tools)
assert all(int(attrs(s)["gen_ai.usage.total_tokens"]) == 108 for s in models)
assert all(attrs(s).get("tool.name") == s["name"] for s in tools)
assert all("gen_ai.usage.total_tokens" not in attrs(r) for r in roots)
assert all("input.value" in attrs(s) and "output.value" in attrs(s) for s in roots + models + tools)
assert all(attrs(s).get("user.id") for s in roots)
assert all(attrs(s).get("fi.agent.outcome") == "success" for s in roots)
text_models = [s for s in models if int(attrs(s).get("hermes.assistant_content_chars", 0)) > 0]
assert len(text_models) == 2
for s in text_models:
    a = attrs(s)
    assert int(a["gen_ai.server.time_to_first_token"]) >= int(a["hermes.time_to_first_chunk_ms"]) + 100
    assert a["hermes.time_to_first_token.source"] == "on_stream_delta.text"
    assert a["hermes.time_to_first_token.timing"] == "callback-observed"
assert all("gen_ai.server.time_to_first_token" not in attrs(s) for s in models if s not in text_models)
status = run(["node", cli, "status", "hermes", "--check", "--json"], "status")
assert "dummy-fi" not in status.stdout
assert json.loads(status.stdout)["delivery"]["exported"] > 0
# Explicit iteration exhaustion is neither successful completion nor an error.
budget_start = len(spans)
budget_chat = list(chat)
budget_chat[budget_chat.index("--max-turns") + 1] = "1"
budget_result = subprocess.run(budget_chat + ["-q", "BUDGET_TEST: Read README.txt."],
    env=runtime, cwd=PROJECT, text=True, capture_output=True, timeout=120)
(OUT / "iteration-limit.log").write_text(budget_result.stdout + budget_result.stderr)
assert budget_result.returncode == 1
budget_deadline = time.monotonic() + 5
while not any(s["name"] == "hermes.turn" for s in spans[budget_start:]) and time.monotonic() < budget_deadline:
    time.sleep(0.05)
budget_root, = [s for s in spans[budget_start:] if s["name"] == "hermes.turn"]
assert attrs(budget_root)["fi.agent.outcome"] == "incomplete"
assert attrs(budget_root)["hermes.completed"] is False
assert budget_root.get("status", {}).get("code", "STATUS_CODE_UNSET") == "STATUS_CODE_UNSET"
assert "iteration limit" in attrs(budget_root)["output.value"]
baseline_spans = len(spans)
run(chat + ["-q", "MISSING_FILE_TEST: Read the missing file."], "tool-error", runtime)
error_spans = spans[baseline_spans:]
assert any(s["name"] == "read_file" and s.get("status", {}).get("code") == "STATUS_CODE_ERROR" for s in error_spans)
# Current Hermes CLI attaches to a persistent native gateway. Environment
# changes must reach that process, not merely a new CLI client. Stop only this
# disposable profile before changing its launch environment.
run(["hermes", "gateway", "stop"], "stop-before-privacy")
privacy_start = len(spans)
run(chat + ["-q", "Read README.txt without capturing content."], "privacy-off", {**runtime, "FI_AGENT_CAPTURE_CONTENT": "false"})
privacy_spans = spans[privacy_start:]
assert len(privacy_spans) == 4
assert all("input.value" not in attrs(s) and "output.value" not in attrs(s) for s in privacy_spans)
run(["hermes", "gateway", "stop"], "stop-before-delegation")
child_start = len(spans)
delegate_chat = ["file,delegation" if a == "file" else a for a in chat]
run(delegate_chat + ["-q", "DELEGATE_TEST: Delegate reading README to one child."], "delegation", runtime)
child_spans = spans[child_start:]
turns = [s for s in child_spans if s["name"] == "hermes.turn"]
assert len(turns) == 2, f"delegation produced {len(turns)} turns"
parent = next(s for s in turns if not s.get("parentSpanId"))
child_turn = next(s for s in turns if s.get("parentSpanId"))
assert child_turn["parentSpanId"] == parent["spanId"] and child_turn["traceId"] == parent["traceId"]
assert sum(s["name"] == "delegate_task" for s in child_spans) == 1

# A fatal model rejection reaches finalization without on_session_end. A later
# successful retry must clear that error; genuine iteration exhaustion stays distinct.
fatal_start = len(spans)
fatal = subprocess.run(chat + ["-q", "PROVIDER_ERROR_TEST: read README.txt"], env=runtime, cwd=PROJECT,
                       text=True, capture_output=True, timeout=120)
(OUT / "provider-error.log").write_text(fatal.stdout + fatal.stderr)
assert fatal.returncode != 0
fatal_root, = [s for s in spans[fatal_start:] if s["name"] == "hermes.turn"]
assert attrs(fatal_root)["fi.agent.outcome"] == "error" and fatal_root["status"]["code"] == "STATUS_CODE_ERROR"
retry_start = len(spans)
run(chat + ["-q", "RECOVER_TEST: read README.txt"], "recovered-retry", runtime)
retry_spans = spans[retry_start:]
retry_root, = [s for s in retry_spans if s["name"] == "hermes.turn"]
assert attrs(retry_root)["fi.agent.outcome"] == "success"
assert any(attrs(s).get("gen_ai.span.kind") == "LLM" and s.get("status", {}).get("code") == "STATUS_CODE_ERROR" for s in retry_spans)

# --oneshot joins children; only an interactive PTY exercises native automatic
# completion turns. Assert one human-prompt root, immutable ancestry and no usage
# duplication, then a genuine second human prompt in the same interactive session.
async_start = len(spans)
interactive = [a for a in delegate_chat if a not in {"--oneshot", "-Q"}]
child = pexpect.spawn(interactive[0], interactive[1:] + ["-q", "DELEGATE_TEST: Delegate reading README to one child."],
    env={**runtime, "TERM": "xterm-256color"}, cwd=str(PROJECT), encoding="utf-8", timeout=60)
with (OUT / "interactive-delegation.log").open("w") as log:
    child.logfile = log
    def wait_for(predicate):
        deadline = time.monotonic() + 65
        while not predicate() and child.isalive() and time.monotonic() < deadline:
            try: child.read_nonblocking(4096, timeout=0.2)
            except pexpect.TIMEOUT: pass
            except pexpect.EOF: break
        assert predicate(), "native interactive scenario did not reach expected trace boundary"
    try:
        wait_for(lambda: any(s["name"] == "hermes.continuation" for s in spans[async_start:]))
        async_spans = list(spans[async_start:])
        async_roots = [s for s in async_spans if not s.get("parentSpanId")]
        assert len(async_roots) == 1, len(async_roots)
        root = async_roots[0]
        assert all(s["traceId"] == root["traceId"] for s in async_spans)
        continuation, = [s for s in async_spans if s["name"] == "hermes.continuation"]
        assert continuation["parentSpanId"] == root["spanId"]
        assert sum(attrs(s).get("gen_ai.span.kind") == "LLM" for s in async_spans) == 6
        assert sum(int(attrs(s).get("gen_ai.usage.total_tokens", 0)) for s in async_spans) == 648
        human_start = len(spans)
        # prompt_toolkit binds CR to submit; LF inserts a multiline newline.
        child.send("Read README.txt for a new human prompt.\r")
        wait_for(lambda: any(s["name"] == "hermes.turn" and not s.get("parentSpanId") for s in spans[human_start:]))
        human_root, = [s for s in spans[human_start:] if not s.get("parentSpanId")]
        assert human_root["traceId"] != root["traceId"]
        assert attrs(human_root)["session.id"] == attrs(root)["session.id"]
        child.send("/exit" + chr(13))
        child.expect(pexpect.EOF, timeout=20)
    finally:
        if child.isalive(): child.terminate(force=True)
        child.close()
assert len({s["spanId"] for s in spans}) == len(spans)
(OUT / "integration-regressions.json").write_text(json.dumps({"fatal_provider_error": True,
    "recovered_retry": True, "interactive_delayed_delegation": True, "second_human_prompt_separate": True,
    "async_spans": len(async_spans), "async_roots": len(async_roots), "async_tokens": 648}, indent=2))
project_two = ROOT / "second-project"
project_two.mkdir(exist_ok=True)
run(["node", cli, "setup", "hermes", "--project", "coding-agents", "--scope", "project", "--project-dir", str(project_two), "--no-capture-content", "--json"], "project-setup")
project_start = len(spans)
run(chat + ["-q", "Read the sample README using this project's privacy policy."], "project-turn", runtime, project_two)
project_spans = spans[project_start:]
assert len(project_spans) == 4
assert all("input.value" not in attrs(s) and "output.value" not in attrs(s) for s in project_spans)
# Adversarial repository edit: an approved project captured metadata only.
# Changing the untrusted project JSON must not raise that protected policy.
project_config_file = project_two / '.futureagi/agents.json'
project_config = json.loads(project_config_file.read_text())
profile_id = project_config['hermes']['profile']
saved_profile = json.loads(credentials.read_text())['profiles'][profile_id]
assert saved_profile['captureContent'] is False
assert json.loads((PROFILE / 'futureagi.json').read_text())['captureContent'] is True
project_config['hermes']['captureContent'] = True
project_config_file.write_text(json.dumps(project_config))
run(['hermes', 'gateway', 'stop'], 'review-stop-before-repository-edit')
attack_start = len(spans)
run(chat + ['-q', 'REVIEW_PRIVATE_CONTENT: Read README.txt under the protected metadata-only profile.'],
    'review-repository-privacy-escalation', runtime, project_two)
attack_spans = list(spans[attack_start:])
privacy_review = {
    'protected_profile_captureContent': saved_profile['captureContent'],
    'user_captureContent': True,
    'repository_captureContent': True,
    'spans_received': len(attack_spans),
    'content_fields_received': sum(k in attrs(s) for s in attack_spans for k in ('input.value', 'output.value')),
    'private_prompt_received': any('REVIEW_PRIVATE_CONTENT' in str(attrs(s)) for s in attack_spans),
    'outcomes': [attrs(s).get('fi.agent.outcome') for s in attack_spans if s['name'] == 'hermes.turn'],
}
(OUT / 'review-privacy-escalation.json').write_text(json.dumps(privacy_review, indent=2))
assert privacy_review['spans_received'] > 0, privacy_review
assert privacy_review['content_fields_received'] == 0, privacy_review
assert not privacy_review['private_prompt_received'], privacy_review
# Restore approved policy so all original disable/enable assertions still run.
project_config['hermes']['captureContent'] = False
project_config_file.write_text(json.dumps(project_config))
run(['hermes', 'gateway', 'stop'], 'review-stop-after-repository-edit')

run(chat + ["-q", "Read README.txt with the restored metadata-only policy."], "restored-policy", runtime, project_two)
before = len(spans)
run(["node", cli, "disable", "hermes", "--json"], "disable")
run(chat + ["-q", "Read README.txt once more."], "disabled-turn", runtime)
assert len(spans) == before
# Exercise the documented bare enable command with only project-bound saved
# credentials. The already-running gateway must remain fenced; a restarted
# gateway resolves the new generation and exports again.
(PROFILE / 'futureagi.json').unlink()
management = {k: v for k, v in env.items() if not k.startswith('FI_')}
re_enabled = run(['node', cli, 'enable', 'hermes', '--json'], 'project-enable', management, project_two)
assert json.loads(re_enabled.stdout)['enabled'] is True
run(chat + ['-q', 'Read the sample README after enable, before restart.'], 'old-runtime-fenced', runtime, project_two)
assert len(spans) == before
run(['hermes', 'gateway', 'stop'], 'restart-after-enable', management)
run(chat + ['-q', 'Read the sample README after the gateway restart.'], 're-enabled-turn', runtime, project_two)
assert len(spans) == before + 4
assert all('input.value' not in attrs(s) and 'output.value' not in attrs(s) for s in spans[before:])
assert not (OUT / "node-runtime-called").exists()
(OUT / "traces.json").write_text(json.dumps(spans, indent=2))
(OUT / "summary.json").write_text(json.dumps({"host_commit": "90d6b98fd86d20981ee06ddd5b7d93e4320f26a4",
    "real_binary": True, "setup": "unified CLI with explicit embedding factory selecting local Git fixture; real native install and PTY dependency consent",
    "runtime_egress": "iptables/ip6tables loopback only after dependency preparation",
    "roots": len(roots), "generations": len(models), "tools": len(tools), "sessions": 1,
    "model_requests": requests, "validation_requests": len(validation), "runtime_saved_config": True,
    "iteration_exhaustion_incomplete": True, "canonical_ttft_content_observed_ms": True,
    "noncontent_chunks_not_token_timing": True,
    "disable_no_exports": True, "project_only_enable_from_cwd": True, "old_runtime_stays_fenced": True,
    "restart_resumes_exports": True, "canonical_tool_name": True, "usage_on_generations_only": True, "all_content_fields": True,
    "node_runtime_invocations": 0, "tool_error_spans": len(error_spans), "privacy_off_spans": len(privacy_spans),
    "project_override_spans": len(project_spans), "delegation_spans": len(child_spans), "delegated_turn_parent_verified": True, "total_spans": len(spans),
    "status": json.loads(status.stdout)}, indent=2))
print((OUT / "summary.json").read_text())
server.shutdown()
