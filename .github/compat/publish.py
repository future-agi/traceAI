"""Render observed combinations, not inferred support from dependency ranges."""
from __future__ import annotations

import json
from datetime import timedelta

from incidents import digest, heartbeat, iso, name_key, scope_key, text, timestamp


def build_index(runs, registry, now, *, fixture=False):
    targets = {t["id"]: t for t in registry["integrations"]}
    rows = {}
    for bundle in sorted(runs, key=lambda b: timestamp(b["run"]["completed_at"])):
        run = bundle["run"]
        for case in bundle["cases"]:
            packages = sorted(case["resolved"], key=lambda p: (p["role"], p["name"]))
            actual = {(name_key(p["name"], case["language"]), p["role"]): p["version"] for p in packages}
            identity = [case["integration_id"], case["mode"], case["runtime"],
                        [(name_key(p["name"], case["language"]), p["role"], p["version"]) for p in packages],
                        case["contract"]["suite_sha256"]]
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
    stale = {a["id"] for a in health}
    for row in rows.values():
        cadence = "weekly" if row["mode"] == "published" or row["selector"] in ("floor", "historical") else "daily"
        # Missing *other* weekly cells is an automation-health gap, not proof
        # that this fresh exact published observation is itself stale.
        if (timestamp(row["checked_at"]) < now - timedelta(days=8) or
                (cadence == "daily" and f"heartbeat:{row['integration_id']}:daily" in stale)):
            row["status"] = "stale"
    return {"schema_version": 1, "generated_at": iso(now), "inventory_revision": registry["inventory_revision"],
            "fixture": fixture, "observations": sorted(rows.values(), key=lambda r: (r["integration_id"], r["mode"], r["id"])),
            "health": health, "inventory": [
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
                  f"- Qualification: **{row['qualification']}**; last checked: **{row['checked_at']}**; "
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
            lines += ["", "Exact tested install (retain the full lock from the evidence for transitive reproducibility):", "",
                      "```sh", row["install_command"], "```"]
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
              "- Missing expected weekday/Sunday evidence is a heartbeat incident after the 36-hour daily / eight-day weekly grace; "
              "Saturday alone is not a missed sweep. Weekly completeness requires source floor/latest at the representative "
              "and boundary runtimes plus the published representative consumer; a lone cell cannot clear it. "
              "Missing other cells does not erase a fresh exact observation. Expired exceptions are automation failures.",
              "- Only labelled, schema-valid GitHub Actions bot issues supply reporter state. Public marker copies are ignored; "
              "malformed or conflicting owned records are isolated without stopping unrelated incidents.",
              "- Incidents deduplicate by integration + complete upstream tuple + failure signature. "
              "Two fresh, distinct comparable passing runs recover every failed scope. Development-only fixes remain release pending "
              "for published failures, including repeated failures of the unchanged released package. "
              "Pre-execution resolution/install failures may recover through a complete validated execution of the exact tuple/runtime; "
              "executed contract failures still require identical suite and failed-test coverage. "
              "A newer upstream patch does not erase an older bad tuple. Unseen old failures remain actionable even after a reporter outage.",
              "- `release-notes.json` contains exact support/failure inputs, not an automatically approved support-policy change. "
              "Confirm published fix versions, support additions, exclusions and floor/cap/privacy changes before release.",
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
                 "release_pending": row["mode"] == "source" and row["status"] == "passed",
                 "confirm_before_release": ["fix included in an installable instrumentation and core release",
                                           "support additions and exact known-bad exclusions", "floor/cap and privacy changes"]}
                for row in index["observations"]]}
