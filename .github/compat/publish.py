"""Render observed combinations, not inferred support from dependency ranges."""
from __future__ import annotations

import json
from incidents import digest, heartbeat, iso, name_key, observation_fresh, parse_issue, reporter_owned, text, timestamp


def known_bad_rows(states, registry, now, issues):
    """Durable failures are evidence references, never newly observed executions.

    Only reconciled current evidence and schema-valid bot-owned incident state
    reach this function. Expiration of a download/discovery window cannot erase
    an unrecovered tuple. Keep age and missing retained artifacts explicit.
    """
    numbers = {}
    for issue in issues:
        if reporter_owned(issue):
            state = parse_issue(issue)
            if state:
                numbers[state["key"]] = issue["number"]
    targets = {t["id"]: t for t in registry["integrations"]}
    rows = []
    for state in states:
        if state["kind"] != "compatibility" or state["status"] == "recovered":
            continue
        failed_at = max((s["failure"]["at"] for s in state["scopes"].values()), key=timestamp)
        rows.append({"id": state["key"], "integration_id": state["integration_id"],
                     "language": targets[state["integration_id"]]["language"], "tuple": [list(p) for p in state["tuple"]],
                     "status": state["status"], "signature": state["signature"], "summary": state["summary"],
                     "issue_number": numbers.get(state["key"]), "last_failure": failed_at,
                     "age_days": round(max(0, (now - timestamp(failed_at)).total_seconds() / 86400), 3),
                     # Keep durable references, not another copy of potentially
                     # thousands of test IDs/argv already retained in the issue.
                     "scopes": [{"suite_sha256": scope["suite"], "evidence": {
                         k: scope["failure"][k] for k in ("run_id", "url", "at", "case_id", "mode", "runtime",
                                                         "source_sha", "resolved", "dependency_lock")}}
                                for _, scope in sorted(state["scopes"].items())]})
    return sorted(rows, key=lambda r: (r["integration_id"], r["id"]))


def build_index(runs, registry, now, *, fixture=False, states=(), issues=()):
    targets = {t["id"]: t for t in registry["integrations"]}
    rows = {}
    for bundle in sorted(runs, key=lambda b: timestamp(b["run"]["completed_at"])):
        run = bundle["run"]
        for case in bundle["cases"]:
            packages = sorted(case["resolved"], key=lambda p: (p["role"], p["name"]))
            actual = {(name_key(p["name"], case["language"]), p["role"]): p["version"] for p in packages}
            lock = case["dependency_lock"]
            identity = [case["integration_id"], case["mode"], case["runtime"],
                        [(name_key(p["name"], case["language"]), p["role"], p["version"], p.get("artifact_sha256"))
                         for p in packages],
                        {"source_build": case["source_sha"] if case["mode"] == "source" else None,
                         "lock": {k: lock[k] for k in ("format", "sha256")} if lock else None,
                         "self_test": run["self_test"]}, case["contract"]["suite_sha256"]]
            if not actual or any(actual.get((name_key(p["name"], case["language"]), p["role"])) != p["selected_version"]
                                 for p in case["requested"]):
                identity.append({"unresolved_selection": sorted(
                    (name_key(p["name"], case["language"]), p["role"], p["selected_version"] or p["requirement"])
                    for p in case["requested"])})
            key = digest(identity)
            if key in rows and timestamp(rows[key]["checked_at"]) > timestamp(case["completed_at"]):
                continue
            rows[key] = {"id": key, "integration_id": case["integration_id"], "language": case["language"],
                         "mode": case["mode"], "qualification": targets[case["integration_id"]]["qualification"]["status"],
                         "evidence_qualification": case["qualification"],
                         "status": case["status"], "observed_status": case["status"], "runtime": case["runtime"],
                         "packages": packages, "requested": case["requested"], "discovery": case["discovery"],
                         "source_sha": case["source_sha"], "checked_at": case["completed_at"],
                         "run_url": run["url"], "run_id": run["run_id"], "self_test": run["self_test"],
                         "selector": case["selector"], "suite_sha256": case["contract"]["suite_sha256"],
                         "scenarios": case["contract"]["scenarios"], "dependency_lock": case["dependency_lock"],
                         "failure": case["failure"], "install_command": None}
            row = rows[key]
            # Published pass means an exact reproducible tuple was actually
            # installed. It remains unqualified if the registry says so.
            if case["mode"] == "published" and case["status"] == "passed" and case["language"] == "python":
                row["install_command"] = "python -m pip install " + " ".join(
                    f"'{p['name']}=={p['version']}'" for p in packages)
    health = heartbeat(runs, registry, now)
    for row in rows.values():
        target = targets[row["integration_id"]]
        if not observation_fresh(target, row["mode"], row["selector"], row["runtime"], row["checked_at"], now):
            row["status"] = "stale"
    return {"schema_version": 1, "generated_at": iso(now), "inventory_revision": registry["inventory_revision"],
            "fixture": fixture, "observations": sorted(rows.values(), key=lambda r: (r["integration_id"], r["mode"], r["id"])),
            "known_bad": known_bad_rows(states, registry, now, issues), "health": health, "inventory": [
                {"id": t["id"], "language": t["language"], "distribution": t["distribution"],
                 "role": t["role"], "qualification": t["qualification"], "owner": t["owner"],
                 "declared": [{"name": p["name"], "range": p["declared_range"]} for p in t["upstream"]],
                 "canary_enabled": t["canary"]["enabled"]} for t in registry["integrations"]]}


