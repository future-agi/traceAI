"""A stand-in for the Claude Code CLI that speaks the SDK's stream-json control protocol.

The real ``claude_agent_sdk`` spawns this through ``ClaudeAgentOptions(cli_path=...)``.
For every user message it replays one tool call: the assistant ``tool_use``, the
PreToolUse and PostToolUse hook callbacks the SDK registered, the ``tool_result``
carrying the media in ``FAKE_CLAUDE_RESULT`` (a JSON file), then a final answer.
``FAKE_CLAUDE_ORDER=result-first`` streams the tool result before PostToolUse;
``no-result`` never streams it. ``FAKE_CLAUDE_GAP`` (seconds) waits between
PostToolUse and the tool result; ``FAKE_CLAUDE_HOLD`` (a path) also waits there,
until that file exists (at most 20 s). ``FAKE_TOOL_ID`` names the tool call. No
model, network or file other than the result and hold files is touched.
"""

import json
import os
import sys
import time

if "-v" in sys.argv[1:] or "--version" in sys.argv[1:]:
    print("2.1.999 (Claude Code)")
    sys.exit(0)

with open(os.environ["FAKE_CLAUDE_RESULT"]) as handle:
    TOOL_RESULT = json.load(handle)
ORDER = os.environ.get("FAKE_CLAUDE_ORDER", "hooks-first")
GAP = float(os.environ.get("FAKE_CLAUDE_GAP", "0"))
HOLD = os.environ.get("FAKE_CLAUDE_HOLD", "")
TOOL_ID = os.environ.get("FAKE_TOOL_ID", "toolu_screen_1")
TOOL = "mcp__screen__capture"
hooks = {}
counter = [0]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def wait_response(request_id):
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") == "control_response":
            if message.get("response", {}).get("request_id") == request_id:
                return message
        else:
            handle(message)


def hook(event, tool_use_id, extra):
    for callback_id in hooks.get(event, []):
        counter[0] += 1
        request_id = "cli_%d" % counter[0]
        send({"type": "control_request", "request_id": request_id, "request": {
            "subtype": "hook_callback", "callback_id": callback_id, "tool_use_id": tool_use_id,
            "input": dict({"hook_event_name": event, "session_id": "s1", "tool_name": TOOL, "tool_input": {},
                           "cwd": "/", "transcript_path": ""}, **extra)}})
        wait_response(request_id)


def assistant(content):
    send({"type": "assistant", "parent_tool_use_id": None, "session_id": "s1", "message": {
        "id": "msg_%d" % counter[0], "type": "message", "role": "assistant", "model": "claude-test",
        "content": content, "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})


def turn():
    tool_use_id = TOOL_ID
    time.sleep(0.05)  # Model latency: the client is iterating receive_response() before any hook.
    assistant([{"type": "tool_use", "id": tool_use_id, "name": TOOL, "input": {}}])
    hook("PreToolUse", tool_use_id, {})
    result = {"type": "user", "parent_tool_use_id": None, "session_id": "s1", "message": {
        "role": "user", "content": [dict(TOOL_RESULT, type="tool_result", tool_use_id=tool_use_id)]}}
    if ORDER == "result-first":
        send(result)
        hook("PostToolUse", tool_use_id, {"tool_response": TOOL_RESULT.get("content")})
    else:
        hook("PostToolUse", tool_use_id, {"tool_response": TOOL_RESULT.get("content")})
        time.sleep(GAP)
        deadline = time.monotonic() + 20
        while HOLD and not os.path.exists(HOLD) and time.monotonic() < deadline:
            time.sleep(0.02)
        if ORDER != "no-result":
            send(result)
    assistant([{"type": "text", "text": "done"}])
    send({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": False,
          "num_turns": 1, "session_id": "s1", "total_cost_usd": 0.0, "result": "done",
          "usage": {"input_tokens": 1, "output_tokens": 1}})


def handle(message):
    kind = message.get("type")
    if kind == "control_request":
        request = message.get("request", {})
        if request.get("subtype") == "initialize":
            for event, matchers in (request.get("hooks") or {}).items():
                hooks[event] = [cid for matcher in matchers for cid in matcher.get("hookCallbackIds", [])]
        send({"type": "control_response", "response": {
            "subtype": "success", "request_id": message.get("request_id"),
            "response": {"commands": [], "models": [], "account": {}} if request.get("subtype") == "initialize"
            else {}}})
    elif kind == "user":
        turn()


send({"type": "system", "subtype": "init", "session_id": "s1", "tools": [], "mcp_servers": [], "model": "claude-test",
      "cwd": "/", "permissionMode": "default", "apiKeySource": "none"})
for line in sys.stdin:
    line = line.strip()
    if line:
        handle(json.loads(line))
