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
exact repository-relative test node IDs (including parameter suffixes),
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
