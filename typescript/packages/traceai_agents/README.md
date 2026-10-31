# fi-agent

`fi-agent` is a local observability runtime for explicitly enabled coding-agent integrations. It is not a coding agent, model proxy, or agent orchestrator.

Installing the runtime does not enable tracing. The opt-in Codex CLI observer supports pinned 0.165.0 and 0.166.0 rollouts on macOS. Agent SDK instrumentation remains a separate integration surface.

## Codex CLI observer

After approving a destination profile, explicitly consent to structural rollout reading:

```sh
fi-agent enable codex --surface cli --profile work --project coding-agents --capture structure --transcript structure
```

Run the two official `codex plugin` commands printed by enable to install the generated local repository-marketplace bundle. Review and approve its hooks in Codex's `/hooks` interface, then restart the session. Enable never edits Codex configuration, trust, model selection or provider credentials. The immutable bundle includes its own observer client and uses the host-supplied `PLUGIN_ROOT`; hooks do not download packages or contact Future AGI directly.

```sh
fi-agent doctor codex --surface cli
fi-agent status
fi-agent disable codex --surface cli
```

Capture reconstructs requests from durable response identities and exact per-response usage, not anonymous pre/post-hook pairs or token summaries. Request timing is unknown, not claimed wire latency. Tools are siblings of model requests; explicit spawn-call identities link detached child traces to their spawning tool. Proven foreground intervals can nest under that tool; missing spawn context is held. No source content is stored. Unknown source versions, inherited/fork histories without proven ownership, rotation and compression hold rather than mint new work IDs.

`fi-agent import codex --surface cli --transcript structure --path /absolute/path/rollout.jsonl --dry-run` selects one file. Commit the returned plan with `--commit PLAN_ID`; no historical-home scan is performed. Unfinished turns remain unfinalized. For child runs, select the directory containing the parent and child `rollout-*.jsonl` files; selection is nonrecursive and limited to 16 matching files and 64 entries. The importer is bounded to 1 MiB total and 128 spans. See `profiles/capabilities.json` for all limits and release holds.

`fi-agent update codex --surface cli --check` validates the actual candidate without changing it. `update` stages the immutable replacement and prints official plugin replacement commands; review the new hooks in Codex. `update --rollback` stages the previous compatible observer bundle, without rolling back the runtime or storage schema. Neither operation grants trust or enables a disabled route.

Disable revokes runtime admission and holds queued data, even while the runtime is stopped. Add `--transcript off` to persistently revoke structural-read consent. An offline `--purge-capture` reports pending work until runtime startup reconciles the fence. Remove the host's installed copy separately with `codex plugin remove traceai-codex@traceai-observers`. The runtime never removes original Codex history or another plugin's configuration.

## Authentication

Create a profile with a fixed project and destination. Enter credentials through a masked terminal prompt or protected JSON on standard input; never put keys in command arguments.

```sh
fi-agent auth add --profile work --project coding-agents --tty
```

The default destination is the complete `https://api.futureagi.com/tracer/v1/traces` endpoint. To choose a different HTTPS destination, use `--endpoint` when creating the profile. An existing profile cannot silently change destinations or projects.

Credentials use the operating system's credential store. Linux requires Secret Service; the runtime does not silently substitute a memory-only keyring. On a headless host, explicitly choose `--storage file` to use a separate owner-only credential file. The store is never placed in agent configuration. Same-user processes are outside this protection boundary.

`--stdin` accepts one bounded JSON object with `api_key` and `secret_key`. `--credential-file` accepts an explicit absolute, owner-only nonsymlink file. These input modes are mutually exclusive.

## Local state and delivery

```sh
fi-agent runtime start
fi-agent status --json
fi-agent runtime stop
```

Status distinguishes local queue state, transport acceptance and unknown remote persistence. HTTP success alone is not proof that a trace can be read back. Export requires a deployment-provided `immutable-agent-v1` contract manifest approved with the profile; the runtime does not probe an invented capability endpoint.

Only structure-only capture is admitted by this foundation. Content persistence is held rather than stored without a qualified per-policy encryption boundary. Unknown agents, surfaces and source schemas fail closed.

A dedicated child process owns the single SQLite connection. The owned spool has a 256 MiB physical cap, including its database, WAL, shared-memory file and maintenance reserve. Capture pauses when checkpoint, filesystem headroom or quota checks fail. This cap does not include installed packages, original agent transcripts, filesystem snapshots or external files. Local receipt retention never authorizes inventing new IDs for previously observed work.

Authenticated exports use the approved complete endpoint, verify TLS and never follow redirects, including same-origin redirects. A partial OTLP rejection is ambiguous, not wholesale success. Disabling a route stops admission and holds queued records instead of silently draining them.

## Native telemetry coexistence

An unowned native destination is a conflict by default. Do not replace a generic logs/metrics/traces endpoint with a traces-only receiver. External collector fan-out requires an independently validated configuration; this runtime does not relay another vendor's credentials or claim delivery to that vendor.

## Compatibility

Node 22.16–22.x and 24.x are supported runtime lines. Windows activation is held. The SQLite binding is pinned; an unqualified SQLite engine is refused rather than substituted.

The library exports an explicit adapter registry and runtime interfaces for maintained integrations. It does not discover or execute plugins from repository configuration. The synthetic reference adapter belongs only to the source conformance tests and is not distributed in the npm package.
