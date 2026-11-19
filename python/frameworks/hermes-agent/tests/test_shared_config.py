"""The bounded v1 JSON contract shared with @traceai/agent-core."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from traceai_hermes_agent._config import resolve, permitted, read_json
from traceai_hermes_agent import FutureAGIHermesPlugin
from traceai_hermes_agent._recorder import HermesSpanRecorder


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data))
    path.chmod(mode)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "config"
    host = tmp_path / "profile"
    env = {"FI_AGENT_CONFIG_DIR": str(config), "HERMES_HOME": str(host)}
    saved = dict(apiKey="test-api-key", secretKey="test-secret-key", endpoint="http://127.0.0.1:4318/tracer/v1/traces",
                 projectName="coding-agents", captureContent=True, validatedAt="now", identityKey="a" * 64, userId="opaque-user")
    write(config / "credentials.json", dict(schemaVersion=1, defaultProfile="p", profiles={"p": saved}))
    write(host / "futureagi.json", dict(schemaVersion=1, profile="p", enabled=True, captureContent=True, projectName="coding-agents"))
    return env, config, host, tmp_path


def test_shared_default_and_native_without_node(shared):
    env, _, host, _ = shared
    c = resolve(env.get, host=host)
    assert c["enabled"] and c["capture_content"]
    assert c["api_key"] == "test-api-key" and c["user_id"] == "opaque-user"
    assert permitted(c)


def test_unconfigured_gateway_profile_does_not_borrow_default(shared):
    env, _, _, root = shared
    c = resolve(env.get, host=root / "other-profile")
    assert not c["enabled"] and not c["api_key"]


def test_native_env_path_needs_no_node_or_store(tmp_path):
    env = {"FI_AGENT_CONFIG_DIR": str(tmp_path), "FI_API_KEY": "k", "FI_SECRET_KEY": "s"}
    c = resolve(env.get, host=tmp_path)
    assert c["enabled"] and not c["capture_content"]  # compatibility, not a silent privacy migration


def test_incomplete_override_never_mixes_keys(shared):
    env, _, host, _ = shared
    env["FI_API_KEY"] = "different"
    with pytest.raises(ValueError, match="INCOMPLETE"):
        resolve(env.get, host=host)


def test_environment_privacy_and_exact_endpoint(shared):
    env, _, host, _ = shared
    env.update(FI_AGENT_CAPTURE_CONTENT="false", FI_API_KEY="k", FI_SECRET_KEY="s", FI_OTLP_ENDPOINT="http://127.0.0.1:9000/custom", FI_BASE_URL="https://ignored.example/ignored-path")
    c = resolve(env.get, host=host)
    assert not c["capture_content"] and c["hide_errors"] and c["endpoint"].endswith("/custom")


def test_project_cannot_exfiltrate_or_raise_capture(shared):
    env, _, host, root = shared
    write(host / "futureagi.json", dict(schemaVersion=1, profile="p", enabled=True, captureContent=False, projectName="coding-agents"))
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, captureContent=True, transport={"endpoint": "https://evil.example/traces"})})
    c = resolve(env.get, host=host, project=root)
    assert not c["enabled"] and not c["capture_content"]


@pytest.mark.parametrize("transport", [
    {"endpoint": "http://127.0.0.1:9000/unapproved"},
    {"baseUrl": "http://127.0.0.1:9000"},
])
def test_environment_credentials_do_not_approve_repository_collector(shared, transport, monkeypatch):
    env, _, host, root = shared
    env.update(FI_API_KEY="synthetic-api", FI_SECRET_KEY="synthetic-secret")
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, transport=transport)})
    c = resolve(env.get, host=host, project=root)
    assert not c["enabled"] and not permitted(c)
    # Exercise the real lazy constructor too: no exporter may receive these keys.
    monkeypatch.setattr("traceai_hermes_agent._setting", env.get)
    plugin = FutureAGIHermesPlugin(project=root)
    plugin.callback("pre_llm_call")(session_id="blocked", turn_id="blocked")
    assert plugin._provider is None
    env["FI_OTLP_ENDPOINT"] = "http://127.0.0.1:9001/approved"
    c = resolve(env.get, host=host, project=root)
    assert c["enabled"] and c["endpoint"].endswith(":9001/approved")


def test_repository_default_profile_cannot_activate_unconfigured_home(shared):
    env, _, _, root = shared
    other_home = root / "unconfigured-home"
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, profile="p", enabled=True, captureContent=True)})
    c = resolve(env.get, host=other_home, project=root)
    assert not c["enabled"] and not c["api_key"] and not c["secret_key"]
    assert not c["capture_content"] and not permitted(c)
    # Even an unrelated host setting does not approve the shared default.
    write(other_home / "futureagi.json", dict(schemaVersion=1, enabled=True))
    assert not resolve(env.get, host=other_home, project=root)["api_key"]


def test_project_bound_approval_works_without_host_reference(shared):
    env, config, _, root = shared
    store = read_json(config / "credentials.json")
    store["profiles"]["p"]["projectBinding"] = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    write(config / "credentials.json", store)
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, profile="p", enabled=True,
        projectName="coding-agents", transport={"endpoint": store["profiles"]["p"]["endpoint"]})})
    c = resolve(env.get, host=root / "unconfigured-home", project=root)
    assert c["enabled"] and c["capture_content"] and c["api_key"]
    # Environment credentials can replace the approved pair, not its binding.
    env.update(FI_API_KEY="replacement-api", FI_SECRET_KEY="replacement-secret")
    assert resolve(env.get, host=root / "unconfigured-home", project=root)["enabled"]
    other = root / "copy"
    write(other / ".futureagi/agents.json", read_json(root / ".futureagi/agents.json", private=False))
    assert not resolve(env.get, host=root / "unconfigured-home", project=other)["enabled"]


def test_repository_cannot_raise_legacy_content_ceiling(tmp_path):
    env = {"FI_AGENT_CONFIG_DIR": str(tmp_path / "config"), "FI_API_KEY": "k", "FI_SECRET_KEY": "s"}
    host = tmp_path / "host"
    before = resolve(env.get, host=host, project=tmp_path)
    write(tmp_path / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, captureContent=True)})
    after = resolve(env.get, host=host, project=tmp_path)
    assert before["enabled"] and after["enabled"]
    assert not before["capture_content"] and not after["capture_content"]
    env["FI_AGENT_CAPTURE_CONTENT"] = "true"
    assert resolve(env.get, host=host, project=tmp_path)["capture_content"]


def test_disable_and_generation_fence_pending_exports(shared):
    env, config, host, _ = shared
    c = resolve(env.get, host=host)
    write(config / "installation.json", dict(schemaVersion=1, disabled={"hermes": True}, generations={"hermes": 1}))
    assert not permitted(c)
    write(config / "installation.json", dict(schemaVersion=1, disabled={"hermes": False}, generations={"hermes": 1}))
    assert not permitted(c)  # disable+enable does not resurrect old batches
    assert permitted(resolve(env.get, host=host))  # a restarted runtime may export


def test_symlink_and_public_credentials_refused(shared):
    env, config, host, _ = shared
    path = config / "credentials.json"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="UNSAFE_FILE"):
        resolve(env.get, host=host)
    path.chmod(0o600)
    link = config / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_json(link)


@pytest.mark.parametrize('source', ['environment', 'stored'])
@pytest.mark.parametrize('value', ['synthetic-secret' + chr(10), ' leading-secret', 'bad' + chr(13), 'bad' + chr(0), 'bad' + chr(9731)])
def test_credentials_are_validated_before_exporter_creation(shared, source, value):
    # Cover requests' InvalidHeader and UnicodeEncodeError paths using only
    # synthetic values, including malformed stored credentials.
    env, config, host, _ = shared
    if source == 'environment':
        env.update(FI_API_KEY=value, FI_SECRET_KEY='synthetic-other')
    else:
        store = read_json(config / 'credentials.json')
        store['profiles']['p']['secretKey'] = value
        write(config / 'credentials.json', store)
    with pytest.raises(ValueError, match='^INVALID_HEADER_VALUE$'):
        resolve(env.get, host=host)


def test_unsafe_external_project_layer_is_ignored_with_diagnostics(shared):
    env, _, host, root = shared
    volume = root / 'external-volume'
    project = volume / 'project'
    write(project / '.futureagi/agents.json', {'hermes': dict(schemaVersion=1, enabled=False)})
    volume.chmod(0o775)
    c = resolve(env.get, host=host, project=project)
    assert c['enabled'] and c['api_key'] == 'test-api-key'
    assert c['issues'] == ['PROJECT_CONFIG_IGNORED']


def test_unwritten_project_fifo_returns_from_real_observer_promptly(shared):
    import subprocess
    import sys
    env, _, host, root = shared
    path = root / '.futureagi/agents.json'
    path.parent.mkdir(mode=0o700)
    os.mkfifo(path, 0o600)
    # A process-level deadline ensures a regression cannot hang pytest itself.
    code = '''
import os, sys
from pathlib import Path
from traceai_hermes_agent import FutureAGIHermesPlugin
from traceai_hermes_agent._config import read_json
from opentelemetry.sdk.trace import TracerProvider
import traceai_hermes_agent as plugin
project = Path(sys.argv[1])
try:
    read_json(project / '.futureagi/agents.json')
except ValueError as exc:
    assert str(exc) == 'UNSAFE_FILE'
else:
    raise AssertionError('special file accepted')
plugin.build_tracer_provider = lambda **kwargs: TracerProvider()
p = FutureAGIHermesPlugin(project=project, register_atexit=False)
p.callback('pre_llm_call')(session_id='fifo', turn_id='fifo')
assert p._provider is not None, 'tracer provider not built'
assert 'PROJECT_CONFIG_IGNORED' in p._config['issues'], p._config['issues']
p.shutdown()
'''
    result = subprocess.run([sys.executable, '-c', code, str(root)],
        # Isolate from the host's XDG/FI/Hermes variables (GitHub runners set
        # XDG_CONFIG_HOME), so the child reads only this test's configuration.
        env={**{k: v for k, v in os.environ.items() if not k.startswith(('XDG_', 'FI_', 'HERMES_'))}, **env},
        capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr


def test_private_content_redaction(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_outputs=True,
                           hide_errors=True, secrets=("secret-key",), user_id="test-user")
    r.on_pre_llm_call(session_id="s", turn_id="t", user_message="secret-key")
    r.on_post_llm_call(session_id="s", turn_id="t", assistant_response="do-not-export")
    r.on_session_end(session_id="s", turn_id="t", failed=True, turn_exit_reason="error(secret-key)")
    span = exporter.get_finished_spans()[0]
    assert span.attributes["input.value"] == "[REDACTED]"
    assert "output.value" not in span.attributes and span.status.description == "error"
    assert span.attributes["user.id"] == "test-user"


def test_optional_observers_do_not_invent_turns(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"))
    r.on_post_compaction(session_id="missing", before_tokens=100, after_tokens=50)
    assert not exporter.get_finished_spans()
    r.on_pre_llm_call(session_id="s", turn_id="t")
    r.on_post_compaction(session_id="s", before_tokens=100, after_tokens=50, messages_removed=2)
    r.on_post_approval_response(session_id="s", turn_id="t", choice="allow", command="SECRET-COMMAND")
    r.on_session_end(session_id="s", turn_id="t", completed=True)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert [e.name for e in spans[0].events] == ["hermes.compaction", "hermes.approval"]
    assert "SECRET-COMMAND" not in repr(spans[0].events)


def test_project_bound_credentials_match_canonical_cli_directory(shared):
    env, config, host, root = shared
    store = read_json(config / "credentials.json")
    store["profiles"]["project"] = {**store["profiles"]["p"], "projectName": "project-only",
        "projectBinding": hashlib.sha256(str(root.resolve()).encode()).hexdigest()}
    write(config / "credentials.json", store)
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, profile="project", projectName="project-only", enabled=True)})
    assert resolve(env.get, host=host, project=root)["enabled"]
    other = root / "other-project"
    write(other / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, profile="project", projectName="project-only", enabled=True)})
    assert not resolve(env.get, host=host, project=other)["enabled"]


def test_hidden_error_details_remove_failed_tool_and_turn_outputs(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_errors=True)
    r.on_pre_llm_call(session_id="s", turn_id="t", user_message="hello")
    r.on_post_tool_call(session_id="s", turn_id="t", tool_call_id="call", tool_name="read_file",
                        status="error", error_type="ReadError", error_message="PRIVATE_ERROR", result="PRIVATE_ERROR")
    r.on_post_llm_call(session_id="s", turn_id="t", assistant_response="PRIVATE_ERROR")
    r.on_session_end(session_id="s", turn_id="t", failed=True, turn_exit_reason="error(PRIVATE_ERROR)")
    for span in exporter.get_finished_spans():
        assert "output.value" not in span.attributes
        assert "PRIVATE_ERROR" not in repr(dict(span.attributes)) + str(span.status.description)


@pytest.mark.parametrize("repository_capture", [False, True])
def test_project_cannot_raise_protected_profile_consent(shared, repository_capture):
    env, config, host, root = shared
    store = read_json(config / "credentials.json")
    store["profiles"]["p"]["captureContent"] = False
    store["profiles"]["p"]["projectBinding"] = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    write(config / "credentials.json", store)
    write(root / ".futureagi/agents.json", {"hermes": dict(schemaVersion=1, profile="p", captureContent=repository_capture)})
    resolved = resolve(env.get, host=host, project=root)
    assert not resolved["capture_content"]
    assert resolved["hide_inputs"] and resolved["hide_outputs"]
    env["FI_AGENT_CAPTURE_CONTENT"] = "true"
    assert resolve(env.get, host=host, project=root)["capture_content"]
