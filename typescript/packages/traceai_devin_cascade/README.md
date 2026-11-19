# Cascade tracing adapter

Private implementation of the **Cascade** hook protocol in Devin Desktop and legacy Windsurf. This is not the Devin Local plugin lifecycle, hosted Devin, or the Devin SDK instrumentor. The package deliberately has `private: true` and an empty host qualification allowlist. Do not publish it, add it as a required dependency of the released unified CLI, or publish a customer install command until a pinned GUI version has passed qualification.

## Intended setup contract

The unified CLI reserves `devin-cascade`. Once qualified, the customer path is keys → `npx @traceai/agents setup devin-cascade --project coding-agents` → restart the editor and use Cascade. The core handles masked key entry or environment overrides, authenticated validation, the private credential store, per-project privacy overrides, status, and the persistent disable fence. Today the registry reports `CASCADE_GUI_QUALIFICATION_REQUIRED`; configuration alone is not installation.

The adapter installs only `pre_user_prompt` and `post_cascade_response_with_transcript`. It preserves other JSON/JSONC hooks and comments, owns only exact matching entries, pins an absolute Node path and a content-addressed standalone bundle outside npm's transient cache, and downloads nothing during a hook. No daemon, gateway, copied customer script, model configuration change, trust approval, or permissions change is needed.

User hooks live at `~/.codeium/windsurf/hooks.json`. Project hook reconciliation respects `.devin/hooks.json` precedence and does not shadow active `.windsurf/hooks.json` hooks. The central credential directory is bound in the hook command so GUI startup does not depend on interactive shell environment variables. The workspace's current directory supplies project routing; no event-provided destination or project path is trusted.

## Trace semantics

- A matching prompt and terminal hook produce one AGENT root per execution, with one stable session per trajectory.
- The documented `planner_response` steps are **reconstructed generations** (LLM children with `fi.agent.fidelity=reconstructed-message`), not native model requests. Their input contains the prompt and bounded preceding context. `code_action` becomes a TOOL child with its documented path and new content. Names, ordering and source identities are preserved; source timings for steps are not invented.
- The documented transcript contains no verified native generation boundary, token/cost counters, subagent parent IDs, or compaction schema. These fields and spans are **not fabricated**. Unknown step shapes mark the turn incomplete. Additional mappings require recorded, sanitized examples from a pinned host.
- Only the newest prompt in an initial transcript is eligible. No historical backfill. Later snapshots must extend the previous HMAC-bound byte cursor; rewrites, truncation, missing/pruned files, partial writes, missing prompt boundaries and late data fail closed with metadata-only diagnostics.
- A response hook is the completion boundary. Bounded polling waits for asynchronous file creation and complete stable JSONL, never treats inactivity as agent completion, and checks the current prompt and execution before export. Multi-turn concurrent GUI behavior remains a qualification gate.
- Claims are persisted before transport. Outages, partial rejection or uncertain delivery can lose spans; there is no blind replay and no exactly-once promise.

## Privacy and limits

Content is on by default through the shared core. It can include code, file contents, prompts and secrets that the agent reads. `FI_AGENT_CAPTURE_CONTENT=false` or setup's `--no-capture-content` selects structure-only mode. Existing `FI_HIDE_*` controls remain stricter ceilings. Captured fields are bounded and known Future AGI credentials are redacted. No email or host account identity is harvested.

Only the hook-provided, trajectory-bound `~/.windsurf/transcripts/<trajectory>.jsonl` is eligible. Symlinks, hardlinks, non-owner files, non-private permissions and non-regular files are rejected. Reads are bounded to 4 MiB, 10,000 lines and 200 steps per current turn. Settling takes at most six reads, spaced 100 ms apart. Hook input is capped at 256 KiB. The packaged process always exits zero, emits no stdout/stderr, and has a six-second wall-clock deadline; Cascade's terminal hook is asynchronous.

The adapter does not copy transcript content into recovery files. Local state contains HMACs, offsets, counters and timestamps, uses 0600 files beneath 0700 directories and is capped at 512 conversations. When a new conversation is observed, state files older than 24 hours are pruned; no background process deletes metadata while the adapter is inactive. The host itself creates content-bearing transcript files and prunes the oldest above 100 files; its retention is independent. The disable fence is checked before reads and before sending. Same-OS-user hostile processes are outside the credential-file isolation boundary.

## Source contracts

- [Cascade hooks](https://docs.devin.ai/desktop/cascade/hooks): hook names, execution and trajectory IDs, user/workspace path precedence, asynchronous JSONL, file mode and pruning.
- [Desktop changelog](https://docs.devin.ai/desktop/changelog): transcript hook introduced in v1.9566.9. This is a protocol minimum, **not** a tested host range. Newer versions are not automatically supported.
- `test/fixtures/documented-transcript.jsonl` is the official documentation example, not a recording from an executed agent. Tests using it are contract tests, not real-binary qualification.

Qualification requires an authorized actual Cascade GUI/account run, source-backed schema captures, workspace trust behavior, asynchronous repeated-prompt/resume checks, trace readback, and real Future AGI screenshots or a short recording. A headless compatible-model route for this GUI surface has not been established. Never substitute the Devin Local CLI or a fixture replay as proof. Keep the customer docs route and media unpublished until these gates pass.