def render_markdown(index, *, baseline=False):
    lines = ["# Version compatibility", "",
             "Declared support, configured tests and observed execution are different facts. "
             "A dependency range is not proof that every admitted version passed.", ""]
    if index["fixture"]:
        lines += ["> **SYNTHETIC FIXTURE PREVIEW — not customer compatibility evidence.**", ""]
    if baseline:
        lines += ["No observed canary results are committed in this baseline. "
                  "Do not infer compatibility from the inventory below.", ""]
    else:
        lines += [f"Generated: {index['generated_at']}. Exact environments and original results are retained in "
                  "the `compatibility-results` and `compatibility-report` Actions artifacts.", ""]
    lines += ["## Exact observed combinations", "",
              "Only **published / passed** rows identify tested installable releases. "
              "Source rows are unreleased builds. Pilot-pending or unqualified targets remain so even after an individual pass. "
              "Stale, blocked, flaky and quarantined are not green. No live-provider behavior is certified.", ""]
    if not index["observations"]:
        lines += ["No observed combinations yet.", ""]
    for row in index["observations"]:
        lines += [f"### {text(row['integration_id'])} — {row['mode']} / {row['status']}", "",
                  f"- Current qualification: **{row['qualification']}**; evidence-time qualification: **{row['evidence_qualification']}**; "
                  f"last checked: **{row['checked_at']}**; "
                  f"[evidence]({row['run_url']})" + (" (**self-test, not a support claim**)" if row["self_test"] else ""),
                  f"- Runtime/OS: `{text(json.dumps(row['runtime'], sort_keys=True))}`; selector: `{row['selector']}`.",
                  f"- Source SHA: `{row['source_sha']}`; contract: `{row['suite_sha256']}`.",
                  "- Requested selection: " + ", ".join(
                      f"`{text(p['name'])}=={text(p['selected_version'] or 'unresolved')}` ({p['role']})" for p in row["requested"]),
                  "- Exact resolved packages: " + (", ".join(f"`{text(p['name'])}=={text(p['version'])}` ({p['role']})"
                                                            for p in row["packages"]) or "none (installation did not complete)"),
                  f"- Features exercised: {text(', '.join(row['scenarios'])) or 'none'}.",
                  f"- Complete environment lock: `{text(json.dumps(row['dependency_lock']))}`."]
        if row["discovery"]:
            lines += ["- Discovery (absolute latest / eligible after 72-hour cooldown / actually resolved): " + "; ".join(
                f"{text(p['name'])}: {text(p['absolute_latest'])} / {text(p['latest_eligible'])} / {text(p['resolved_version'])}"
                for p in row["discovery"])]
        if row["failure"]:
            lines += [f"- Failure: {text(row['failure']['summary'])}; signature `{row['failure']['signature']}`. "
                      "Do not install this tuple as a workaround. Consult a fresh published pass for this integration, if any."]
        if row["install_command"] and row["status"] == "passed":
            lines += ["", "Direct-version install pins (use the complete evidence lock to reproduce the tested transitive environment):", "",
                      "```sh", row["install_command"], "```"]
        lines += [""]
    if index["known_bad"]:
        lines += ["## Unrecovered known-bad tuples", "",
                  "These durable incident references remain actionable even after an original artifact ages out of discovery or retention. "
                  "They are not fresh observations; age does not imply recovery or erase a released gap.", ""]
        for bad in index["known_bad"]:
            issue = (f"[incident #{bad['issue_number']}](https://github.com/future-agi/traceAI/issues/{bad['issue_number']})"
                     if bad["issue_number"] else "incident write pending (dry-run)")
            lines += [f"### {text(bad['integration_id'])} — {bad['status']} / aged {bad['age_days']} days", "",
                      f"- Exact upstream tuple: `{text(json.dumps(bad['tuple']))}`; {issue}.",
                      f"- Last failure: **{bad['last_failure']}**; signature `{bad['signature']}`; {text(bad['summary'])}."]
            for scope in bad["scopes"]:
                ev = scope["evidence"]
                lines += [f"- {ev['mode']} / `{text(json.dumps(ev['runtime'], sort_keys=True))}`; "
                          f"[retained evidence reference]({ev['url']}); suite `{scope['suite_sha256']}`; "
                          f"lock `{text(json.dumps(ev['dependency_lock']))}`; source `{ev['source_sha']}`."]
            lines += [""]
    lines += ["## Qualification inventory", "",
              "This includes known-broken and unqualified gaps, shared/core packages and separately labelled example checks. "
              "A Go/Maven pin is a baseline, not an inferred minimum. Java/.NET floating qualification is deferred.", "",
              "| Target | Role | Qualification | Declared upstream ranges (not all verified) | Canary |",
              "|---|---|---|---|---|"]
    for target in index["inventory"]:
        declared = "; ".join(f"{p['name']}: {p['range'] if p['range'] is not None else 'undeclared'}" for p in target["declared"])
        lines.append(f"| {text(target['id'])} | {target['role']} | {target['qualification']['status']} | "
                     f"{text(declared, 8000) or 'none'} | {'pilot' if target['canary_enabled'] else 'not enabled'} |")
    if index["health"] and not baseline:
        lines += ["", "## Automation health", ""] + [f"- {text(a['summary'])}" for a in index["health"]]
    lines += ["", "## Operating and release practice", "",
              "- The pilot is Python OpenAI, Anthropic and LangChain. Expand only after a low-noise baseline and measured CI use.",
              "- Main is the default branch. Cron definitions merged only into dev are dormant. "
              "An operator must separately promote trusted canary/reporter implementation to main and observe a real scheduled run. "
              "Branch self-tests and manual dry runs are not proof of active scheduling.",
              "- Missing expected weekday/Sunday evidence is a heartbeat incident after the daily due time plus 36 hours / eight days since the last complete weekly scope; "
              "Saturday alone is not a missed sweep. Weekly completeness requires source floor/latest at the representative "
              "and boundary runtimes plus the published representative consumer; a lone cell cannot clear it. "
              "Missing other cells does not erase a fresh exact observation. Expired exceptions are automation failures.",
              "- Only labelled, schema-valid GitHub Actions bot issues supply reporter state. Public marker copies are ignored; "
              "malformed, conflicting or oversized owned records are isolated as health errors without stopping unrelated incidents. "
              "Persisted state uses bounded lossless compression; failed-test/recovery scope is never truncated.",
              "- Incidents deduplicate by integration + complete upstream tuple + failure signature. "
              "Two fresh, distinct comparable passing runs recover every failed scope. Development-only fixes remain release pending "
              "for published failures, including repeated failures of the unchanged released package. "
              "Pre-execution resolution/install/selection-mismatch failures may recover through complete validated execution of the intended exact tuple/runtime. "
              "Missing uploads require retained immutable planned runtime evidence, never the collector runtime; "
              "executed contract failures still require identical suite and failed-test coverage. "
              "A newer upstream patch does not erase an older bad tuple. Unseen old failures remain actionable even after a reporter outage.",
              "- A package/configuration version bump or new regression test changes the suite hash, not proof of recovery. "
              "For a fixed published release, replay the retained failed suite/configuration from its immutable source SHA "
              "against the new clean installed instrumentation/core selection, twice. Retain original plan/JUnit/lock/invocation evidence; "
              "do not relabel a changed suite with the old digest or remove failed-test/scenario coverage.",
              "- Production and offline artifact ingestion require the shared canonical validator after PR1 absorption, including "
              "per-invocation status and complete frozen resolution evidence. Production fetches registered suite files at the authenticated "
              "source commit, including its authenticated registry, for static hashing/AST checks only; current trusted code validates historical "
              "evidence against that historical registry, while current qualification remains separate. Tested-source code and artifact commands are never imported or executed. "
              "The separately trusted canary collector binds the aggregate to its original plan; the aggregate's copied plan alone is not independent provenance.",
              "- Publication and recovery use the same cadence-aware freshness rule, including persisted source-recovery evidence. "
              "Old daily passes cannot combine with a new pass merely because they are less than eight days old; weekly boundary evidence keeps its own cadence. "
              "Recovery records retain their actual selector/mode/runtime; ambiguous legacy source records use the stricter representative-daily rule.",
              "- `release-notes.json` contains exact support/failure inputs, not an automatically approved support-policy change. "
              "Its historical incident inputs retain unrecovered exclusions and release obligations even without current downloads; "
              "they are explicitly aged references, not fresh observations or proof of a released fix. "
              "Confirm published fix versions, support additions, exclusions and floor/cap/privacy changes before release.",
              "- A completed canary self-test is not a production failure. The workflow_run trigger must authenticate "
              "its run identity and retained plan before incident reconciliation is enabled. Authenticated self-tests skip all writes, "
              "including heartbeat; an unverified trigger also fails closed on writes and retains a diagnostic artifact. "
              "A scheduled production heartbeat remains independent. Running retries wait for completion instead of reusing the previous attempt's aggregate.",
              "- Retained weekly aggregate archives allow at most 4,096 members, 24 MiB compressed and 64 MiB expanded, "
              "including both attempts. Large plan wheelhouses are separate inputs, not reporter archives. "
              "Registered suite files are required for executed evidence; pre-execution failures remain reportable without inventing an executed suite.",
              "- Normal production dry-run artifacts read existing incident and repair-PR context with read-only permissions; "
              "only the separately guarded apply job can write issues. Explicit retained run IDs bypass discovery age limits, "
              "but their actual attempt execution timestamps still govern freshness; recent aggregate upload discovery also retains fresh reruns of old IDs "
              "in subsequent scheduled reports. Unavailable/rejected requested evidence is a health error, never recovered merely by omission.",
              "- Exact environment keys include dependency-lock digests, source-build identity and resolved artifact digests. "
              "Unrecovered known-bad tuples remain in customer outputs as aged incident evidence references after downloads expire; "
              "only validated bot-owned state is accepted, not public marker copies. Health recovery requires type-specific positive evidence.",
              "- Agents receive a human-claimed handoff with a one-open-repair-PR marker guard. "
              "This reporter never starts an agent, invokes vendors, spends money, opens repair PRs, pushes code or merges. "
              "Slack/Linear delivery is disabled; GitHub issues are canonical.",
              "- New integrations need a real upstream SDK contract, exact floor/family/runtime evidence, aligned metadata/guards, "
              "privacy assertions, clean built-consumer checks, no empty/skip success, and a successful scheduled result before qualification.",
              "- Weekly review: new majors, stale/unqualified targets, expiring exceptions, release-pending fixes, maintenance time and runner use.", ""]
    return "\n".join(lines)


