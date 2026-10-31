# fi-agent

`fi-agent` is a local observability runtime for explicitly enabled coding-agent integrations. It is not a coding agent, model proxy, or agent orchestrator.

The executable includes an opt-in Claude Code native tracing preview for version 2.1.316 on macOS. Installing the runtime does not enable tracing. Agent SDK instrumentation remains a separate integration surface.

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

## Claude Code native preview

Enable the route, then launch Claude through the explicit managed launcher:

```sh
fi-agent enable claude-code --surface cli --profile work
fi-agent run claude-code --surface cli -- --print "Explain this repository's entry point."
fi-agent doctor claude-code --surface cli
fi-agent disable claude-code --surface cli
```

This uses Claude's documented enhanced native OTel tracing, not detailed-beta request-body tracing. The managed runtime receives traces on an authenticated loopback endpoint, minimizes them before the shared spool, and preserves native trace/span/parent IDs. Model counters include cached input exactly once; tool, execution and permission spans never own model usage. Native request duration includes retries, not separate wire-attempt latencies. A source outcome that is not reported stays incomplete rather than becoming success.

Only the CLI print surface has real-binary qualification. Interactive TTY, Desktop, remote hosts and managed-policy deployments are not qualified. Version/schema mismatches reject capture rather than silently reinterpreting usage. `doctor` reports activation only after a durable native ACK in the current runtime; it never treats installation or HTTP acceptance as backend persistence.

No Claude settings files, credentials, model selection, base URLs, permission policies or hooks are modified. `fi-agent run` carries a short-lived local capability in the process-scoped traces header; agent tools may inherit it. This is a same-user boundary, not protection from other processes running as that user. It is not a Future AGI credential.

Structure-only is the only enabled capture policy. Transcripts and an enrichment plugin remain held: native request IDs are not proven transcript message identities. There is no timestamp/FIFO join and no second additive request source. Native trace IDs group the accounting plan; this profile does not claim source prompt/turn identities across arbitrary inbound trace contexts.

## Native telemetry coexistence

An unowned native destination is a conflict by default. Do not replace a generic logs/metrics/traces endpoint with a traces-only receiver. External collector fan-out requires an independently validated configuration; this runtime does not relay another vendor's credentials or claim delivery to that vendor.

## Compatibility

Node 22.16–22.x and 24.x are supported runtime lines. Windows activation is held. The SQLite binding is pinned; an unqualified SQLite engine is refused rather than substituted.

The library exports an explicit adapter registry and runtime interfaces for maintained integrations. It does not discover or execute plugins from repository configuration. The synthetic reference adapter belongs only to the source conformance tests and is not distributed in the npm package.
