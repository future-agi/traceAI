"""Pinned real Goose contract. Run only in E2B, after package/build prep, without external egress."""

import os, json, pathlib, threading, subprocess, time, sqlite3, hashlib, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = pathlib.Path("/work/live")
root.mkdir(exist_ok=True)
cli = "/work/consumer/node_modules/@traceai/agents/dist/cli.js"
requests = []
exports = []
unexpected = []
mode = "normal"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, typ="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.end_headers()
        self.wfile.write(data.encode())

    def do_GET(self):
        self.send(
            200,
            json.dumps(
                {
                    "data": [
                        {"id": "gpt-4o", "object": "model", "context_length": 128000}
                    ]
                }
            ),
        )

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/tracer/v1/traces":
            if self.headers.get("X-Api-Key") == "invalid":
                self.send(401, "{}")
                return
            if mode == "timeout":
                time.sleep(10)
                return
            body = json.loads(raw)
            if set(body) != {"resourceSpans"}:
                unexpected.append("non-trace-signal")
                self.send(400, "{}")
                return
            exports.append(body)
            self.send(200, "{}")
            return
        if self.path == "/v1/messages":
            body = json.loads(raw)
            requests.append({"path": self.path, "stream": body.get("stream", False)})
            assert body.get("stream"), "cache regression must exercise the real streaming provider"
            events = [
                {"type": "message_start", "message": {
                    "id": f"msg_synthetic_cache_{len(requests)}", "type": "message",
                    "role": "assistant", "model": body["model"], "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 100, "cache_read_input_tokens": 200,
                              "cache_creation_input_tokens": 50, "output_tokens": 0},
                }},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Synthetic cache response"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 10}},
                {"type": "message_stop"},
            ]
            self.send(200, "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events), "text/event-stream")
            return
        if self.path != "/v1/chat/completions":
            unexpected.append(self.path)
            self.send(404, "{}")
            return
        body = json.loads(raw)
        requests.append({"path": self.path, "stream": body.get("stream", False)})
        response_id = f"chatcmpl-synthetic-{len(requests)}"
        messages = body.get("messages", [])
        text = json.dumps(messages)
        if "PROVIDER_ERROR_SCENARIO" in text:
            self.send(
                400,
                '{"error":{"message":"synthetic provider error","type":"invalid_request_error"}}',
            )
            return
        shell = next(
            (
                t["function"]["name"]
                for t in body.get("tools", [])
                if t.get("function", {}).get("name", "").endswith("shell")
            ),
            None,
        )
        did_tool = any(m.get("role") == "tool" for m in messages)
        delta = {"role": "assistant", "content": "Synthetic response"}
        finish = "stop"
        if shell and not did_tool:
            command = (
                "exit 3"
                if "TOOL_ERROR_SCENARIO" in text
                else "printf c3ludGhldGljLXRvb2wtb3V0cHV0 | base64 -d"
            )
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {
                            "name": shell,
                            "arguments": json.dumps({"command": command}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if body.get("stream"):
            chunks = [
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": usage,
                },
            ]
            self.send(
                200,
                "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                + "data: [DONE]\n\n",
                "text/event-stream",
            )
        else:
            self.send(
                200,
                json.dumps(
                    {
                        "id": response_id,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "gpt-4o",
                        "choices": [
                            {"index": 0, "message": delta, "finish_reason": finish}
                        ],
                        "usage": usage,
                    }
                ),
            )


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_port
home = root / "home"
home.mkdir(exist_ok=True)
goose_root = home / "goose"
(goose_root / "config").mkdir(parents=True, exist_ok=True)
(goose_root / "config" / "config.yaml").write_text(
    "GOOSE_PROVIDER: openai\nGOOSE_MODEL: gpt-4o\nGOOSE_MODE: auto\nextensions:\n  developer:\n    enabled: true\n    type: builtin\n    name: developer\n    timeout: 30\n"
)
env = {
    **os.environ,
    "PATH": "/work/vendor:" + os.environ["PATH"],
    "HOME": str(home),
    "GOOSE_PATH_ROOT": str(goose_root),
    "GOOSE_PROVIDER": "openai",
    "GOOSE_MODEL": "gpt-4o",
    "OPENAI_API_KEY": "dummy",
    "OPENAI_HOST": f"http://127.0.0.1:{port}",
    "GOOSE_MODE": "auto",
    "GOOSE_TELEMETRY_ENABLED": "false",
    "FI_AGENT_CONFIG_DIR": str(home / "fi"),
    "XDG_STATE_HOME": str(home / "state"),
    "FI_API_KEY": "dummy-api",
    "FI_SECRET_KEY": "dummy-secret",
    "FI_OTLP_ENDPOINT": f"http://127.0.0.1:{port}/tracer/v1/traces",
}
for k in list(env):
    if k.startswith("OTEL_"):
        del env[k]


def run(args, overrides=None, ok=True, cwd=None):
    e = {**env, **(overrides or {})}
    start = time.monotonic()
    p = subprocess.run(
        args, env=e, cwd=cwd or root, text=True, capture_output=True, timeout=40
    )
    with (root / "commands.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {
                    "args": args,
                    "code": p.returncode,
                    "seconds": time.monotonic() - start,
                    "stdout": p.stdout,
                    "stderr": p.stderr,
                }
            )
            + "\n"
        )
    if ok:
        assert p.returncode == 0, (args, p.stdout[-3000:], p.stderr[-3000:])
    return p


