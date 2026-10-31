# fi-agent

`fi-agent` is a local observability runtime for explicitly enabled coding-agent integrations. It is not a coding agent, model proxy, or agent orchestrator.

Installing the runtime does not enable tracing. Cursor has separate, opt-in IDE and CLI observer previews; other agent adapters remain unregistered. Agent SDK instrumentation remains a separate integration surface.

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

## Cursor observer preview

The Cursor adapters currently qualify **documented payload fixtures, not real host activation**. They require explicit `--preview`; the capability manifest lists the exact accepted payload versions (IDE `3.24.18`, CLI `2026.10.17`). Unknown versions are ignored by the neutral hook and never become captured data. Do not interpret this preview as a released full-request integration.

```sh
fi-agent enable cursor --surface ide --profile work --preview --capture structure --transcript off
fi-agent doctor cursor --surface ide
fi-agent disable cursor --surface ide
```

Use `--surface cli` for the CLI observer. Automatic installation is available only when the official user-level `hooks.json` does not exist; it creates the file without replacing a competing save. An unchanged existing installation is a no-op. Adding hooks to an existing file, adding a second surface, changing executable paths, and automatic hook removal are held with `CURSOR_CONFIG_MANUAL_CLEANUP`: ordinary editor saves cannot be safely coordinated with a replacing rename. Do not delete an existing configuration to bypass this hold. The directory and file must be owner-only and nonsymlink. Existing legacy `fi-hook-to-otlp` registrations remain a conflict. No credential, permission, model, provider, or trust setting is changed.

`disable` and `uninstall` first persist a disabled capture state, then fence the runtime; they do not need upstream credentials or an active profile. A stopped runtime is started with the route already disabled. If the runtime cannot start, the command reports failure and retains the disabled restart state. Once fenced, hooks that require an existing-file edit are left in place but cannot admit capture. The command returns exit code **4**, `capture-disabled-cleanup-pending`, and `CURSOR_CONFIG_MANUAL_CLEANUP` rather than claiming complete removal. In Cursor's user-level `hooks.json`, remove only the `postToolUse`, `postToolUseFailure`, and `stop` entries for the recorded `fi-cursor-hook.cjs` command and matching route; preserve other entries and settings. Run `uninstall` again to finish. Modified or ambiguous registrations remain conflicts. A bounded owner-only `hooks.json.fi-agent-owned.json` sidecar retains only the original executable, hook, route, and socket paths to identify those entries after upgrades; it never backs up host configuration. Rollback also refuses to replace an existing host file.

Turns group observed activity by conversation; tools use `tool_use_id`. The runtime durably reserves each tool's turn parent in the same transaction as the tool. Source admission is serialized within each route/capture epoch before credential lookup. A stop starts a durable two-second reconciliation grace before the parent becomes immutable; tools arriving within that grace extend its observed interval without changing the terminal outcome. Repeated stops do not extend the grace, and conflicting terminal outcomes are held. If no stop arrives within 15 minutes of the first tool observation, the runtime finalizes an incomplete parent using only the observed interval, not the timeout as an execution end. Pending parents survive runtime restarts and obey disable/purge fences. Further new work for a finalized turn is held rather than rewriting an immutable trace. Reported tool and turn intervals are callback-anchored estimates, not model latency or the whole turn's duration. Thought and final-answer hooks never become LLM requests. Prompts, responses, tool payloads, error text, model labels, workspace paths, and transcript paths do not leave the hook. Doctor distinguishes installed hooks from an observed durable event, and never equates activation with server persistence.

The bounded stream-json reader can plan explicit CLI imports in `--mode import-only --transcript structure`, with `--schema cursor-cli-stream-2026-10-31-v1`. It requires one terminal result with `request_id` and source timestamps; only `model_call_id` flushes identify reconstructed inference units. Anonymous messages and aggregate durations do not. Import commit/export is **held** until the hook/stream source-work alias is proven, even after uninstall/reinstall. Persisted IDE/CLI transcripts, live incremental tailing, usage, subagent ancestry, and automatic bundle updates remain held. No real agent home is scanned.

## Native telemetry coexistence

An unowned native destination is a conflict by default. Do not replace a generic logs/metrics/traces endpoint with a traces-only receiver. External collector fan-out requires an independently validated configuration; this runtime does not relay another vendor's credentials or claim delivery to that vendor.

## Compatibility

Node 22.16–22.x and 24.x are supported runtime lines. Windows activation is held. The SQLite binding is pinned; an unqualified SQLite engine is refused rather than substituted.

The library exports an explicit adapter registry and runtime interfaces for maintained integrations. It does not discover or execute plugins from repository configuration. The synthetic reference adapter belongs only to the source conformance tests and is not distributed in the npm package.