def release_notes(index):
    return {"schema_version": 1, "generated_at": index["generated_at"], "fixture": index["fixture"],
            "requires_maintainer_review": True, "inputs": [
                {"integration_id": row["integration_id"], "mode": row["mode"], "status": row["status"],
                 "qualification": row["qualification"], "exact_packages": row["packages"],
                 "checked_at": row["checked_at"], "evidence": row["run_url"],
                 "source_build_unreleased": row["mode"] == "source",
                 "confirm_before_release": ["fix included in an installable instrumentation and core release",
                                           "support additions and exact known-bad exclusions", "floor/cap and privacy changes"]}
                for row in index["observations"]],
            "historical_incidents": [
                {"evidence_kind": "historical_incident", "incident_key": bad["id"],
                 "integration_id": bad["integration_id"], "incident_status": bad["status"],
                 "intended_upstream_tuple": bad["tuple"], "signature": bad["signature"],
                 "summary": bad["summary"], "issue_number": bad["issue_number"],
                 "last_failure": bad["last_failure"], "age_days": bad["age_days"], "scopes": bad["scopes"],
                 "fresh_observation": False, "requires_maintainer_review": True,
                 "published_scope_affected": any(s["evidence"]["mode"] == "published" for s in bad["scopes"]),
                 "confirm_before_release": ["investigate unrecovered exact tuple before claiming compatibility",
                                            "confirm known exclusions and any installable released fix",
                                            "retention expiry is not recovery; references may outlive artifacts"]}
                for bad in index["known_bad"]]}