def spans():
    return [
        s
        for b in exports
        for r in b.get("resourceSpans", [])
        for sc in r.get("scopeSpans", [])
        for s in sc.get("spans", [])
    ]


def attrs(s):
    return {a["key"]: next(iter(a["value"].values())) for a in s.get("attributes", [])}


def clear():
    exports.clear()


def command(*args, **kw):
    return run(["node", cli, *args], **kw)


def goose(prompt, extra=None, **kw):
    return run(["/work/vendor/goose", "run", *(extra or []), "-t", prompt], **kw)


def session_id():
    db = sqlite3.connect(goose_root / "data/sessions/sessions.db")
    sid = db.execute(
        "SELECT id FROM sessions ORDER BY created_at DESC,id DESC LIMIT 1"
    ).fetchone()[0]
    db.close()
    return sid


try:
    run(["/work/consumer/node_modules/.bin/fi-agent", "--help"])
    bad = command(
        "setup",
        "goose",
        "--project",
        "coding-agents",
        "--json",
        overrides={"FI_API_KEY": "invalid"},
        ok=False,
    )
    assert bad.returncode == 2 and not (home / "fi").exists(), bad.stdout
    p = command("setup", "goose", "--project", "coding-agents", "--json")
    assert json.loads(p.stdout)["registered"]
    plugin = goose_root / ".agents/plugins/futureagi-goose"
    before = (plugin / "hooks/hooks.json").read_bytes()
    command("setup", "goose", "--project", "coding-agents", "--json")
    assert before == (plugin / "hooks/hooks.json").read_bytes()
    for path in [
        home / "fi/credentials.json",
        goose_root / "config/futureagi.json",
        plugin / "plugin.json",
        plugin / "hooks/hooks.json",
    ]:
        assert path.stat().st_mode & 0o777 == 0o600, path
    # Persisted credentials, not the setup environment, power native Goose hooks.
    del env["FI_API_KEY"]
    del env["FI_SECRET_KEY"]
    del env["FI_OTLP_ENDPOINT"]
    clear()
    goose("SYNTHETIC please run the shell tool then answer")
    sid = session_id()
    goose("SYNTHETIC second request", ["--resume", "--session-id", sid])
    all_spans = spans()
    roots = [s for s in all_spans if not s.get("parentSpanId")]
    models = [s for s in all_spans if attrs(s).get("fi.span.kind") == "LLM"]
    tools = [s for s in all_spans if attrs(s).get("fi.span.kind") == "TOOL"]
    assert len(roots) == 2, (len(roots), all_spans)
    assert len(models) == 3 and len(tools) == 1, (len(models), len(tools))
    assert len({attrs(s)["session.id"] for s in roots}) == 1
    assert len({s["traceId"] for s in roots}) == 2
    assert (
        sum(int(attrs(s).get("gen_ai.usage.input_tokens", 0)) for s in all_spans) == 300
    )
    assert all("gen_ai.usage.input_tokens" not in attrs(s) for s in roots)
    assert tools[0]["parentSpanId"] in {s["spanId"] for s in models}
    assert any(
        "SYNTHETIC please" in str(attrs(s).get("input.value", "")) for s in roots
    )
    raw = json.dumps(exports)
    for marker in [
        "user.name",
        "host.name",
        "session.user",
        "session.host",
        "dummy-api",
        "dummy-secret",
        "/work/live",
    ]:
        assert marker not in raw, marker
    assert all(not s.get("events") for s in all_spans)
    (root / "two-turn-otlp.json").write_text(json.dumps(exports, indent=2))
    status = json.loads(command("status", "goose", "--json").stdout)
    assert (
        status["delivery"]["exported"] == 6 and status["activation"] == "OBSERVED"
    ), status
    # Goose normalizes Anthropic input usage before writing SQLite: cache is a
    # breakdown, not an additional input charge. Verify the real provider path.
    clear()
    goose("CACHE_ACCOUNTING_REGRESSION answer briefly", overrides={
        "GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": "claude-opus-5-5",
        "ANTHROPIC_API_KEY": "synthetic-anthropic-key",
        "ANTHROPIC_HOST": f"http://127.0.0.1:{port}",
    })
    cached_sid = session_id()
    db = sqlite3.connect(goose_root / "data/sessions/sessions.db")
    saved = [json.loads(r[0]) for r in db.execute(
        "SELECT metadata_json FROM messages WHERE session_id=? AND role='assistant'", (cached_sid,)
    ) if json.loads(r[0]).get("inference")]
    db.close()
    cached_models = [s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]
    assert saved and len(cached_models) == len(saved), (saved, cached_models)
    assert all(m["inference"]["provider"] == "anthropic" for m in saved), saved
    assert all(m["usage"]["inputTokens"] == 350 for m in saved), saved
    assert sum(int(attrs(s).get("gen_ai.usage.input_tokens", 0)) for s in spans()) == 350 * len(saved)
    assert sum(int(attrs(s).get("gen_ai.usage.output_tokens", 0)) for s in spans()) == 10 * len(saved)
    for model in cached_models:
        values = attrs(model)
        assert values["gen_ai.usage.cache_read_tokens"] == 200, values
        assert values["gen_ai.usage.cache_write_tokens"] == 50, values
        assert values["gen_ai.usage.total_tokens"] == 360, values
    (root / "anthropic-cache-otlp.json").write_text(json.dumps(exports, indent=2))
    (root / "anthropic-cache-saved.json").write_text(json.dumps(saved, indent=2))
    # A legacy claim abandoned by an actual killed writer must not wedge the
    # installed hook runtime when real Goose resumes the same conversation.
    state_file = goose_root / "config/futureagi-sessions" / (hashlib.sha256(cached_sid.encode()).hexdigest() + ".json")
    legacy_claim = pathlib.Path(str(state_file) + ".lock")
    writer = subprocess.Popen(["node", "-e", "const fs=require('node:fs'); const fd=fs.openSync(process.argv[1], 'wx', 0o600); process.stdout.write('acquired\\n'); setInterval(()=>{}, 1000)", str(legacy_claim)], stdout=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([writer.stdout], [], [], 10)[0], "legacy writer never acquired claim"
        assert writer.stdout.readline().strip() == "acquired"
    finally:
        writer.kill()
        writer.wait(timeout=5)
        writer.stdout.close()
    for i in range(2):
        clear()
        goose(f"RESUME_AFTER_KILLED_WRITER_{i}", ["--resume", "--session-id", cached_sid])
        assert len([s for s in spans() if not s.get("parentSpanId")]) == 1
        assert [s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]
        assert legacy_claim.exists(), "legacy claims are inert, not removed"
    # Identical prompts must not rewind into the previous completed turn.
    for loop in ({}, {"GOOSE_STATE_MACHINE": "0"}):
        clear()
        goose("SAME_PROMPT_REPEAT", overrides=loop)
        repeated = session_id()
        assert len([s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]) == 2
        clear()
        goose("SAME_PROMPT_REPEAT", ["--resume", "--session-id", repeated], overrides=loop)
        assert len([s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]) == 1
        assert not [s for s in spans() if attrs(s).get("fi.span.kind") == "TOOL"]
        assert sum(int(attrs(s).get("gen_ai.usage.input_tokens", 0)) for s in spans()) == 100
        (root / ("repeat-default.json" if not loop else "repeat-legacy.json")).write_text(json.dumps(exports, indent=2))
    # Stop is only an attempt; a later policy can ask Goose to continue this turn.
    hookfile = plugin / "hooks/hooks.json"
    original_hooks = hookfile.read_text()
    policy = root / "block-once.py"
    policy.write_text('import pathlib,json\np=pathlib.Path("/work/live/blocked")\nif not p.exists():\n p.touch()\n print(json.dumps({"decision":"block","reason":"Please verify once more."}))\n')
    hooks = json.loads(original_hooks)
    hooks["hooks"]["Stop"][0]["hooks"].append({"type":"command","command":f"python3 -I {policy}","timeout":3})
    hookfile.write_text(json.dumps(hooks))
    clear()
    goose("STOP_BLOCK_SCENARIO")
    assert (root / "blocked").exists()
    assert len([s for s in spans() if not s.get("parentSpanId")]) == 1
    assert len([s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]) == 3
    assert sum(int(attrs(s).get("gen_ai.usage.input_tokens", 0)) for s in spans()) == 300
    (root / "stop-block.json").write_text(json.dumps(exports, indent=2))
    hookfile.write_text(original_hooks)
    # Native automatic compaction replaces SQLite rows, not logical message identities.
    clear()
    goose("FIRST_COMPACTION_PROMPT")
    compacted = session_id()
    db = sqlite3.connect(goose_root / "data/sessions/sessions.db")
    before_ids = dict(db.execute("SELECT message_id,id FROM messages WHERE session_id=?", (compacted,)))
    db.close()
    clear()
    goose("SECOND_COMPACTION_PROMPT", ["--resume","--session-id",compacted], overrides={"GOOSE_AUTO_COMPACT_THRESHOLD":"0.00001"})
    db = sqlite3.connect(goose_root / "data/sessions/sessions.db")
    new_models = [json.loads(r[1]) for r in db.execute("SELECT message_id,metadata_json FROM messages WHERE session_id=?", (compacted,)) if r[0] not in before_ids and json.loads(r[1]).get("inference")]
    after_ids = dict(db.execute("SELECT message_id,id FROM messages WHERE session_id=?", (compacted,)))
    db.close()
    assert any(after_ids[k] != v for k,v in before_ids.items() if k in after_ids), "compaction must actually replace rows"
    compact_roots = [s for s in spans() if not s.get("parentSpanId")]
    assert len(compact_roots) == 1 and attrs(compact_roots[0]).get("input.value") == "SECOND_COMPACTION_PROMPT", compact_roots
    assert len([s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]) == len(new_models), new_models
    assert sum(int(attrs(s).get("gen_ai.usage.input_tokens", 0)) for s in spans()) == sum(m["usage"]["inputTokens"] for m in new_models)
    (root / "compaction.json").write_text(json.dumps(exports, indent=2))
    # Duplicate terminal hooks do not duplicate delivery.
    clear()
    registration = json.loads((home / "fi/goose-plugin.json").read_text())
    registration_file = home / "fi/goose-plugin.json"
    original_registration = registration_file.read_text()
    registration_file.write_text(json.dumps({**registration, "node": "/missing-node"}))
    unavailable = json.loads(command("status", "goose", "--json", ok=False).stdout)
    assert unavailable["registration"] == "NOT_REGISTERED"
    assert unavailable["registrationDetail"] == "RUNTIME_UNAVAILABLE"
    registration_file.write_text(original_registration)
    duplicate = subprocess.run(
        ["node", registration["runtime"], str(home / "fi")],
        input=json.dumps(
            {"event": "Stop", "session_id": sid, "working_dir": str(root)}
        ),
        text=True,
        capture_output=True,
        env=env,
        timeout=8,
    )
    assert duplicate.returncode == 0 and not duplicate.stdout and not spans()
    clear()
    env["FI_AGENT_CAPTURE_CONTENT"] = "false"
    goose("PRIVATE_MARKER content must be absent")
    assert (
        spans()
        and "PRIVATE_MARKER" not in json.dumps(exports)
        and "Synthetic response" not in json.dumps(exports)
    )
    del env["FI_AGENT_CAPTURE_CONTENT"]
    for switch in ("FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES"):
        clear()
        goose("OUTPUT_PRIVACY_SCENARIO use shell", overrides={switch:"true"})
        assert spans() and "synthetic-tool-output" not in json.dumps(exports), switch
    clear()
    goose("TOOL_ERROR_SCENARIO run shell and handle error")
    errors = [s for s in spans() if attrs(s).get("fi.span.kind") == "TOOL"]
    assert errors and errors[0]["status"]["code"] == 2, errors
    assert [s for s in spans() if not s.get("parentSpanId")][0]["status"]["code"] == 2
    (root / "tool-error-otlp.json").write_text(json.dumps(exports, indent=2))
    clear()
    goose("PROVIDER_ERROR_SCENARIO")
    (root / "provider-error-otlp.json").write_text(json.dumps(exports, indent=2))
    failure_roots = [s for s in spans() if not s.get("parentSpanId")]
    assert (
        len(failure_roots) == 1
        and attrs(failure_roots[0])["fi.agent.outcome"] == "incomplete"
    )
    assert not [s for s in spans() if attrs(s).get("fi.span.kind") == "LLM"]
    clear()
    command("disable", "goose", "--json")
    goose("DISABLED_MARKER")
    assert not spans()
    command("enable", "goose", "--json")
    clear()
    env["FI_AGENT_ENABLED"] = "false"
    goose("ENV_DISABLED_MARKER")
    assert not spans()
    del env["FI_AGENT_ENABLED"]
    clear()
    mode = "timeout"
    start = time.monotonic()
    goose("TIMEOUT_MARKER")
    elapsed = time.monotonic() - start
    assert elapsed < 16, elapsed
    mode = "normal"
    # A native destination is never rewritten or allowed to duplicate this plugin's exports.
    clear()
    env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
        f"http://127.0.0.1:{port}/native-blocked"
    )
    env["OTEL_SDK_DISABLED"] = "true"
    # SDK_DISABLED neutralizes native export, so the safe hook path still works.
    goose("SAFE_WITH_NATIVE_DISABLED")
    assert spans()
    del env["OTEL_SDK_DISABLED"]
    del env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    # Native destination conflicts are visible in status and never silently overwritten.
    conflict = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}/native"}
    assert (
        command("update", "goose", "--json", overrides=conflict, ok=False).returncode
        == 2
    )
    inspection = json.loads(
        command("status", "goose", "--json", overrides=conflict, ok=False).stdout
    )
    assert inspection["registrationDetail"] == "NATIVE_TRACING_CONFLICT", inspection
    # Project setup needs explicit credentials/approval; runtime uses the bound saved profile.
    project = root / "project"
    project.mkdir()
    command(
        "setup",
        "goose",
        "--scope",
        "project",
        "--project-dir",
        str(project),
        "--project",
        "other-project",
        "--json",
        overrides={"FI_API_KEY": "dummy-api", "FI_SECRET_KEY": "dummy-secret"},
    )
    clear()
    goose("PROJECT_MARKER", cwd=project)
    project_names = {
        attrs(r["resource"])["project_name"]
        for b in exports
        for r in b["resourceSpans"]
    }
    assert project_names == {"other-project"}, project_names
    local_status = json.loads(command("status", "goose", "--check", "--json", cwd=project).stdout)
    assert local_status["projectName"] == "other-project" and local_status["privacy"] == "content-on-by-default", local_status
    # Real Goose resume sends the saved project on prompt/Stop but no cwd on SessionEnd.
    project_sid = session_id()
    clear()
    goose("CROSS_DIRECTORY_RESUME", ["--resume", "--session-id", project_sid], cwd=root)
    assert spans(), "resumed turn must not be discarded at SessionEnd"
    assert {attrs(r["resource"])["project_name"] for b in exports for r in b["resourceSpans"]} == {"other-project"}
    (root / "cross-directory-resume.json").write_text(json.dumps(exports, indent=2))
    # Privacy-only setup must not replace a local destination with the user default.
    command("setup", "goose", "--scope", "project", "--no-capture-content", "--json", cwd=project)
    privacy = json.loads(command("status", "goose", "--json", cwd=project).stdout)
    assert privacy["projectName"] == "other-project" and privacy["privacy"] == "structure-only", privacy
    command("setup", "goose", "--scope", "project", "--capture-content", "--json", cwd=project)
    settings = project / ".futureagi/agents.json"
    original = settings.read_text()
    tampered = json.loads(original)
    tampered["goose"]["transport"] = {"endpoint": f"http://127.0.0.1:{port}/unapproved"}
    settings.write_text(json.dumps(tampered))
    clear()
    goose("UNAPPROVED_DESTINATION", cwd=project, overrides={"FI_API_KEY":"inherited-api", "FI_SECRET_KEY":"inherited-secret"})
    assert not spans()
    settings.write_text(original)
    # A group-writable external volume does not silence a user-scope installation.
    project.chmod(0o775)
    clear()
    goose("EXTERNAL_VOLUME_MARKER", cwd=project)
    assert spans()
    assert {attrs(r["resource"])["project_name"] for b in exports for r in b["resourceSpans"]} == {"coding-agents"}
    unsafe = json.loads(command("doctor", "goose", "--json", cwd=project, ok=False).stdout)
    assert "PROJECT_CONFIG_IGNORED" in unsafe["issues"], unsafe
    project.chmod(0o755)
    # Ownership-aware uninstall refuses to remove a user's modified hook file.
    owned = plugin / "hooks/hooks.json"
    original_hooks = owned.read_text()
    owned.write_text(original_hooks + " ")
    assert (
        command("uninstall", "goose", "--json", ok=False).returncode == 2
        and owned.exists()
    )
    owned.write_text(original_hooks)
    command("uninstall", "goose", "--json")
    clear()
    goose("UNINSTALLED_MARKER")
    assert not spans()
    assert not (plugin / "hooks/hooks.json").exists()
    # Project-only install with another agent's shared default profile is not global consent.
    scoped_home = root / "scoped-home"
    scoped_goose = scoped_home / "goose"
    (scoped_goose / "config").mkdir(parents=True)
    (scoped_goose / "config/config.yaml").write_text((goose_root / "config/config.yaml").read_text())
    env.update({"HOME":str(scoped_home), "GOOSE_PATH_ROOT":str(scoped_goose), "FI_AGENT_CONFIG_DIR":str(scoped_home / "fi"), "XDG_STATE_HOME":str(scoped_home / "state")})
    credentials = {"FI_API_KEY":"dummy-api", "FI_SECRET_KEY":"dummy-secret", "FI_OTLP_ENDPOINT":f"http://127.0.0.1:{port}/tracer/v1/traces"}
    command("setup", "pi", "--project", "pi-project", "--configure-only", "--json", overrides=credentials)
    scoped_project = root / "scoped-project"
    scoped_project.mkdir()
    command("setup", "goose", "--scope", "project", "--project", "goose-project", "--json", cwd=scoped_project, overrides=credentials)
    inspected = json.loads(command("status", "goose", "--check", "--json", cwd=scoped_project, ok=False).stdout)
    assert inspected["enabled"] and inspected["projectName"] == "goose-project", inspected
    assert inspected["issues"] == ["ACTIVATION_NOT_OBSERVED"], inspected
    command("disable", "goose", "--json", cwd=scoped_project)
    command("enable", "goose", "--json", cwd=scoped_project)
    doctor = json.loads(command("doctor", "goose", "--json", cwd=scoped_project, ok=False).stdout)
    assert doctor["credentialCheck"] == "ACCEPTED" and doctor["issues"] == ["ACTIVATION_NOT_OBSERVED"], doctor
    command("update", "goose", "--json", cwd=scoped_project)
    clear()
    goose("INSIDE_PROJECT_SENTINEL", cwd=scoped_project)
    assert "INSIDE_PROJECT_SENTINEL" in json.dumps(exports)
    assert {attrs(r["resource"])["project_name"] for b in exports for r in b["resourceSpans"]} == {"goose-project"}
    clear()
    goose("OUTSIDE_PROJECT_SENTINEL", cwd=root)
    assert not spans(), exports
    outside = json.loads(command("status", "goose", "--json", ok=False, cwd=root).stdout)
    assert not outside["enabled"] and outside["registrationDetail"] == "PROJECT_NOT_REGISTERED", outside
    assert command("update", "goose", "--json", ok=False, cwd=root).returncode == 2
    command("setup", "goose", "--scope", "project", "--project", "goose-project", "--no-capture-content", "--json", cwd=scoped_project, overrides=credentials)
    local_status = json.loads(command("status", "goose", "--check", "--json", cwd=scoped_project, ok=False).stdout)
    assert local_status["privacy"] == "structure-only" and local_status["projectName"] == "goose-project"
    # Reconfiguration advances the capture epoch; an older accepted export is
    # not proof of activation for the newly selected privacy policy.
    assert local_status["issues"] == ["ACTIVATION_NOT_OBSERVED"], local_status
    command("disable", "goose", "--json", cwd=scoped_project)
    command("enable", "goose", "--json", cwd=scoped_project)
    clear()
    goose("PRIVATE_PROJECT_SENTINEL", cwd=scoped_project)
    assert spans() and "PRIVATE_PROJECT_SENTINEL" not in json.dumps(exports)
    # Even a copied project file plus explicit environment credentials cannot escape install scope.
    copied = root / "copied-project"
    (copied / ".futureagi").mkdir(parents=True)
    (copied / ".futureagi/agents.json").write_text((scoped_project / ".futureagi/agents.json").read_text())
    clear()
    goose("COPIED_PROJECT_SENTINEL", cwd=copied, overrides=credentials)
    assert not spans(), exports
    assert not unexpected, unexpected
    result = {
        "status": "PASS",
        "goose_version": "1.54.0",
        "transport": "native hooks + read-only saved provider records",
        "two_turn_spans": len(all_spans),
        "turns": len(roots),
        "model_generations": len(models),
        "tools": len(tools),
        "additive_input_tokens": 300,
        "model_request_paths": sorted(set(r["path"] for r in requests)),
        "timeout_run_seconds": elapsed,
        "assertions": [
            "fresh invalid/valid setup",
            "owner-only files",
            "idempotent registration",
            "saved credentials",
            "two turns one session",
            "no duplicate terminal export",
            "tool parent model",
            "source usage once",
            "real Anthropic cached input matches persisted usage",
            "real resume survives a killed legacy state writer",
            "privacy content off",
            "no host/OS identity or log events",
            "tool error status",
            "provider failure turn",
            "disable and env kill switch",
            "bounded export timeout",
            "native export disabled compatibility",
            "native conflict status",
            "bound per-project profile",
            "unapproved destination blocked",
            "ownership-aware uninstall",
            "repeated prompts default and legacy loop",
            "automatic compaction stable identities and correct prompt",
            "blocking Stop continuation retains all usage",
            "project-only scope with unrelated agent profile",
            "cwd setup status doctor disable enable update parity",
            "copied project cannot broaden registration",
            "cross-directory real session resume preserves project",
            "privacy-only project reconfiguration preserves destination",
            "inherited credentials cannot approve repository redirect",
            "all output privacy switches apply across generation inputs",
            "unsafe external-volume layer falls back visibly",
            "missing interpreter is actionable in status",
        ],
        "external_egress": "network namespace: loopback only",
    }
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
finally:
    server.shutdown()
