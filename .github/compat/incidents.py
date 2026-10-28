"""Pure incident reconciliation. No dependency execution, code pushes or paid APIs."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

MARKER = re.compile(r"<!-- traceai-compat:v1 ([A-Za-z0-9_=-]+) -->")
REPAIR = re.compile(r"<!-- traceai-compat-repair:([0-9a-f]{64}) -->")
LABELS = {
    "compatibility": "5319e7", "compat:agent-ready": "0e8a16",
    "compat:release-pending": "fbca04", "compat:heartbeat": "d93f0b",
    "compat:resolution-install": "b60205", "compat:contract-regression": "b60205",
    "compat:upstream-api-change": "d93f0b", "compat:infra": "c5def5",
}


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def name_key(name, language):
    return re.sub(r"[-_.]+", "-", name).lower() if language == "python" else name


def upstream_tuple(case):
    # A failed resolver may not have actual versions. Keep the selected tuple in
    # a distinct identity; it must never masquerade as a resolved working tuple.
    actual = {(name_key(p["name"], case["language"]), p["role"]): p["version"]
              for p in case["resolved"] if p["role"] in ("upstream", "companion")}
    selected = {(name_key(p["name"], case["language"]), p["role"]): p["selected_version"]
                for p in case["requested"] if p["role"] in ("upstream", "companion")}
    return sorted((name, role, actual.get((name, role)) or version or "unresolved")
                  for (name, role), version in (selected | actual).items())


def scope_key(case, mode=None):
    return digest({"mode": mode or case["mode"], "runtime": case["runtime"],
                   "suite": case["contract"]["suite_sha256"]})


def incident_key(case):
    return digest([case["integration_id"], upstream_tuple(case), case["failure"]["signature"]])


def classify(case):
    kind = case["failure"]["kind"]
    if kind in ("resolution", "install", "requested_resolved_mismatch"):
        return "resolution-install"
    if kind in ("infrastructure", "automation"):
        return "infra"
    # A routing hypothesis, not a claim that the upstream is proven at fault.
    if re.search(r"AttributeError|ImportError|ModuleNotFoundError|unexpected keyword|cannot import|has no attribute",
                 case["failure"]["summary"], re.I):
        return "upstream-api-change"
    return "contract-regression"


def text(value, limit=1500):
    # Artifacts may contain mentions, HTML and Markdown. They are data, not a
    # request to notify arbitrary users or inject a hidden incident marker.
    value = str(value).replace("@", "＠").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("|", "\\|").replace("`", "'")
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", value)[:limit]


def parse_issue(issue):
    matches = MARKER.findall(issue.get("body") or "")
    if len(matches) != 1:
        return None
    try:
        state = json.loads(base64.urlsafe_b64decode(matches[0]))
        if not re.fullmatch(r"[0-9a-f]{64}", state["key"]):
            return None
        if state.get("version") != 1 or state.get("kind") not in ("compatibility", "health"):
            return None
        return state
    except (ValueError, KeyError, TypeError):
        return None


def evidence(case, run):
    return {"run_id": run["run_id"], "url": run["url"], "at": case["completed_at"],
            "case_id": case["case_id"], "mode": case["mode"], "runtime": case["runtime"],
            "source_sha": case["source_sha"], "resolved": case["resolved"],
            "dependency_lock": case["dependency_lock"], "reproduction": case["reproduction"],
            "test_ids": case["failure"]["test_ids"] if case["failure"] else [],
            "scenarios": case["contract"]["scenarios"]}


def last_known_good(case, runs, before):
    candidates = [(c, r["run"]) for r in runs for c in r["cases"]
                  if not r["run"]["self_test"] and c["status"] == "passed"
                  and c["integration_id"] == case["integration_id"]
                  and scope_key(c) == scope_key(case) and timestamp(c["completed_at"]) < timestamp(before)]
    if not candidates:
        return None
    c, run = max(candidates, key=lambda pair: timestamp(pair[0]["completed_at"]))
    return evidence(c, run)


def body_for(state):
    encoded = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).decode()
    lines = [f"<!-- traceai-compat:v1 {encoded} -->", "## Compatibility incident", "",
             f"State: **{text(state['status'])}**. Owner: {text(state.get('owner', 'nik13'))}.",
             f"First seen: {state['first_seen']}; last evidence: {state['last_seen']}."]
    if state["kind"] == "health":
        lines += ["", text(state["summary"]), "", "Check the canary/report heartbeat and retained artifacts. No compatibility pass is implied."]
    else:
        lines += ["", f"Integration: **{text(state['integration_id'])}**",
                  f"Exact upstream tuple: `{text(json.dumps(state['tuple']))}`",
                  f"Failure signature: `{state['signature']}`",
                  f"Routing: **{state['classification']}** (provisional; confirm with the last-known-good rerun).",
                  "", text(state["summary"]), "", "### Failed scope / latest evidence"]
        for scope in state["scopes"].values():
            bad = scope["failure"]
            lines += [f"- {bad['mode']} / {text(json.dumps(bad['runtime']))}; source `{bad['source_sha']}`; "
                      f"[run]({bad['url']}); case `{bad['case_id']}`; comparable passing runs: {len(scope['passes'])}.",
                      f"  Resolved: `{text(json.dumps(bad['resolved']), 3000)}`",
                      f"  Lock: `{text(json.dumps(bad['dependency_lock']))}`",
                      f"  Tests: `{text(json.dumps(bad['test_ids']))}`",
                      f"  Reproduce (inspect argv; never execute artifact commands automatically): `{text(json.dumps(bad['reproduction']))}`"]
        good = state.get("last_known_good")
        lines += ["", "### Last-known-good rerun hint"]
        if good:
            lines += [f"Replay the retained dependency lock from [run]({good['url']}) on the failing source SHA, "
                      "then rerun the failed exact tuple. Do not resolve a fresh latest.",
                      f"- Lock: `{text(json.dumps(good['dependency_lock']))}`",
                      f"- Resolved: `{text(json.dumps(good['resolved']), 3000)}`"]
        else:
            lines += ["No comparable passing snapshot was retained. Establish a locked baseline before attributing the failure upstream."]
        lines += ["", "### Agent remediation handoff", "",
                  f"Before starting, search ALL open repair PR bodies for `traceai-compat-repair:{state['key']}`.",
                  "If any exists, update it; do not open another. Claim this issue with the maintainer before work.",
                  f"A repair PR must include `<!-- traceai-compat-repair:{state['key']} -->` and reference this incident.",
                  "Reproduce the captured plan, compare the locked baseline, add a real-SDK regression contract, "
                  "and verify floor/affected family/latest plus clean released consumers where applicable.",
                  "Record release-note input: exact upstream tuple, instrumentation/core fix versions, "
                  "known exclusions, support/privacy changes and whether a release is pending.",
                  "Do not merge, spend money, invoke providers, push automatically, narrow support or add exceptions to obtain green.",
                  "Recovery needs two distinct fresh comparable runs for every failed mode/runtime/suite. "
                  "A source fix cannot close a published-package failure. A different upstream patch cannot erase this tuple."]
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 60000:
        raise ValueError("incident body exceeds safe GitHub limit")
    return rendered


def reconcile(runs, issues, registry, now, health=()):
    """Return deterministic issue upserts and repair handoffs; never perform writes."""
    targets = {i["id"]: i for i in registry["integrations"]}
    states, existing = {}, {}
    for issue in issues:
        if "pull_request" in issue:
            continue
        state = parse_issue(issue)
        if not state:
            continue
        key = state["key"]
        if key in existing:
            raise ValueError("duplicate incident markers require maintainer reconciliation")
        states[key], existing[key] = state, issue
    original = {key: json.dumps(value, sort_keys=True) for key, value in states.items()}
    # Process older evidence first, but never replay older evidence over persisted
    # newer failures. Per-scope watermarks keep retries and repeated reports idempotent.
    observations = sorted([(c, r["run"]) for r in runs if not r["run"]["self_test"] for c in r["cases"]],
                          key=lambda pair: (timestamp(pair[0]["completed_at"]), pair[1]["run_id"], pair[0]["attempt"]))
    fresh_cutoff = now - timedelta(days=8)
    for case, run in observations:
        at = timestamp(case["completed_at"])
        if at > now or at < fresh_cutoff:
            continue
        failed = case["status"] in ("failed", "flaky", "quarantined", "blocked") and case["failure"] is not None
        if failed:
            key = incident_key(case)
            state = states.setdefault(key, {
                "version": 1, "key": key, "kind": "compatibility", "integration_id": case["integration_id"],
                "tuple": upstream_tuple(case), "signature": case["failure"]["signature"],
                "owner": targets[case["integration_id"]]["owner"], "classification": classify(case),
                "summary": case["failure"]["summary"][:1500], "status": "open", "scopes": {},
                "first_seen": case["completed_at"], "last_seen": case["completed_at"],
                "last_known_good": last_known_good(case, runs, case["completed_at"]),
            })
            sk = scope_key(case)
            scope = state["scopes"].get(sk)
            observation = [run["run_id"], run["run_attempt"], case["attempt"], case["completed_at"]]
            if scope and (at < timestamp(scope["watermark"][3]) or observation == scope["watermark"]):
                continue
            state["scopes"][sk] = {"failure": evidence(case, run), "passes": [], "source_passes": [],
                                   "watermark": observation, "suite": case["contract"]["suite_sha256"]}
            state["status"] = "open"
            state["last_seen"] = max(state["last_seen"], case["completed_at"], key=timestamp)
        elif case["status"] == "passed":
            for state in states.values():
                if state["kind"] != "compatibility" or state["integration_id"] != case["integration_id"]:
                    continue
                if state["tuple"] != [list(t) for t in upstream_tuple(case)] and state["tuple"] != upstream_tuple(case):
                    continue
                for sk, scope in state["scopes"].items():
                    bad = scope["failure"]
                    if at <= timestamp(scope["watermark"][3]) or scope["suite"] != case["contract"]["suite_sha256"]:
                        continue
                    if bad["runtime"] != case["runtime"]:
                        continue
                    executed = {t["test_id"] for t in case["contract"]["tests"] if t["outcome"] == "passed"}
                    if not set(bad["test_ids"]).issubset(executed) or not set(bad["scenarios"]).issubset(case["contract"]["scenarios"]):
                        continue
                    comparable = sk == scope_key(case)
                    source_recovery = bad["mode"] == "published" and case["mode"] == "source"
                    if not (comparable or source_recovery):
                        continue
                    field = "passes" if comparable else "source_passes"
                    if run["run_id"] not in [p["run_id"] for p in scope[field]]:
                        scope[field] = (scope[field] + [evidence(case, run)])[-2:]
                    scope["watermark"] = [run["run_id"], run["run_attempt"], case["attempt"], case["completed_at"]]
                    state["last_seen"] = max(state["last_seen"], case["completed_at"], key=timestamp)
    for state in states.values():
        if state["kind"] == "compatibility":
            scopes = list(state["scopes"].values())
            # Closed historical incidents stay closed when evidence ages out;
            # heartbeat owns freshness. Open incidents cannot combine a months-
            # old pass with a new pass to satisfy two *fresh* recovery runs.
            if state["status"] != "recovered":
                for scope in scopes:
                    for field in ("passes", "source_passes"):
                        scope[field] = [p for p in scope[field] if timestamp(p["at"]) >= fresh_cutoff]
            if all(len(s["passes"]) >= 2 for s in scopes):
                state["status"] = "recovered"
            elif any(s["failure"]["mode"] == "published" and len(s["source_passes"]) >= 2
                     and len(s["passes"]) < 2 for s in scopes):
                state["status"] = "release_pending"
            else:
                state["status"] = "open"
    active_health = set()
    for alert in health:
        key = digest(["health", alert["id"]])
        active_health.add(key)
        state = states.setdefault(key, {"version": 1, "key": key, "kind": "health", "first_seen": iso(now),
                                        "owner": "nik13"})
        # Do not generate a write just to bump a heartbeat timestamp every day.
        if state.get("status") != "open" or state.get("summary") != alert["summary"]:
            state.update(status="open", summary=alert["summary"], last_seen=iso(now))
    for key, state in states.items():
        if state["kind"] == "health" and key not in active_health and state["status"] != "recovered":
            state.update(status="recovered", last_seen=iso(now))
    actions, handoffs = [], []
    for key, state in sorted(states.items()):
        issue = existing.get(key)
        desired = "closed" if state["status"] == "recovered" else "open"
        # A first report may replay a fully recovered historical incident. Do
        # not open a new issue just to announce an already completed recovery.
        if desired == "closed" and issue is None:
            continue
        labels = ["compatibility"]
        labels += ["compat:heartbeat"] if state["kind"] == "health" else ["compat:" + state["classification"]]
        if state["kind"] == "compatibility" and desired == "open":
            labels += ["compat:release-pending" if state["status"] == "release_pending" else "compat:agent-ready"]
            prs = [i["number"] for i in issues if "pull_request" in i and i["state"] == "open"
                   and key in REPAIR.findall(i.get("body") or "")]
            handoffs.append({"incident_key": key, "issue_number": issue["number"] if issue else None,
                             "guard": "blocked_existing_pr" if prs else "maintainer_claim_required",
                             "existing_prs": prs, "required_pr_marker": f"<!-- traceai-compat-repair:{key} -->",
                             "automatic_execution": False})
        issue_labels = {l["name"] if isinstance(l, dict) else l for l in (issue or {}).get("labels", [])}
        unchanged = original.get(key) == json.dumps(state, sort_keys=True)
        if unchanged and issue and issue["state"] == desired and set(labels).issubset(issue_labels):
            continue
        suffix = state.get("integration_id", "automation heartbeat")
        actions.append({"key": key, "number": issue["number"] if issue else None, "state": desired,
                        "title": f"[compatibility] {suffix}: {state.get('classification', 'health')} ({key[:12]})",
                        "body": body_for(state), "labels": labels,
                        "notification": "recovery" if desired == "closed" else ("new" if not issue else "evidence_updated")})
    return {"actions": actions, "handoffs": handoffs, "states": list(states.values())}


def heartbeat(runs, registry, now):
    """Due-aware calendar: Sun weekly, Mon-Fri stable, Saturday has no sweep."""
    alerts = []
    for target in registry["integrations"]:
        for exc in target["exceptions"]:
            if datetime.fromisoformat(exc["expires"]).date() < now.date():
                alerts.append({"id": "exception:" + exc["id"],
                               "summary": f"Expired compatibility exception {exc['id']} for {target['id']}; repair or explicitly review it."})
        if not target["canary"]["enabled"]:
            continue
        cases = [c for r in runs if not r["run"]["self_test"] for c in r["cases"]
                 if c["integration_id"] == target["id"] and c["status"] not in ("stale", "not_tested")]
        for cadence, grace in (("daily", timedelta(hours=36)), ("weekly", timedelta(days=8))):
            eligible = [c for c in cases if (c["mode"] == "source" and c["selector"] == "latest_eligible"
                        if cadence == "daily" else c["mode"] == "published" or c["selector"] in ("floor", "historical"))]
            last = max((timestamp(c["completed_at"]) for c in eligible), default=None)
            # Find a scheduled due time whose deadline passed and which has not
            # been satisfied by a later complete observation. A Saturday gap alone
            # cannot create an alert after a complete Friday run.
            missed = False
            for days in range(18):
                due = (now - timedelta(days=days)).replace(hour=3, minute=17, second=0, microsecond=0)
                scheduled = due.weekday() in (0, 1, 2, 3, 4, 6) if cadence == "daily" else due.weekday() == 6
                if scheduled and due + grace <= now and (last is None or last < due):
                    missed = True
                    break
            if missed:
                alerts.append({"id": f"heartbeat:{target['id']}:{cadence}",
                               "summary": f"Stale/missing {cadence} canary evidence for {target['id']} "
                               f"(last complete: {iso(last) if last else 'none'}). Check the default-branch scheduler; "
                               "dev-only cron is dormant. No passing status may be inferred."})
    return alerts
