# Compatibility contract

This directory is the versioned ABI shared by the compatibility foundation,
truthful-regression PR (PR1), registry-canary PR (PR2), and evidence-reporter PR
(PR3). Registry discovery is not a passing compatibility test.

## Canonical files

- `registry.schema.json`: JSON Schema draft 2020-12, including the exact-test
  exception definition at `#/$defs/exception`.
- `registry.json`: complete package-language inventory. IDs are stable; source
  paths and package names remain case-sensitive. The inventory contains 144
  packages (67 Python, 47 TypeScript, 5 Go, 24 Java, 1 C#) and two separately
  labelled existing Java example checks.
- `results.schema.json`: the PR2-to-PR3 results ABI. Per-run files are generated
  artifacts, never checked-in claims or automatic timestamp commits.

PR1 owns subsequent registry qualification/exception changes and semantic
validation. PR2 owns discovery, planning, execution and the canary workflow.
PR3 owns reporting, incident lifecycle and generated customer documentation.
Do not create a second integration inventory in a planner.

PR1 adds the optional boolean `integrations[].regression_ci` (absent means
false). This additive schema extension explicitly preserves the nine targets
already executed by `integration-tests.yml`; many other registry entries have
candidate contract commands but were never wired to that workflow. The planner
now derives executable targets from this field and emits `coverage_gaps` for
affected remaining inventory, rather than treating test files as execution.
No results ABI fields change. Consumers should absorb PR1's registry/schema
before validating its registry snapshot.

The Java example runtime observations reference the parent compiler target
(`>=17`), not a verified example runtime; their legacy jobs execute on JDK 21.

## Registry interpretation

`package_dir` is repository-relative. `upstream` names primary and coupled
packages; `range_source` identifies the exact metadata/runtime/development
source of the declaration. A missing declaration is `null`, not unbounded
support. `exact_floor` is a selected published release, not a verified floor.
A Go/Maven baseline pin must not be interpreted as a tested minimum. Python
Poetry constraints, PEP 440, npm semver and Go MVS are different semantics.

The only initially enabled candidates are Python OpenAI, Anthropic and
LangChain. Their `pilot_pending` status deliberately does not claim they are
qualified. An individual passing candidate case does not change that status.
PR1 must establish real-SDK execution, align declarations, and record evidence
before promoting a target. Anthropic's new contract path is intentionally
reserved for PR1; a missing suite is blocked, never empty green.

Other entries are explicitly unqualified or known broken; an inventory test
command is an audit starting point, not certification. Never run a target
merely because its files or a `test` script exist. Expansion requires an
explicit qualification change and measured low-noise pilot operation.

`contract_test.command` is an argv array executed from `cwd`; `paths` are
repository-relative suite identities. Append a per-case JUnit output option.
Test from an isolated consumer with installed built/published distributions,
not source-shadowed imports. Snapshot the entire resolved environment.
LangChain's initial suite excludes networked resolver checks and Deep Agents;
resolve its core/graph pair together, including historical constraints.

## Scoped exceptions

The canonical exception list is `integrations[].exceptions`. Each entry has
exactly one repository-relative test node ID (including its parameter suffix),
upstream/runtime constraints, expected outcome and SHA-256 failure signature,
a real GitHub issue, human owner, added/expiry dates and exit criteria. Maximum
lifetime is 14 days. No wildcard IDs, fake issues or indefinite expiry.

All exceptions still execute and produce `quarantined`, never `passed`.
Unexpected pass, new failure/signature, missing tests, unexpected skip,
installation/collection error or expired exception fails closed. Package-wide
legacy reasons are provenance only and never authorize a canary failure.
The old list may survive while repair PRs remove lines, but new scoped verdicts
must not fall back to its permission-to-fail behavior. If no valid scoped
exception can be established, the failure stays visible.

Fingerprints use the actual JUnit exception diagnostic and type, never a displayed
traceback source line (which may contain an earlier unrelated assertion). All
producers must use `validate.failure_signature` / `validate.junit_tests` so that
quarantine matching and downstream evidence reconciliation use the same digest.
Existing fingerprints require review when the diagnostic algorithm changes.
A signature includes its exact node ID. Multiple parameterized nodes therefore
need separate exception IDs and signatures, not one signature shared across nodes.
The array-shaped `test_ids` field is retained for ABI compatibility, with length 1.

Each Python invocation writes `<report>.xml.exit-code`. The verdict reconciles
each status against its own JUnit **before** considering any exact exceptions.
An all-passing suite with a coverage/runner exit of 1 remains red even if another
suite has an exactly quarantined test failure. A maximum aggregate exit is only
a display/checksum, never execution evidence.

Canonical canary artifacts additionally preserve
`invocations/<case_id>-attempt-<attempt>.json` (schema_version 1, case_id,
attempt, source_sha, plan_sha256, collection, contract). Each invocation is null
if never started; otherwise it records exit_code (integer or null), timed_out,
started_at/completed_at, and nullable evidence_path/evidence_sha256. Every
successful/flaky/quarantined case requires a successful full collection (exit 0,
no deselection/errors) and independently reconciled contract invocation. A timeout,
unknown exit, runner/session/coverage failure or absent manifest cannot attest
success. Failed/blocked attempts keep their actual statuses. The collector copies
these sidecars and the adjacent JUnit exit-code files into the aggregate.

## Results ABI

PR2 uploads `compatibility-plan` (including `compat-plan.json`), per-case
`compatibility-case-<case_id>` artifacts, and aggregate
`compatibility-results` containing `results.json`, JUnit and dependency locks.
PR3 reads these exact names. Artifact references inside JSON are relative,
non-traversing paths; consumers impose size/count limits and reject symlinks.

A result records source and workflow SHAs, plan digest and all planned IDs,
72-hour cutoff, requested and actually resolved tuples, source/published mode,
instrumentation/core versions, runtime, lock digest, exact per-test JUnit
outcomes and normalized failure signatures. Emit a failed/blocked record even
when install fails or JUnit is missing. Results and retries retain the same
plan; never resolve a new latest during a retry. Recovery compares the same
integration/version/runtime/mode/suite scope, not a different newer version.

Schema validation is necessary, not sufficient. PR1's semantic validator must
reject duplicate/unknown IDs, missing planned cells, SHA/plan mismatches,
nonzero/skip violations, false pass with failures, requested/resolved mismatch,
bad lock hashes and expired/mis-scoped exceptions. Qualified passed cases need
nonzero executed contracts and a reproducible dependency lock. Preserve both
attempts; a retry pass after failure is `flaky`, not an ordinary pass.

`validate_results(..., artifact_root=..., source_root=..., expected_plan=...,
plan_bytes=...)` validates original reports, lock digests, trusted suite digest,
registered command/scenarios and every statically declared test function in the
registered paths. Parameterized nodes retain exact IDs in JUnit and exceptions;
complete-suite coverage is checked at the function level. The caller supplies the
trusted immutable source checkout and plan bytes; downloaded argv is never run.
A passing JSON object without an artifact root is not execution evidence.

Lock-byte hashes alone do not establish an installed environment. Qualified
Python lock adapters are `pip-freeze` (complete `name==version` inventory) and
`pip-report` version 1. Reject malformed, empty, duplicate and contradictory
identities. Every observed instrumentation/core/upstream/companion version must
match the installed snapshot; a strict plan-backed replay also reconciles the
**complete** snapshot with the original resolution report and hash-pinned
requirements, whose exact bytes/digests are bound by the plan. These files remain
at their original `locks/` paths or beneath
`evidence/<case_id>/attempt-<attempt>/locks/` in the aggregate. Other lock adapters
remain unqualified, not silently treated as equivalent to Python. Registry roles
are bound to normalized distribution names; roles cannot be swapped and the same
identity cannot occur under multiple roles. A truthful failed/blocked
`requested_resolved_mismatch` observation may retain an actual installation that
differs from the intended frozen report; validate both inventories and retain the
mismatch, never replace actual observations with intended versions. This cannot
attest passed/quarantined/flaky compatibility.

`flaky` requires all the same successful execution validation as `passed`, plus a
retained prior genuine failed/blocked attempt with failure evidence. It never
means empty, skipped, partial or still failing. Previously unobserved installation
fields (empty resolved tuple, null lock, empty pre-execution suite) may populate on
retry; already observed identities, versions, lock and suite cannot change. The
plan and requested selection remain immutable, and both attempts remain visible.
If a **later** upload is missing/malformed, a wholly unobserved synthesized
`blocked`/`automation` retry (empty resolved/suite/JUnit/scenarios, null lock) is
also allowed. It is UNKNOWN, not a contradictory empty installation. The first
attempt remains intact; never copy its installed observations or JUnit into the
retry. Partial observations, genuinely changed versions/locks, claimed suites or
successful statuses cannot use this narrowly scoped unknown-retry rule.

The `results` CLI requires the original `compat-plan.json` beside `results.json`
(or `--plan PATH`) and a `--root` checkout at the tested source SHA. Plan identity,
requested tuples, resolved tuples, discovery and runtime are reconciled before a
passing claim is accepted. Full-suite scenarios are owner's declarations bound
to the complete trusted suite, not arbitrary labels supplied by the artifact.
Dynamic/inherited Python suites and non-Python evidence need qualified native
collector adapters before this validator will accept a successful case.

Python requests are checked using PEP 440/508. Native non-Python requests are
accepted only as exact pins, never reinterpreted with Python range semantics.
Latest-eligible evidence requires the complete upstream/companion discovery set,
non-yanked releases, a publication or first-seen timestamp at/before the immutable
72-hour cutoff, and matching selected/resolved/latest-eligible versions. Stable
latest-eligible rejects PEP 440 prerelease/dev versions even when aged; these may
run only in explicitly registered exploratory `prerelease` cells. Existing
locked regression jobs do not claim floating-latest coverage.

Initial counts/statuses are historical fixture data only. Production tests check
metadata/inventory completeness and valid changes; they do not prohibit adding
integrations, reviewed exceptions or evidence-backed qualification.

## Execution and privileges

Default cooldown is **72 hours**, excluding yanked/ineligible releases.
Separate absolute latest, latest eligible and actually tested versions.
No live provider execution or spend is enabled.

Install/build jobs have `contents: read`, no provider/publish secrets or write
token, and checkout with `persist-credentials: false`. After dependencies are
installed, PR2 runs the contract process and descendants in an isolated Linux
network namespace with only loopback brought up. Namespace setup failure is a
failure, not permission to use the host network. Probe external denial and
loopback availability before testing. Exporter URLs at `127.0.0.1:9` alone are
not network isolation.

Ordinary Python CI's registered offline contracts and the exact-SDK contract jobs
invoke `validate.py sandbox` with workflow-owned argv. The trusted runner creates
an empty Linux network namespace, brings up loopback, drops supplementary groups,
capabilities and privilege escalation, and proves external IPv4/IPv6/connect_ex
and descendant denial plus child loopback success before executing tests. Failure
to set up/probe the namespace fails the invocation; no host-network fallback.
This baseline runner is not a replacement for PR2's stronger clean-consumer,
read-only filesystem sandbox. Artifact command/reproduction arrays remain data;
the results CLI never sends them to either runner.

`Compatibility canary` supports PR self-tests, branch `workflow_dispatch`, and
one default-branch scheduler; it explicitly records the checked-out dev SHA.
A cron committed only to dev is dormant until an operator separately lands it
on main. Do not modify main, repository settings, or branch protection here.

`Compatibility report` uses trusted default-branch code. Tests and artifact
parsing are separate from the narrowly privileged incident job
(`actions: read`, `contents: read`, `issues: write`). Untrusted PR/branch results
may render a preview but cannot create/update/close incidents. Never execute
artifact-supplied commands, import source-checkout code in the reporter, or use
`pull_request_target` to install dependencies. Publish generated JSON/Markdown
as Actions artifacts; package publishing, arbitrary git pushes and external
Slack/Linear delivery are not enabled by this contract.

## Integration order

All three PRs start from the same single foundation commit and target dev.
Land foundation through PR1 first; merge current dev into each later branch
(no rebase/force-push) before its final validation. PR2 follows PR1; PR3 follows
PR2. A later branch must preserve earlier files and validate its artifacts
against this ABI. Human review/merge and separate main scheduler rollout remain
explicit operator decisions.
