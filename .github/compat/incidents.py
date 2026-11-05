"""Pure incident reconciliation. No dependency execution, code pushes or paid APIs."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
import zlib

MAX_STATE = 2 * 1024 * 1024


def decode_json(raw, limit=8 * 1024 * 1024):
    """Bound untrusted JSON before either the decoder or schema can recurse."""
    if len(raw) > limit:
        raise ValueError("JSON exceeds size limit")
    value = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    depth, quoted, escaped = 0, False, False
    for char in value:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON exceeds nesting limit")
        elif char in "]}":
            depth -= 1
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    try:
        decoded = json.loads(value, object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
    except RecursionError:
        raise ValueError("JSON exceeds decoder nesting limit") from None
    # ASCII JSON escapes can decode to lone UTF-16 surrogates even when the wire
    # bytes are valid UTF-8. Reject them in keys and values, including nested
    # arrays and persisted issue state, before they can reach final output.
    stack = [decoded]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if re.search(r"[\ud800-\udfff]", item):
                raise ValueError("JSON contains an invalid Unicode scalar")
        elif isinstance(item, dict):
            stack.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return decoded

REPORTER_USER = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}

MARKER = re.compile(r"<!-- traceai-compat:v([12]) ([A-Za-z0-9_=-]+) -->")
REPAIR = re.compile(r"<!-- traceai-compat-repair:([0-9a-f]{64}) -->")
LABELS = {
    "compatibility": "5319e7", "compat:agent-ready": "0e8a16",
    "compat:release-pending": "fbca04", "compat:heartbeat": "d93f0b",
    "compat:resolution-install": "b60205", "compat:contract-regression": "b60205",
    "compat:upstream-api-change": "d93f0b", "compat:infra": "c5def5",
}


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return dt.astimezone(timezone.utc)
    except OverflowError:
        # ISO-shaped values can cross datetime's year boundary when normalized.
        # This is malformed evidence, not a process-wide reporter failure.
        raise ValueError("timestamp outside supported UTC range") from None


def observation_fresh(target, mode, selector, runtime, checked_at, now):
    """One cadence rule for customer observations and both recovery stores.

    A representative source/latest cell is due Mon-Fri and Sunday, with 36h
    grace. Other configured scopes have an eight-day maximum age. Missing other
    cells belongs to heartbeat, not to the freshness of this observation.
    """
    checked = timestamp(checked_at)
    if checked > now or checked <= now - timedelta(days=8):
        return False
    representative = target["runtimes"]["representative"][:1]
    daily = mode == "source" and selector == "latest_eligible" and any(
        runtime["version"] == v or runtime["version"].startswith(v + ".") for v in representative)
    if daily:
        for days in range(18):
            due = (now - timedelta(days=days)).replace(hour=3, minute=17, second=0, microsecond=0)
            if due.weekday() in (0, 1, 2, 3, 4, 6) and due + timedelta(hours=36) <= now and checked < due:
                return False
    return True


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
    if case["failure"] and case["failure"]["kind"] == "requested_resolved_mismatch":
        # This is an automation/installation failure of the *intended* selection,
        # not a compatibility defect of the accidentally installed distribution.
        return sorted((name, role, version or "unresolved") for (name, role), version in selected.items())
    return sorted((name, role, actual.get((name, role)) or version or "unresolved")
                  for (name, role), version in (selected | actual).items())


def scope_key(case, mode=None):
    return digest({"mode": mode or case["mode"], "runtime": case["runtime"],
                   "suite": case["contract"]["suite_sha256"]})


def incident_key(case):
    return digest([case["integration_id"], upstream_tuple(case), case["failure"]["signature"]])


def covered_failed_ids(failure, executed_ids):
    covered = set(executed_ids)
    # Canonical pytest collection errors can identify a module, not an executed
    # node. Only collection failures have this interpretation; assertions retain
    # exact node/parameter matching. The :: boundary prevents filename prefixes
    # or sibling modules from accidentally satisfying coverage. Set operations
    # keep large retained suites linear rather than comparing every node pair.
    if failure.get("failure_kind", failure.get("kind")) == "collection":
        covered.update(node.split("::", 1)[0] for node in list(covered)
                       if "::" in node and node.split("::", 1)[0].endswith(".py"))
    return set(failure["test_ids"]) & covered


def complete_pass(case):
    # Production callers have already authenticated full collect-only evidence,
    # invocation success, JUnit and the retained suite through the canonical
    # validator. Keep these in-memory checks too; a partial/skipped execution
    # cannot supply recovery merely because its status string says passed.
    contract = case["contract"]
    tests = contract["tests"]
    return (bool(tests) and contract["collected"] == contract["executed"] == len(tests)
            and not contract["unexpected_skips"] and case["failure"] is None
            and len({t["test_id"] for t in tests}) == len(tests)
            and all(t["outcome"] == "passed" for t in tests))


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
    # Defense in depth for direct in-memory callers; decoded untrusted JSON is
    # rejected instead. A display string must always be writable as UTF-8.
    value = re.sub(r"[\ud800-\udfff]", "�", str(value))
    value = value.replace("@", "＠").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("|", "\\|").replace("`", "'")
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", value)[:limit]


@lru_cache(maxsize=1)
def state_validator():
    """Validate every persisted field before it can influence reconciliation."""
    from jsonschema import Draft202012Validator, FormatChecker
    abi = json.loads(Path(__file__).with_name("results.schema.json").read_text())
    case = abi["$defs"]["case"]["properties"]
    string = {"type": "string", "minLength": 1}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    date = {"type": "string", "format": "date-time"}
    def obj(properties, required=None):
        return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
                "additionalProperties": False}
    evidence_properties = {key: case[key] for key in ("case_id", "mode", "runtime", "source_sha", "resolved",
                                                       "dependency_lock", "reproduction")}
    evidence_properties.update(run_id={"type": "string", "pattern": "^[1-9][0-9]*$"}, url=string, at=date,
                               test_ids={"type": "array", "uniqueItems": True, "items": string},
                               scenarios=case["contract"]["properties"]["scenarios"])
    evidence_required = list(evidence_properties)
    evidence_properties.update(selector=case["selector"],
                               failure_kind=case["failure"]["anyOf"][0]["properties"]["kind"],
                               pre_execution={"type": "boolean"},
                               planned_runtime={"type": "string", "minLength": 1, "maxLength": 128},
                               planned_runtime_identity=case["runtime"])
    ev = obj(evidence_properties, evidence_required)
    watermark = {"type": "array", "prefixItems": [evidence_properties["run_id"], {"type": "integer", "minimum": 1},
                    {"type": "integer", "minimum": 1, "maximum": 2}, date], "minItems": 4, "maxItems": 4}
    scope_properties = {"failure": ev, "passes": {"type": "array", "maxItems": 2, "items": ev},
                        "source_passes": {"type": "array", "maxItems": 2, "items": ev},
                        "watermark": watermark, "suite": sha,
                        "source_watermark": {"anyOf": [watermark, {"type": "null"}]}}
    properties = {"version": {"const": 1}, "key": sha, "kind": {"enum": ["compatibility", "health"]},
                  "owner": string, "status": {"enum": ["open", "release_pending", "recovered"]},
                  "first_seen": date, "last_seen": date, "summary": string, "health_id": string,
                  "integration_id": string, "signature": sha,
                  "classification": {"enum": ["resolution-install", "infra", "upstream-api-change", "contract-regression"]},
                  "tuple": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {
                      "type": "array", "prefixItems": [string, {"enum": ["upstream", "companion"]}, string],
                      "minItems": 3, "maxItems": 3}},
                  "last_known_good": {"anyOf": [ev, {"type": "null"}]},
                  "scopes": {"type": "object", "minProperties": 1, "propertyNames": sha,
                             "additionalProperties": obj(scope_properties, [k for k in scope_properties if k != "source_watermark"])}}
    required = ["version", "key", "kind", "owner", "status", "first_seen", "last_seen", "summary"]
    schema = obj(properties, required)
    schema["allOf"] = [{"if": {"properties": {"kind": {"const": "compatibility"}}},
                        "then": {"required": ["integration_id", "signature", "classification", "tuple", "last_known_good", "scopes"]}}]
    return Draft202012Validator(schema, format_checker=FormatChecker())


def reporter_owned(issue):
    user = issue.get("user") or {}
    labels = {label.get("name") if isinstance(label, dict) else label for label in issue.get("labels", [])}
    return (isinstance(user, dict) and all(user.get(key) == value for key, value in REPORTER_USER.items())
            and "compatibility" in labels and "pull_request" not in issue)


def parse_issue(issue):
    body = issue.get("body") or ""
    if not isinstance(body, str) or len(body) > 65000:
        return None
    matches = MARKER.findall(body)
    if len(matches) != 1:
        return None
    try:
        version, encoded = matches[0]
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if version == "2":
            decoder = zlib.decompressobj()
            raw = decoder.decompress(raw, MAX_STATE + 1)
            if len(raw) > MAX_STATE or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                return None
        state = decode_json(raw, MAX_STATE)
        if not state_validator().is_valid(state) or timestamp(state["first_seen"]) > timestamp(state["last_seen"]):
            return None
        if state["kind"] == "health" and "health_id" in state and state["key"] != digest(["health", state["health_id"]]):
            return None
        if state["kind"] == "compatibility":
            if state["key"] != digest([state["integration_id"], state["tuple"], state["signature"]]):
                return None
            if state["last_known_good"]:
                timestamp(state["last_known_good"]["at"])
            for key, scope in state["scopes"].items():
                timestamp(scope["watermark"][3])
                if scope.get("source_watermark"):
                    timestamp(scope["source_watermark"][3])
                bad = scope["failure"]
                if key != digest({"mode": bad["mode"], "runtime": bad["runtime"], "suite": scope["suite"]}):
                    return None
                if bad.get("pre_execution") and (bad.get("failure_kind") not in
                        ("resolution", "install", "requested_resolved_mismatch", "automation") or bad["test_ids"]):
                    return None
                if bad.get("pre_execution") and bad.get("failure_kind") == "automation" and "planned_runtime" not in bad:
                    return None
                for ev in [bad, *scope["passes"], *scope["source_passes"]]:
                    timestamp(ev["at"])
                    if ev["url"] != f"https://github.com/future-agi/traceAI/actions/runs/{ev['run_id']}":
                        return None
        return state
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, zlib.error):
        return None


def evidence(case, run, selected=None):
    ev = {"run_id": run["run_id"], "url": run["url"], "at": case["completed_at"],
          "case_id": case["case_id"], "mode": case["mode"], "selector": case["selector"], "runtime": case["runtime"],
          "source_sha": case["source_sha"], "resolved": case["resolved"],
          "dependency_lock": case["dependency_lock"], "reproduction": case["reproduction"],
          "test_ids": case["failure"]["test_ids"] if case["failure"] else [],
          "scenarios": case["contract"]["scenarios"]}
    if case["failure"]:
        ev["failure_kind"] = case["failure"]["kind"]
        empty = (not case["contract"]["tests"] and not case["contract"]["junit_paths"]
                 and case["contract"]["executed"] == 0 and not ev["test_ids"])
        missing = (case["failure"]["kind"] == "automation" and not case["resolved"]
                   and case["dependency_lock"] is None and selected is not None)
        ev["pre_execution"] = empty and (missing or case["failure"]["kind"] in
                                                     ("resolution", "install", "requested_resolved_mismatch"))
        if missing and empty:
            # Only the separately validated immutable plan can supply a missing
            # job's execution scope. Collector runtime remains an observation,
            # not a fabricated runner runtime. No plan => no automatic recovery.
            ev["planned_runtime"] = selected["runtime"]
            identity = selected.get("runtime_identity")
            if identity:
                ev["planned_runtime_identity"] = {k: identity[k] for k in ("version", "implementation", "os", "arch")}
    return ev


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
    # Lossless bounded v2 encoding deduplicates repeated suite IDs and evidence.
    # v1 records remain readable; never truncate failed-test or recovery scope.
    raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_STATE:
        raise ValueError("incident state exceeds safe expanded limit")
    encoded = base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode()
    lines = [f"<!-- traceai-compat:v2 {encoded} -->", "## Compatibility incident", "",
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
                      f"  Planned runtime (missing uploads only): `{text(json.dumps(bad.get('planned_runtime_identity') or bad.get('planned_runtime')))}`",
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
                  "For a released fix, retain the failed suite/configuration source SHA and run that suite against the new clean installed release twice. "
                  "Changed test/configuration bytes cannot borrow the old suite digest; preserve failed IDs/scenarios and original invocation evidence. "
                  "A source fix cannot close a published-package failure. A different upstream patch cannot erase this tuple."]
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 60000:
        raise ValueError("incident body exceeds safe GitHub limit")
    return rendered


def health_recovered(state, runs, issues, registry, now, states):
    """Omission is not evidence. Each health type has a positive recovery test.

    Legacy records lack health_id. Compare their stable digest to candidate
    identifiers we can actually prove; otherwise leave them open for review.
    """
    def matches(ident):
        return state["key"] == digest(["health", ident])
    for bundle in runs:
        if bundle["run"]["self_test"]:
            continue
        for prefix in ("requested-run:", "invalid-run:"):
            if matches(prefix + bundle["run"]["run_id"]):
                return True  # This exact run has now passed trusted validation.
    stale = {a["id"] for a in heartbeat(runs, registry, now)}
    for target in registry["integrations"]:
        if not target["canary"]["enabled"]:
            continue
        for cadence in ("daily", "weekly"):
            ident = f"heartbeat:{target['id']}:{cadence}"
            observed = any(not r["run"]["self_test"] and any(c["integration_id"] == target["id"] for c in r["cases"])
                           for r in runs)
            if matches(ident) and observed and ident not in stale:
                return True
    ident = state.get("health_id", "")
    if ident.startswith("exception:"):
        exc_id = ident.removeprefix("exception:")
        exceptions = [e for t in registry["integrations"] for e in t["exceptions"] if e["id"] == exc_id]
        return not exceptions or all(datetime.fromisoformat(e["expires"]).date() > now.date() for e in exceptions)
    for issue in issues:
        if not reporter_owned(issue):
            continue
        parsed = parse_issue(issue)
        if matches(f"issue-state:{issue['number']}") and parsed is not None:
            return True
    for candidate in states.values():
        key = candidate["key"]
        if matches("issue-conflict:" + key):
            # Reconciliation retains this key only when exactly one valid record
            # exists; conflicting/malformed records are isolated before this test.
            return True
        if matches("incident-size:" + key):
            try:
                body_for(candidate)
                return True
            except ValueError:
                return False
    return False


def reconcile(runs, issues, registry, now, health=(), *, plans=None):
    """Return deterministic issue upserts and repair handoffs; never perform writes."""
    targets = {i["id"]: i for i in registry["integrations"]}
    states, existing, isolated = {}, {}, set()
    record_health = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        # Public issue bodies and labels are attacker-controlled. Only the fixed
        # GitHub Actions bot's issues may supply persisted reporter state.
        if not reporter_owned(issue):
            continue
        state = parse_issue(issue)
        if not state or (state["kind"] == "compatibility" and state["integration_id"] not in targets):
            record_health.append({"id": f"issue-state:{issue['number']}",
                                  "summary": f"Malformed reporter-owned incident #{issue['number']}; isolated for maintainer review."})
            continue
        key = state["key"]
        if key in existing or key in isolated:
            isolated.add(key)
            states.pop(key, None)
            record_health.append({"id": "issue-conflict:" + key,
                                  "summary": f"Conflicting reporter-owned incident records for {key}; only this key is paused for maintainer reconciliation."})
            continue
        states[key], existing[key] = state, issue
    original = {key: json.dumps(value, sort_keys=True) for key, value in states.items()}
    # Process older evidence first, but never replay older evidence over persisted
    # newer failures. Per-scope watermarks keep retries and repeated reports idempotent.
    observations = sorted([(c, r["run"]) for r in runs if not r["run"]["self_test"] for c in r["cases"]],
                          key=lambda pair: (timestamp(pair[0]["completed_at"]), pair[1]["run_id"], pair[0]["attempt"]))
    def fresh(ev, target):
        # Legacy persisted evidence has no selector. Do not assume weekly grace
        # for an ambiguous representative source pass; use the stricter daily
        # rule. Newly written evidence retains its actual selector independently
        # of the failed scope (source recovery may differ from published scope).
        return observation_fresh(target, ev["mode"], ev.get("selector", "latest_eligible"),
                                 ev["runtime"], ev["at"], now)
    for case, run in observations:
        at = timestamp(case["completed_at"])
        if at > now:
            continue
        failed = case["status"] in ("failed", "flaky", "quarantined", "blocked") and case["failure"] is not None
        if failed:
            observation = [run["run_id"], run["run_attempt"], case["attempt"], case["completed_at"]]
            if case["mode"] == "source":
                # A changed diagnostic creates a distinct incident, but cannot
                # leave older source passes certifying an affected published
                # scope. Invalidate across signatures while retaining each
                # original failure and its independent incident identity.
                for prior in states.values():
                    if (prior["kind"] != "compatibility" or prior["integration_id"] != case["integration_id"]
                            or digest(prior["tuple"]) != digest(upstream_tuple(case))):
                        continue
                    for other in prior["scopes"].values():
                        bad = other["failure"]
                        if (bad["mode"] != "published" or bad["runtime"] != case["runtime"]
                                or other["suite"] != case["contract"]["suite_sha256"]):
                            continue
                        affected = (covered_failed_ids(bad, case["failure"]["test_ids"])
                                    or covered_failed_ids(case["failure"], bad["test_ids"]))
                        source_mark = other.get("source_watermark")
                        source_times = [timestamp(p["at"]) for p in other["source_passes"]]
                        if source_mark:
                            source_times.append(timestamp(source_mark[3]))
                        # Legacy valid state may retain passes without the newer
                        # optional watermark. Their timestamps still fence replay.
                        if affected and (not source_times or at >= max(source_times)):
                            other["source_passes"] = []
                            other["source_watermark"] = observation
                            prior["last_seen"] = max(prior["last_seen"], case["completed_at"], key=timestamp)
            key = incident_key(case)
            if key in isolated:
                continue
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
            if scope and (at < timestamp(scope["watermark"][3]) or observation == scope["watermark"]):
                continue
            selected = next((c for c in (plans or {}).get(run["run_id"], {}).get("cases", [])
                             if c["case_id"] == case["case_id"]), None)
            state["scopes"][sk] = {"failure": evidence(case, run, selected), "passes": [],
                                   "source_passes": scope["source_passes"] if scope and case["mode"] == "published" else [],
                                   "source_watermark": scope.get("source_watermark") if scope else None,
                                   "watermark": observation, "suite": case["contract"]["suite_sha256"]}
            state["status"] = "open"
            state["last_seen"] = max(state["last_seen"], case["completed_at"], key=timestamp)
        elif case["status"] == "passed" and complete_pass(case) and fresh(evidence(case, run), targets[case["integration_id"]]):
            for state in states.values():
                if state["kind"] != "compatibility" or state["integration_id"] != case["integration_id"]:
                    continue
                if state["tuple"] != [list(t) for t in upstream_tuple(case)] and state["tuple"] != upstream_tuple(case):
                    continue
                for sk, scope in state["scopes"].items():
                    bad = scope["failure"]
                    # Resolution/install failed before a suite existed. A full
                    # validated pass of that exact tuple/runtime can recover it;
                    # executed contract failures still require the identical suite.
                    if not bad.get("pre_execution") and scope["suite"] != case["contract"]["suite_sha256"]:
                        continue
                    if bad.get("pre_execution") and bad.get("failure_kind") == "automation":
                        # Immutable planned identity wins, never collector Python.
                        planned = bad["planned_runtime"]
                        identity = bad.get("planned_runtime_identity")
                        if (identity and identity != case["runtime"]) or not (
                                case["runtime"]["version"] == planned or case["runtime"]["version"].startswith(planned + ".")):
                            continue
                    elif bad["runtime"] != case["runtime"]:
                        continue
                    executed = {t["test_id"] for t in case["contract"]["tests"] if t["outcome"] == "passed"}
                    if (covered_failed_ids(bad, executed) != set(bad["test_ids"])
                            or not set(bad["scenarios"]).issubset(case["contract"]["scenarios"])):
                        continue
                    comparable = bad["mode"] == case["mode"]
                    source_recovery = bad["mode"] == "published" and case["mode"] == "source"
                    if not (comparable or source_recovery):
                        continue
                    field = "passes" if comparable else "source_passes"
                    watermark_field = "watermark" if comparable else "source_watermark"
                    watermark = scope.get(watermark_field)
                    if at <= timestamp(bad["at"]) or (watermark and at <= timestamp(watermark[3])):
                        continue
                    if run["run_id"] not in [p["run_id"] for p in scope[field]]:
                        scope[field] = (scope[field] + [evidence(case, run)])[-2:]
                    scope[watermark_field] = [run["run_id"], run["run_attempt"], case["attempt"], case["completed_at"]]
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
                        scope[field] = [p for p in scope[field] if fresh(p, targets[state["integration_id"]])]
            if all(len(s["passes"]) >= 2 for s in scopes):
                state["status"] = "recovered"
            elif all(len(s["passes"]) >= 2 or (s["failure"]["mode"] == "published" and len(s["source_passes"]) >= 2)
                     for s in scopes):
                state["status"] = "release_pending"
            else:
                state["status"] = "open"
    actions, handoffs = [], []

    def queue(state):
        key = state["key"]
        issue = existing.get(key)
        desired = "closed" if state["status"] == "recovered" else "open"
        # Never open an issue just to announce an already recovered history.
        if desired == "closed" and issue is None:
            return
        try:
            body = body_for(state)
        except ValueError:
            # A pathological incident is isolated, not silently truncated or
            # allowed to prevent every other integration's output/action.
            record_health.append({"id": "incident-size:" + key,
                                  "summary": f"Incident {key} exceeds safe persisted/body limits; this key's writes and handoff are paused. "
                                             "Inspect retained compatibility-results for its complete failed/recovery scope."})
            return
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
            return
        suffix = state.get("integration_id", "automation heartbeat")
        actions.append({"key": key, "number": issue["number"] if issue else None, "state": desired,
                        "title": f"[compatibility] {suffix}: {state.get('classification', 'health')} ({key[:12]})",
                        "body": body, "labels": labels,
                        "notification": "recovery" if desired == "closed" else ("new" if not issue else "evidence_updated")})

    for key, state in sorted(states.items()):
        if state["kind"] == "compatibility":
            queue(state)
    active_health = set()
    for alert in list(health) + record_health:
        key = digest(["health", alert["id"]])
        active_health.add(key)
        state = states.setdefault(key, {"version": 1, "key": key, "kind": "health", "first_seen": iso(now), "owner": "nik13"})
        state["health_id"] = alert["id"]
        # Do not generate a write just to bump a heartbeat timestamp every day.
        if state.get("status") != "open" or state.get("summary") != alert["summary"]:
            state.update(status="open", summary=alert["summary"], last_seen=iso(now))
    for key, state in sorted(states.items()):
        if state["kind"] == "health":
            if (key not in active_health and state["status"] != "recovered"
                    and health_recovered(state, runs, issues, registry, now, states)):
                state.update(status="recovered", last_seen=iso(now))
            queue(state)
    return {"actions": actions, "handoffs": handoffs, "states": list(states.values()), "health": record_health}


def heartbeat(runs, registry, now):
    """Due-aware calendar: Sun weekly, Mon-Fri stable, Saturday has no sweep."""
    alerts = []
    for target in registry["integrations"]:
        for exc in target["exceptions"]:
            if datetime.fromisoformat(exc["expires"]).date() <= now.date():
                alerts.append({"id": "exception:" + exc["id"],
                               "summary": f"Expired compatibility exception {exc['id']} for {target['id']}; repair or explicitly review it."})
        if not target["canary"]["enabled"]:
            continue
        cases = [c for r in runs if not r["run"]["self_test"] for c in r["cases"]
                 if c["integration_id"] == target["id"] and c["status"] not in ("stale", "not_tested")
                 and timestamp(c["completed_at"]) <= now
                 and not (c["failure"] and c["failure"]["kind"] == "automation" and not c["contract"]["executed"])]
        representative = target["runtimes"]["representative"][:1]
        boundaries = sorted(set(representative + target["runtimes"]["boundaries"]))
        for cadence, grace in (("daily", timedelta(hours=36)), ("weekly", timedelta(days=8))):
            required = [("source", "latest_eligible", runtime) for runtime in representative]
            if cadence == "weekly":
                required = [("source", selector, runtime) for runtime in boundaries for selector in ("floor", "latest_eligible")]
                if "published" in target["canary"]["modes"]:
                    required += [("published", "latest_eligible", runtime) for runtime in representative]
            # Every required scope needs fresh evidence. One floor/source result
            # cannot substitute for the published consumer or runtime boundaries.
            latest = []
            for mode, selector, runtime in required:
                eligible = [c for c in cases if c["mode"] == mode and c["selector"] == selector
                            and (c["runtime"]["version"] == runtime or c["runtime"]["version"].startswith(runtime + "."))]
                latest.append(max((timestamp(c["completed_at"]) for c in eligible), default=None))
            last = min(latest) if latest and all(t is not None for t in latest) else None
            # Find a scheduled due time whose deadline passed and which has not
            # been satisfied by a later complete observation. A Saturday gap alone
            # cannot create an alert after a complete Friday run.
            missed = False
            if cadence == "weekly":
                # Eight days is the maximum AGE of a complete scope, not eight
                # additional days after the next Sunday (~15 days in total).
                # A complete Sunday remains fresh throughout the next weekend.
                missed = last is None or last + grace <= now
            else:
                for days in range(18):
                    due = (now - timedelta(days=days)).replace(hour=3, minute=17, second=0, microsecond=0)
                    if due.weekday() in (0, 1, 2, 3, 4, 6) and due + grace <= now and (last is None or last < due):
                        missed = True
                        break
            if missed:
                alerts.append({"id": f"heartbeat:{target['id']}:{cadence}",
                               "summary": f"Stale/missing {cadence} canary evidence for {target['id']} "
                               f"(last complete: {iso(last) if last else 'none'}). Check the default-branch scheduler; "
                               "dev-only cron is dormant. No passing status may be inferred."})
    return alerts
