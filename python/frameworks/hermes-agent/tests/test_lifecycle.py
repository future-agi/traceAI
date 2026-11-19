"""Long-lived gateways and real multi-batch collector shutdown regressions."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import FakeContext
from traceai_hermes_agent import FutureAGIHermesPlugin, build_tracer_provider, register


class CliContext(FakeContext):
    cwd = ""

    def session_surface(self):
        return "cli"

    def session_cwd(self):
        return self.cwd


def turn(ctx, sid):
    ctx.fire("pre_llm_call", session_id=sid, turn_id=sid)
    ctx.fire("on_session_end", session_id=sid, turn_id=sid, completed=True)


def test_more_than_eight_sequential_workspaces(provider, exporter, tmp_path):
    ctx = CliContext()
    register(ctx, tracer_provider=provider)
    for i in range(12):
        ctx.cwd = str(tmp_path / str(i))
        turn(ctx, str(i))
        ctx.fire("on_session_finalize", session_id=str(i))
        assert len(exporter.get_finished_spans()) == i + 1


def test_idle_eviction_preserves_active_parent_and_child_routes(provider, exporter, tmp_path, monkeypatch):
    retired = []
    original = FutureAGIHermesPlugin.shutdown

    def shutdown(plugin, *args):
        retired.append(plugin._project)
        original(plugin, *args)

    monkeypatch.setattr(FutureAGIHermesPlugin, "shutdown", shutdown)
    ctx = CliContext()
    register(ctx, tracer_provider=provider)
    parent_dir = tmp_path / "parent"
    ctx.cwd = str(parent_dir)
    ctx.fire("pre_llm_call", session_id="parent", turn_id="parent-turn")
    ctx.fire("subagent_start", session_id="parent", child_session_id="child", parent_session_id="parent", parent_turn_id="parent-turn")
    # The child remains active even when the parent finalizes. It must keep its
    # route while other CLI projects churn through slots.
    ctx.fire("on_session_finalize", session_id="parent")
    for i in range(12):
        ctx.cwd = str(tmp_path / str(i))
        turn(ctx, str(i))
        ctx.fire("on_session_finalize", session_id=str(i))
    ctx.fire("pre_llm_call", session_id="child", turn_id="child-turn")
    ctx.fire("on_session_end", session_id="child", turn_id="child-turn", completed=True)
    assert parent_dir not in retired
    spans = exporter.get_finished_spans()
    assert {"parent", "child"} <= {s.attributes.get("session.id") for s in spans}
    assert len(spans) == 14
    ctx.fire("subagent_stop", session_id="parent", child_session_id="child", parent_session_id="parent")
    for i in range(8):
        ctx.cwd = str(tmp_path / f"after-{i}")
        turn(ctx, f"after-{i}")
        ctx.fire("on_session_finalize", session_id=f"after-{i}")
    assert parent_dir in retired


def test_active_projects_are_never_evicted(provider, exporter, tmp_path):
    ctx = CliContext()
    register(ctx, tracer_provider=provider)
    for i in range(8):
        ctx.cwd = str(tmp_path / str(i))
        ctx.fire("pre_llm_call", session_id=str(i), turn_id=str(i))
    ctx.cwd = str(tmp_path / "ninth")
    turn(ctx, "ninth")
    assert not exporter.get_finished_spans()
    for i in range(8):
        # Deliberately leave cwd on another project: session routing is pinned.
        ctx.fire("on_session_end", session_id=str(i), turn_id=str(i), completed=True)
        ctx.fire("on_session_finalize", session_id=str(i))
    turn(ctx, "ninth")
    assert len(exporter.get_finished_spans()) == 9


def test_shutdown_one_deadline_for_slow_collector_and_many_batches():
    started = threading.Event()
    requests = []

    class Collector(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(time.monotonic())
            started.set()
            time.sleep(0.65)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = build_tracer_provider(api_key="synthetic-key", secret_key="synthetic-secret",
        base_url=f"http://127.0.0.1:{server.server_port}", project_name="shutdown-regression")
    plugin = FutureAGIHermesPlugin()
    plugin._provider = provider
    tracer = provider.get_tracer("shutdown-test")
    try:
        for i in range(128):
            tracer.start_span(f"inflight-{i}").end()
        assert started.wait(3)
        for i in range(1024):
            tracer.start_span(f"queued-{i}").end()
        start = time.monotonic()
        plugin.shutdown()
        elapsed = time.monotonic() - start
        assert elapsed < 3.4, elapsed
        # Wait for the existing network request to finish; no new request may
        # start after the deadline, nor may a second shutdown wait unboundedly.
        time.sleep(0.8)
        assert len(requests) < 9
        assert all(t < start + 3.1 for t in requests)
        again = time.monotonic()
        plugin.shutdown(deadline=again)
        assert time.monotonic() - again < 0.1
        print(f"shutdown elapsed={elapsed:.3f}s requests={len(requests)} (nine batches queued)")
    finally:
        provider.shutdown(timeout_millis=0)
        server.shutdown()
        server.server_close()
