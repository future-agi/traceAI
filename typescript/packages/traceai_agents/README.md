# fi-agent

`fi-agent` is a local observability runtime for explicitly enabled coding-agent integrations. It is not a coding agent, model proxy, or agent orchestrator.

The executable includes no enabled or installable agent adapters. Installing the runtime does not enable tracing. Agent SDK instrumentation remains a separate integration surface.

## Authentication

Create a profile with a fixed project and destination. Enter credentials through a masked terminal prompt or protected JSON on standard input; never put keys in command arguments.

```sh
fi-agent auth add --profile work --project coding-agents --tty
```

The default destination is the complete `https://api.futureagi.com/tracer/v1/traces` endpoint. To choose a different HTTPS destination, use `--endpoint` when creating the profile. An existing profile cannot silently change destinations or projects.

Credentials use the operating system's credential store. Linux requires Secret Service; the runtime does not silently substitute a memory-only keyring. On a headless host, explicitly choose `--storage file` to use a separate owner-only credential file. The store is never placed in agent configuration. Same-user processes are outside this protection boundary.

`--stdin` accepts one bounded JSON object with `api_key` and `secret_key`. `--credential-file` accepts an explicit absolute, owner-only nonsymlink file. These input modes are mutually exclusive.

`auth remove --profile work` removes upstream authentication and holds matching routes. Protected binding identity and content keys remain in the separate credential store so that `auth add` can restore authentication for the same destination without changing work IDs. Removing authentication does not remove owned agent registrations; `disable` and `uninstall` remain available without upstream credentials.

## Local state and delivery

```sh
fi-agent runtime start
fi-agent status --json
fi-agent runtime stop
```

Status distinguishes local queue state, transport acceptance and unknown remote persistence. HTTP success alone is not proof that a trace can be read back. Export requires a deployment-provided `immutable-agent-v1` contract manifest approved with the profile; the runtime does not probe an invented capability endpoint.

Only structure-only capture is admitted by this foundation. Content persistence is held rather than stored without a qualified per-policy encryption boundary. Unknown agents, surfaces and source schemas fail closed.

A dedicated child process owns the single SQLite connection. The owned spool has a 256 MiB physical cap, including its database, WAL, shared-memory file and maintenance reserve. Capture pauses when checkpoint, filesystem headroom or quota checks fail. This cap does not include installed packages, original agent transcripts, filesystem snapshots or external files. Local receipt retention never authorizes inventing new IDs for previously observed work.

Authenticated exports use the approved complete endpoint, verify TLS and never follow redirects, including same-origin redirects. A partial OTLP rejection is ambiguous, not wholesale success. With the approved immutable backend contract, ambiguous deliveries retry the original bytes and IDs with backoff, subject to the same destination holds, policy fences and 24-hour queue expiry. Temporary native-receiver admission failures return a retryable HTTP status rather than a permanent partial rejection.

Runtime-version-only replay is a duplicate, not a new span or an update to accepted wire bytes. Changed timing, parentage, usage or source facts remain immutable conflicts. Legacy wire-only receipts can be recovered from retained payloads or a proven exact digest for the original 0.1.0 format; receipts from unknown historical formats without retained payloads remain held.

Disabling a route stops admission and holds queued records instead of silently draining them. If the daemon is stopped, `disable`, `uninstall` and authentication removal use a temporary control-only runtime that cannot admit or export data and closes when the command finishes.

`run --profile` must match the installed route's profile and binding; it cannot redirect an installed route to another approved project. An explicit `--transcript off` overrides previous consent. Transcript-dependent activation or launch then refuses rather than reading transcripts; an omitted option retains the existing approval.

## Native telemetry coexistence

An unowned native destination is a conflict by default. Do not replace a generic logs/metrics/traces endpoint with a traces-only receiver. External collector fan-out requires an independently validated configuration; this runtime does not relay another vendor's credentials or claim delivery to that vendor.

For a qualified native adapter, `run` asks the runtime for a short-lived loopback traces receiver. The receiver runs inside the runtime process, never receives Future AGI credentials, and admits only the adapter's fixed source schema. Disable, configuration refresh and runtime stop revoke these receivers; a killed launcher's lease expires. A native source event counts as activation only after it is durably committed locally.

The per-launch capability is passed to the agent in a trace-specific header variable. Tools and subprocesses started by that agent inherit it. It grants access only to that loopback lease, but it is not a sandbox against other same-user processes.

If the local runtime is unavailable or holds the route, `run` starts the agent without managed trace variables and returns the agent's own exit status or signal. Routes that are not enabled, unsupported profiles and destination conflicts refuse before launch. Bundled Copilot CLI and VS Code profiles remain qualification-only and cannot use this path yet.

## Compatibility

Node 22.16–22.x and 24.x are supported runtime lines. Windows activation is held. The SQLite binding is pinned; an unqualified SQLite engine is refused rather than substituted.

The library exports an explicit adapter registry and runtime interfaces for maintained integrations. It does not discover or execute plugins from repository configuration. The synthetic reference adapter belongs only to the source conformance tests and is not distributed in the npm package.
