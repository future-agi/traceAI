#!/usr/bin/env python3.12
"""Bounded, offline, sequential profiling companion; leaves results.* untouched."""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(HERE), str(HERE.parent)]
from run import SCRATCH, environment, file_hash, snapshot_node, tree_hash, variant
from measure import run_process, stats
from safety import HostSafety
from mocks import CompareMocks
from scoring import privacy

STAGES = ('transport_buffer', 'body_copy', 'json_codec', 'sse_reassembly', 'privacy_S1',
          'privacy_S5_S6', 'attributes', 'span', 'processor_export', 'context', 'wrapper', 'gc', 'unattributed')
NOTES = [
    'QUICK DIAGNOSTIC, not a benchmark or a release pass. 20-call caps and shared-host p99 are unstable.',
    'Plain, CPU-profile and internal-timer processes are separate; never subtract a profiled run from an unprofiled baseline.',
    'Python RAW profile figures use cProfile exclusive process CPU with C callees apportioned by observed call counts. Its per-event Python process-clock callback has a dominant, instrumentation-dependent observer effect. Raw microseconds and percentages describe the PERTURBED profile only; no unprofiled stage partition is validated. Background exporter CPU can contribute; internal boundary timers independently restrict themselves to the foreground thread. Thread-local clocks are invalid for cross-thread cProfile events.',
    'Node figures are V8 100us active-wall sampling estimates (idle/program excluded), not exact CPU or caller latency. Samples include 4ms inter-call drains and background export callbacks.',
    'Percentages divide each RAW stage by all attributed instrumentation stages IN THE PROFILED PROCESS; they are not unprofiled cost shares. Unattributed includes SDK, runtime and harness work. Node inheritance stops at provider-SDK/HTTP-client and application/harness boundaries, except explicitly named codec/copy shims. Only reviewed chunk-aggregation functions, not generic streaming wrappers, count as Braintrust SSE reassembly.',
    'Normalized stage estimates are WITHHELD for both languages. A positive/noise-screened plain CPU delta does not calibrate stage-dependent observation overhead, background CPU or sample composition. Retain raw profiles, observer-effect audit and independently timed boundaries; do not rescale them to plain CPU or p50.',
    'S1=credential discovery; S5/S6=privacy/safe snapshot boundary. They are grouped because the same recursive scrub functions serve both. No privacy checks are disabled.',
    'Internal timers are mutually exclusive nested boundaries (Python thread CPU, Node synchronous wall); JSON/body-copy *_all rows include provider SDK work and are NOT traceAI-only costs.',
    'Simple/batch is a harness-only processor swap retaining the exact guarded exporter, privacy overlay and coverage callback. Python native=batch; Node native=simple. No production agent sources are modified.',
    'Python is the already-installed comparison wheel, Node is a hash-pinned snapshot of the current prebuilt bundle. These may differ from concurrently edited checkout sources and from results.json.',
    'Node hot-function file:line:column refers to the measured minified OUR bundle; no source map exists. Do not invent original TypeScript line numbers.',
    'No cloud calls: existing clean environment, dead-loopback proxies, loopback mocks and Python/Node socket/DNS/fetch guards. These are defense-in-depth, not an OS firewall.',
]
RECOMMENDATIONS = [
    'S9 P0: keep one authoritative fail-closed final snapshot, but replace repeated whole-span scrubs with policy/secret-registry-versioned immutable safe views. Preserve per-export policy refresh, all resource/event/link/context surfaces, caps, hostile-object handling and multi-exporter isolation. Never reuse a stale safe mark across changed policy or secrets.',
    'S9 P0: queue bounded detached telemetry work after body completion. Python can build/serialize/export on a worker, but must copy bounded owned buffers, request-time context and timing before return; redact before handing telemetry to untrusted exporters. Node BSP only defers exporter dispatch to the event loop, not another CPU thread: use a bounded worker for parse/sanitize/serialization if tail isolation is required.',
    'S9 P1: lazy content materialization; skip request-parameter JSON, duplicate raw.input/raw.output and per-message aliases until a consumer/export convention requires them. Keep model, usage, response identity, error and privacy semantics. Do not parse nested JSON-looking strings repeatedly in each masking pass.',
    'S9 P1: reuse already parsed request and response summaries; parse SSE incrementally once with bounded decoder state, reusing usage/text deltas rather than concatenating and reparsing all frames. Keep consumer identity, cancellation, byte ordering and backpressure tests.',
    'S9 P2: bounded zero-copy views where ownership is guaranteed; one final owned copy only when asynchronous work outlives transport buffers. Cache catalog/URL match results with bounded keys. Keep context capture once per call; avoid per-chunk async-context work and avoid allocation-heavy Proxy/closure construction where safe.',
    'Drop redundant representation/validation work, NOT credential discovery, fail-closed masking, default content capture, sampling correctness or required semantic fields. No safety-off comparison is a proposed optimization.',
]
TARGETS = {
    'python': {'estimated_added_p50_us': None, 'estimated_added_p99_us': None,
               'release_budget_added_p50_us': None, 'release_budget_added_p99_us': None,
               'planning_hypothesis_added_p50_us': [200, 450], 'planning_hypothesis_added_p99_us': [600, 1200],
               'candidate_ceiling_added_p50_us': 500, 'candidate_ceiling_added_p99_us': 1500},
    'node': {'estimated_added_p50_us': None, 'estimated_added_p99_us': None,
             'release_budget_added_p50_us': None, 'release_budget_added_p99_us': None,
             'planning_hypothesis_added_p50_us': [250, 600], 'planning_hypothesis_added_p99_us': [800, 1800],
             'candidate_ceiling_added_p50_us': 650, 'candidate_ceiling_added_p99_us': 2000},
    'status': 'UNVERIFIED PLANNING HYPOTHESES ONLY. Achievable p50/p99 and accepted release budgets are unresolved: no optimized prototype, calibrated stage partition or measured residual-cost model establishes them. Especially no p99 achievability claim.',
    'competitive_policy': 'Absolute candidate ceilings alone do NOT establish competitive parity. Measure a stable Braintrust floor on the same dedicated runner and separately approve a numeric privacy-guarantee surcharge (currently unresolved). Require BOTH an absolute limit and traceAI added overhead <= Braintrust added overhead + that approved surcharge, per scenario and percentile. Nonpositive/noisy Braintrust overhead abstains. The implied gaps below are disclosures, not approved surcharges or evidence that the gaps are acceptable.',
    'gate_protocol': 'Proposed validation protocol, NOT an active release pass: dedicated runner, pinned payload/SDK/runtime, chat/stream/embeddings each, content ON, exact parity/privacy/one-span checks; >=10000 calls per mode across >=6 counterbalanced processes, warmup >=200; use upper bootstrap 95% CI of paired p50/p99 differences. Other candidate budgets: incremental SSE work <=50us p50 / <=200us p99 per chunk; <=4MiB/call retained capture and a queue bounded by BOTH 2048 entries and 32MiB total with visible drops; flush <=3s under healthy loopback collector.',
    'calibration_required': 'Before any unprofiled stage estimates: collect low-overhead statistical Python samples at multiple intervals in counterbalanced plain/sampled processes, quantify per-tool perturbation and sampling uncertainty, compare stage shares across intervals, and validate selected boundaries with timer/no-op controls. Independently validate Node sampling against event-loop/background work and process-CPU mismatch. A positive aggregate CPU anchor or a single privacy timer does not validate every stage proportion. This experiment has NOT been performed.'
}


def python_tag(file, name):
    ours = 'fi_instrumentation/' in file
    if ours:
        if '/_secrets.py' in file or name in ('observe_request', 'observe_body', 'secrets', 'observe_credentials'):
            return 'privacy_S1'
        if '/_privacy.py' in file or '/instrumentation/config.py' in file:
            return 'privacy_S5_S6'
        if '/wire/_core.py' in file:
            return 'sse_reassembly' if name in ('parse_sse', 'parse_ndjson') else 'json_codec' if name == '_json_or_none' else 'attributes'
        if '/_capture.py' in file:
            return 'span' if name == '_emit' else 'context' if name in ('mark_active', '_skip_current_context') else 'transport_buffer'
        if '/_transports.py' in file:
            return 'transport_buffer'
        if 'context' in file:
            return 'context'
        if 'otel.py' in file or '/_manager.py' in file:
            return 'processor_export'
        if '_tracers.py' in file or '_spans.py' in file:
            return 'span'
        return 'wrapper'
    if '/braintrust/' in file:
        if '/integrations/' in file or '/wrappers/' in file:
            return 'sse_reassembly' if name in ('gen', 'agen', '__process_message') else 'wrapper'
        if name in ('start_span', 'end', '__enter__', '__exit__', '_start_span_impl'):
            return 'span'
        if 'background' in file or name in ('flush', '_flush', 'log', '_log', 'log_partial', 'log_feedback'):
            return 'processor_export'
        if 'copy' in name or 'serial' in name or 'merge' in name:
            return 'attributes'
        return 'wrapper'
    if '/opentelemetry/context' in file or '/contextvars' in file:
        return 'context'
    if '/opentelemetry/sdk/trace/export' in file or '/opentelemetry/exporter' in file or '/opentelemetry/sdk/_shared_internal' in file:
        return 'processor_export'
    if '/opentelemetry/sdk/trace/' in file:
        return 'span'
    return None


def python_stages(raw, n):
    import pstats
    data = pstats.Stats(str(raw)).stats
    if any(v[2] < -1e-9 for v in data.values()):
        raise RuntimeError('Invalid negative profiler self times; refuse attribution rather than clip or publish')
    cache = {}
    def attribution(key, seen=frozenset()):
        if key in cache:
            return cache[key]
        if key in seen:
            return {'unattributed': 1.0}
        file, line, name = key
        stage = python_tag(file, name)
        if stage:
            return {stage: 1.0}
        # Only propagate generic library/C helpers; SDK and harness bodies must
        # stay unattributed, even if invoked by an instrumentation wrapper.
        if any(s in file for s in ('/openai/', '/httpx/', '/httpcore/', 'profile_', 'app.py', 'application.py')):
            return {'unattributed': 1.0}
        callers = data[key][4]
        weights = defaultdict(float)
        for caller, values in callers.items():
            count = values[0] if isinstance(values, tuple) else values
            for tag, fraction in attribution(caller, seen | {key}).items():
                weights[tag] += count * fraction
        total = sum(weights.values())
        result = {k: v / total for k, v in weights.items()} if total else {'unattributed': 1.0}
        generic_stage = ('json_codec' if '/json/' in file or '_json.' in name or name in ('raw_decode', 'iterencode')
                         else 'body_copy' if ("'join' of 'bytes'" in name or "'decode' of 'bytes'" in name
                                                or "'encode' of 'str'" in name) else None)
        if generic_stage:
            attributed_weight = sum(v for k, v in result.items() if k != 'unattributed')
            result = {generic_stage: attributed_weight, 'unattributed': result.get('unattributed', 0)}
        cache[key] = result
        return result
    result = defaultdict(float)
    for key, values in data.items():
        name = key[2]
        if 'gc.collect' in name:
            result['gc'] += values[2] * 1e6 / n
        else:
            for stage, weight in attribution(key).items():
                result[stage] += values[2] * 1e6 / n * weight
    return dict(result)


# These are reviewed OpenAI chunk aggregation regions in the measured Braintrust
# package, not every function with "stream" or "generator" in its name.
BRAINTRUST_REASSEMBLY = frozenset({'aggregateChatCompletionChunks'})
NODE_BUILTIN_SHIMS = frozenset({'profileJSONParse', 'profileJSONStringify', 'profileBodyCopy'})
NODE_SDK_PATHS = ('/node_modules/openai/', '/node_modules/@anthropic-ai/sdk/',
                  '/node_modules/@google/genai/', '/node_modules/@google/generative-ai/',
                  '/node_modules/undici/', 'node:internal/deps/undici/')


def node_attribution_boundary(frame):
    """Never inherit an agent label through a provider SDK or the calling app.

    Named harness shims expose otherwise opaque native codec/copy operations;
    only those shims may walk through a harness file. Generic runtime/library
    helpers can inherit, but must still reach an agent before any boundary.
    """
    url = frame.get('url', '')
    if any(path in url for path in NODE_SDK_PATHS):
        return True
    if frame['functionName'] in NODE_BUILTIN_SHIMS:
        return False
    return bool(url and not url.startswith('node:') and not url.endswith('/runtime.cjs')
                and '/node_modules/' not in url)


def node_stages(raw, ranges, n, tool):
    profile = json.loads(raw.read_text())
    nodes = {row['id']: row for row in profile['nodes']}
    parents = {child: row['id'] for row in nodes.values() for child in row.get('children', [])}
    starts = ranges['line_starts']
    resolved = {}
    def tag(node):
        if node['id'] in resolved:
            return resolved[node['id']]
        frame = node['callFrame']; url = frame.get('url', '')
        result = (None, False)
        if url.endswith('/runtime.cjs') and frame['lineNumber'] < len(starts):
            pos = starts[frame['lineNumber']] + frame['columnNumber']
            options = [r for r in ranges['ranges'] if r['start'] <= pos < r['end']]
            if options:
                best = min(options, key=lambda r: r['end'] - r['start'])
                result = best['stage'], best['ours']
            elif frame['functionName'] in ('startSpan', 'setAttribute', 'setAttributes', 'setStatus', 'addEvent', 'end', 'spanContext', 'isRecording'):
                result = 'span', False
            elif frame['functionName'] in ('active', 'with', 'bind', 'getValue', 'setValue', 'deleteValue'):
                result = 'context', False
            elif frame['functionName'] in ('onEnd', 'onStart', '_exportSpan', '_flushOneBatch', '_flushAll'):
                result = 'processor_export', False
        if '/node_modules/braintrust/' in url:
            name = frame['functionName']
            result = ('sse_reassembly' if name in BRAINTRUST_REASSEMBLY
                      else 'span' if 'span' in name.lower()
                      else 'processor_export' if any(k in name.lower() for k in ('log', 'flush', 'export'))
                      else 'attributes' if any(k in name.lower() for k in ('copy', 'merg', 'serial')) else 'wrapper', False)
        if frame['functionName'] == '(garbage collector)':
            result = 'gc', False
        resolved[node['id']] = result
        return result
    stages = defaultdict(float); hot = defaultdict(float)
    active = 0; idle = 0; stopped_us = 0; stopped_samples = 0
    for node_id, delta in zip(profile.get('samples', []), profile.get('timeDeltas', [])):
        leaf = nodes[node_id]; name = leaf['callFrame']['functionName']
        if name in ('(idle)', '(program)', '(root)'):
            idle += delta; continue
        active += delta
        cursor = node_id; category = None; builtin_stage = None
        while cursor is not None:
            frame = nodes[cursor]['callFrame']
            frame_name = frame['functionName']
            if frame_name in ('profileJSONParse', 'profileJSONStringify'):
                builtin_stage = 'json_codec'
            elif frame_name == 'profileBodyCopy':
                builtin_stage = 'body_copy'
            category, _ = tag(nodes[cursor])
            if category:
                break
            if node_attribution_boundary(frame):
                stopped_us += delta
                stopped_samples += 1
                break
            cursor = parents.get(cursor)
        if category and builtin_stage:
            category = builtin_stage
        stages[category or 'unattributed'] += delta / n
        # Hot functions are SELF samples, never an inclusive stack repeated.
        category, ours = tag(leaf)
        if ours or (tool == 'braintrust' and '/node_modules/braintrust/' in leaf['callFrame'].get('url', '')):
            frame = leaf['callFrame']
            name = ranges.get('labels', {}).get(name, name) or '<anonymous>'
            hot[(frame.get('url', ''), frame['lineNumber'] + 1, frame['columnNumber'] + 1, name)] += delta / n
    functions = [{'file': f, 'line': l, 'column': c, 'function': name, 'self_us': us}
                 for (f, l, c, name), us in sorted(hot.items(), key=lambda x: -x[1])]
    return dict(stages), functions, {'active_samples_us': active, 'idle_program_us': idle,
                                    'boundary_stopped_us_per_call': stopped_us / n,
                                    'boundary_stopped_samples': stopped_samples,
                                    'samples': len(profile.get('samples', []))}


def execute(language, tool, processor, mode, repetition, args, work, guard, ranges):
    label = f'{language}-{args.current_scenario}-{tool}-{processor}-{mode}-{repetition}'
    source, _ = variant(language, tool, args.current_scenario)
    app = work / (label + ('.py' if language == 'python' else '.mjs'))
    raw = work / (label + ('.pstats' if language == 'python' else '.cpuprofile'))
    if language == 'python':
        app.write_text(source)
        runner = work / 'profile_python.py'
        if not runner.exists():
            shutil.copy2(HERE / 'profile_python.py', runner)
    else:
        prefix = source.split('console.log(JSON.stringify({kind:\'ready\'}));')[0]
        runner_source = (HERE / 'profile_node.mjs').read_text()
        if tool == 'braintrust':
            runner_source = runner_source.replace('// FLUSH_SETUP', 'await compareLogger.flush();')
        app.write_text(prefix + '\n' + runner_source)
        runner = app
    with CompareMocks() as mocks:
        env = environment(work, mocks, args.current_scenario, args)
        env.update({'PROFILE_TOOL': tool, 'PROFILE_PROCESSOR': processor, 'PROFILE_MODE': mode,
                    'PROFILE_APP': str(app), 'PROFILE_RAW': str(raw), 'PROFILE_NODE_DEPS': str(args.node_deps)})
        if language == 'python':
            if tool == 'traceai':
                env['FI_AUTO_INSTRUMENT'] = 'true'
            command = [args.python, '-I', str(runner)]
        else:
            command = [args.node, '--import', str(HERE / 'loopback.mjs')]
            command += ['--import', '@traceai/auto'] if tool == 'traceai' else ['--import', 'braintrust/hook.mjs'] if tool == 'braintrust' else []
            command += [str(runner)]
        with guard.step():
            run = run_process(command, env, work, work / (label + '.stderr.log'), args.timeout)
        export_count = len(mocks.spans) if tool == 'traceai' else len(mocks.bt_rows) if tool == 'braintrust' else 0
        identities = ({(s.get('trace_id'), s.get('span_id')) for s in mocks.spans} if tool == 'traceai'
                      else {r.get('id') for r in mocks.bt_rows if r.get('id')} if tool == 'braintrust' else set())
        provider_count = len(mocks.requests)
        unexpected_baseline_exports = tool == 'baseline' and bool(mocks.spans or mocks.bt_rows)
        collector_errors = list(mocks.collector_errors)
        # Scan the decoded exported bodies (resource, events and links included), as the comparison does.
        credential_leak = privacy(mocks.exports)['credentials_in_payload']
    calls = [r for r in run['records'] if r.get('kind') == 'call']
    profile = next((r for r in run['records'] if r.get('kind') == 'profile'), {})
    errors = []
    if run['exit_code'] or run['timed_out'] or len(calls) != args.n or not profile:
        errors.append('failed/incomplete application: ' + run['stderr_log'])
    if collector_errors:
        errors.extend(collector_errors)
    if credential_leak:
        errors.append('credential canary in exported payload')
    if provider_count != args.n + args.warmup:
        errors.append('provider request count mismatch')
    if tool != 'baseline' and len(identities) != args.n + args.warmup:
        errors.append('missing/extra telemetry identities, including warmups')
    if tool == 'traceai' and export_count != len(identities):
        errors.append('duplicate traceAI span exports')
    if unexpected_baseline_exports:
        errors.append('baseline unexpectedly exported telemetry')
    if not any(r.get('kind') == 'done' for r in run['records']):
        errors.append('missing done')
    stages = {}; functions = profile.pop('functions', [])
    sample_info = {}
    if mode == 'cpu' and not errors:
        if language == 'python':
            stages = python_stages(raw, args.n)
            functions = [f for f in functions if ('fi_instrumentation/' in f['file'] if tool == 'traceai' else '/braintrust/' in f['file'])]
        else:
            stages, functions, sample_info = node_stages(raw, ranges, args.n, tool)
    print(label + (' OK' if not errors else ' FAIL ' + repr(errors)), file=sys.stderr, flush=True)
    return {'language': language, 'scenario': args.current_scenario, 'tool': tool, 'processor': processor,
            'mode': mode, 'repetition': repetition, 'n': args.n, 'warmup': args.warmup,
            'latency_us': stats([r['latency_us'] for r in calls]), 'calls': calls,
            'profile': profile, 'stages_us': stages, 'top_functions': functions[:50], 'sampling': sample_info,
            'raw_profile': str(raw) if mode == 'cpu' else None, 'command': command,
            'stderr_log': run['stderr_log'], 'exports_or_rows': export_count,
            'telemetry_identities': len(identities), 'provider_requests': provider_count,
            'credential_leak': credential_leak, 'errors': errors}


def summarize(runs):
    breakdown = {}; hot = {}; overhead = []
    for language in sorted({r['language'] for r in runs}):
        for tool in ('traceai', 'braintrust'):
            policy = 'batch' if language == 'python' else 'simple'
            selected = [r for r in runs if r['language'] == language and r['tool'] == tool and r['mode'] == 'cpu'
                        and (tool != 'traceai' or r['processor'] == policy) and not r['errors']]
            costs = {s: statistics.fmean(r['stages_us'].get(s, 0) for r in selected) for s in STAGES} if selected else {}
            attributed = sum(v for k, v in costs.items() if k != 'unattributed')
            cpu_deltas = []
            cluster_means = []
            for plain in runs:
                if (plain['language'] != language or plain['tool'] != tool or plain['mode'] != 'plain'
                        or (tool == 'traceai' and plain['processor'] != policy) or plain['errors']):
                    continue
                baseline = next((r for r in runs if r['tool'] == 'baseline' and r['mode'] == 'plain'
                                 and all(r[k] == plain[k] for k in ('language', 'scenario', 'repetition'))), None)
                if baseline:
                    deltas = [a['cpu_us'] - b['cpu_us'] for a, b in zip(plain['calls'], baseline['calls'])]
                    cpu_deltas += deltas
                    if deltas:
                        cluster_means.append(statistics.fmean(deltas))
            anchor = statistics.fmean(cpu_deltas) if cpu_deltas else None
            se = statistics.stdev(cluster_means) / len(cluster_means) ** .5 if len(cluster_means) > 1 else 0
            # An aggregate anchor, however positive/stable, cannot correct
            # stage-dependent profiler overhead. Never publish normalized stage
            # estimates without an independent sampling/calibration experiment.
            breakdown[language + '/' + tool] = {'method': 'cProfile process CPU' if language == 'python' else 'V8 active-wall samples',
                'processor': policy if tool == 'traceai' else 'native', 'attributed_us': attributed,
                'unprofiled_added_mean_cpu_us': anchor, 'normalization_reliable': False,
                'normalization_status': 'withheld_uncalibrated_observer_effect',
                'stage_values_scope': 'perturbed_profile_only_not_unprofiled_costs_or_shares',
                'unprofiled_cpu_cluster_count': len(cluster_means), 'unprofiled_cpu_cluster_se_us': se,
                'estimate_warning': 'RAW perturbed profile only. Microseconds and percentages are NOT unprofiled stage costs/shares; no uniform rescaling is supported. Python per-event observer overhead is instrumentation-dependent. Node active-wall samples include background work and cannot partition whole-process added CPU.',
                'stages': {k: {'us_per_call': v, 'estimated_added_cpu_us': None,
                              'percent_of_attributed': 100 * v / attributed if attributed and k != 'unattributed' else None}
                           for k, v in costs.items()}}
            merged = defaultdict(float)
            for row in selected:
                for f in row['top_functions']:
                    merged[(f['file'], f['line'], f.get('column'), f['function'])] += f['self_us'] / len(selected)
            hot[language + '/' + tool] = [{'file': f, 'line': line, 'column': col, 'function': name, 'self_us_per_call': value}
                for (f, line, col, name), value in sorted(merged.items(), key=lambda i: -i[1])[:10]]
    for row in runs:
        if row['tool'] == 'baseline':
            continue
        baseline = next((r for r in runs if all(r[k] == row[k] for k in ('language', 'scenario', 'repetition', 'mode')) and r['tool'] == 'baseline'), None)
        if baseline is None:
            row['errors'].append('missing paired baseline')
            continue
        parity = len(row['calls']) == len(baseline['calls']) and all(a['response'] == b['response'] for a, b in zip(row['calls'], baseline['calls']))
        row['parity'] = parity
        if not parity:
            row['errors'].append('response parity failed')
        if row['mode'] != 'plain':
            continue
        added = [a['latency_us'] - b['latency_us'] for a, b in zip(row['calls'], baseline['calls'])]
        overhead.append({k: row[k] for k in ('language', 'scenario', 'tool', 'processor', 'repetition')} |
                        {'parity': parity, 'added_us': stats(added), 'errors': row['errors']})
    return breakdown, hot, overhead


def plain_deltas(runs, key, field):
    """Signed within-repetition/index differences, never profiled minus plain."""
    values = []
    for row in runs:
        if (tuple(row[k] for k in ('language', 'scenario', 'tool', 'processor')) != key
                or row['mode'] != 'plain' or row['errors']):
            continue
        baseline = next((b for b in runs if b['tool'] == 'baseline' and b['mode'] == 'plain' and not b['errors']
                         and all(b[k] == row[k] for k in ('language', 'scenario', 'repetition'))), None)
        if baseline and len(row['calls']) == len(baseline['calls']):
            values += [a[field] - b[field] for a, b in zip(row['calls'], baseline['calls'])]
    return values


def observer_effect(runs):
    """Descriptive plain/profiled comparisons, NOT a calibration correction."""
    result = []
    keys = sorted({tuple(r[k] for k in ('language', 'scenario', 'tool', 'processor')) for r in runs})
    for key in keys:
        by_mode = {mode: [r for r in runs if r['mode'] == mode and not r['errors']
                         and tuple(r[k] for k in ('language', 'scenario', 'tool', 'processor')) == key]
                   for mode in ('plain', 'cpu', 'internal')}
        medians = {mode: stats([c['latency_us'] for r in rows for c in r['calls']]).get('p50')
                   for mode, rows in by_mode.items()}
        added = plain_deltas(runs, key, 'cpu_us') if key[2] != 'baseline' else []
        anchor = statistics.fmean(added) if added else None
        raw = [sum(v for k, v in r['stages_us'].items() if k != 'unattributed') for r in by_mode['cpu']]
        attributed = statistics.fmean(raw) if raw else None
        result.append(dict(zip(('language', 'scenario', 'tool', 'processor'), key)) | {
            'plain_p50_us': medians['plain'], 'cpu_profile_p50_us': medians['cpu'],
            'internal_p50_us': medians['internal'],
            'cpu_profile_to_plain_p50_ratio': medians['cpu'] / medians['plain'] if medians['plain'] and medians['cpu'] is not None else None,
            'unprofiled_added_mean_cpu_us': anchor, 'raw_profile_attributed_us_per_call': attributed,
            'raw_profile_to_unprofiled_added_cpu_ratio': attributed / anchor if attributed is not None and anchor and anchor > 0 else None,
            'unprofiled_stage_composition_validated': False})
    return result


def competitive_budget_context(runs):
    """Expose, rather than endorse, what the old absolute hypotheses permit."""
    result = []
    for language, scenario in sorted({(r['language'], r['scenario']) for r in runs}):
        diffs = plain_deltas(runs, (language, scenario, 'braintrust', 'native'), 'latency_us')
        p50 = stats(diffs).get('p50')
        ceiling = TARGETS[language]['candidate_ceiling_added_p50_us']
        result.append({'language': language, 'scenario': scenario, 'braintrust_added_p50_us': p50,
                       'candidate_traceai_ceiling_p50_us': ceiling,
                       'candidate_to_braintrust_ratio': ceiling / p50 if p50 and p50 > 0 else None,
                       'implied_unapproved_surcharge_us': ceiling - p50 if p50 and p50 > 0 else None,
                       'approved_privacy_surcharge_us': None,
                       'status': 'descriptive_tiny_smoke_only_not_an_accepted_budget'})
    return result


def observations(data):
    notes = []
    def internal(language, tool, policy):
        return [r for r in data['runs'] if r['language'] == language and r['tool'] == tool
                and r['processor'] == policy and r['mode'] == 'internal' and not r['errors']]
    py = internal('python', 'traceai', 'batch')
    if py:
        privacy = statistics.fmean(r['profile'].get('stages_us', {}).get('privacy_S5_S6', 0) for r in py)
        copies = statistics.fmean(r['profile'].get('boundary_calls_per_call', {}).get('fi_instrumentation.auto.wire._privacy.export_span', 0) for r in py)
        masks = statistics.fmean(r['profile'].get('boundary_calls_per_call', {}).get('fi_instrumentation.auto.wire._privacy.mask_attributes', 0) for r in py)
        notes.append(f'Python native batch: foreground privacy boundaries consume {privacy:.1f} us/call under internal timers, with {copies:.1f} export_span snapshots and {masks:.1f} mask_attributes invocations per call before any unobserved background work. Repeated privacy/materialization is the primary target, not switching Python to SimpleSpanProcessor.')
    node = internal('node', 'traceai', 'simple'); baseline = internal('node', 'baseline', 'native')
    if node and baseline:
        codec = statistics.fmean(r['profile'].get('stage_calls_per_call', {}).get('json_codec_all', 0) for r in node)
        off = statistics.fmean(r['profile'].get('stage_calls_per_call', {}).get('json_codec_all', 0) for r in baseline)
        dispatch = statistics.fmean(r['profile'].get('stages_us', {}).get('privacy_and_export_dispatch', 0) for r in node)
        notes.append(f'Node native simple: {codec:.1f} whole-application JSON parse/stringify invocations per call versus baseline {off:.1f}; guarded privacy/export synchronous dispatch measures {dispatch:.1f} us/call under separate internal timers (codec child time excluded). Do not add overlapping sampled and timed figures.')
    for language in ('python', 'node'):
        arms = []
        for scenario in ('openai_chat', 'openai_stream', 'embeddings'):
            values = {}
            for policy in ('simple', 'batch'):
                diffs = []
                rows = [r for r in data['runs'] if r['language'] == language and r['tool'] == 'traceai'
                        and r['scenario'] == scenario and r['processor'] == policy and r['mode'] == 'plain']
                for r in rows:
                    base = next((b for b in data['runs'] if b['tool'] == 'baseline' and b['mode'] == 'plain'
                                 and all(b[k] == r[k] for k in ('language', 'scenario', 'repetition'))), None)
                    if base:
                        diffs += [a['latency_us'] - b['latency_us'] for a, b in zip(r['calls'], base['calls'])]
                if diffs:
                    values[policy] = stats(diffs)['p50']
            if len(values) == 2:
                arms.append(f"{scenario} {values['simple']:.0f} / {values['batch']:.0f}")
        if arms:
            notes.append(language + ' added p50 simple / batch (us): ' + '; '.join(arms) + '. Tiny shared-host observations, not causal effect sizes or stable tail claims.')
    notes.append('Byte-copy/SSE have few observed samples on these tiny fixtures; that does not establish their unprofiled costs. Transport includes observation/wrapper bookkeeping, not just copying. The fetch hook observes caller-created clones/tees rather than proactively teeing every response. Large payloads, slow consumers and concurrency are not measured.')
    notes.append('Braintrust has no directly equivalent S1/S5/S6 boundary in this attribution model: zero in those cells is not proof of identical privacy protection or zero sanitization cost. Sampled GC is process-wide, not proof of incremental agent GC.')
    return notes


def render(data):
    lines = ['# Offline cost profile', '', '**QUICK DIAGNOSTIC — NOT A BENCHMARK.**', '',
             f"Generated {data['created_at']}; N={data['config']['n']}, warmup={data['config']['warmup']}, plain repetitions={data['config']['repetitions']}. All runs sequential.", '', '## Scope and interpretation', '']
    if data.get('reanalyzed_at'):
        lines.insert(6, f"Interpretation recomputed {data['reanalyzed_at']} from unchanged saved profiles; the measurement date above is original.")
        lines.insert(7, '')
    lines += ['- ' + n for n in NOTES]
    lines += ['', '## What the measurements indicate', ''] + ['- ' + n for n in data.get('observations', observations(data))]
    lines += ['', '## Unprofiled stage partition: WITHHELD', '',
              'No normalized microsecond estimates are published for either language. Python process-clock callbacks perturb high-event-count instrumentation disproportionately; multiplying raw stage shares by a plain CPU delta cannot remove that bias. Node active-wall sample composition is also uncalibrated against background/worker CPU. Raw percentages below describe ONLY the observed profiled execution, not how unprofiled per-call time is divided. Independently timed boundaries support local prioritization, not all other stage shares.', '',
              TARGETS['calibration_required'], '', '## Observer-effect audit', '',
              'Separate processes, shared host; ratios are descriptive, not causal correction factors. Plain p50 pools plain repetitions. Raw attributed / plain added CPU is especially not a scale factor: its numerator includes profiler effects and may include background work. Baseline and nonpositive denominators abstain.', '',
              '| Language/scenario/tool/processor | Plain p50 us | CPU-profile p50 us | Ratio | Internal p50 us | Raw attributed us/call | Plain added mean CPU us | Raw / added |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    def number(value, decimals=1):
        return 'unresolved' if value is None else f'{value:.{decimals}f}'
    for row in data['observer_effect']:
        key = '/'.join(row[k] for k in ('language', 'scenario', 'tool', 'processor'))
        values = [number(row[k], 2 if 'ratio' in k else 1) for k in
                  ('plain_p50_us', 'cpu_profile_p50_us', 'cpu_profile_to_plain_p50_ratio', 'internal_p50_us',
                   'raw_profile_attributed_us_per_call', 'unprofiled_added_mean_cpu_us', 'raw_profile_to_unprofiled_added_cpu_ratio')]
        lines.append('| ' + key + ' | ' + ' | '.join(values) + ' |')
    lines += ['', '## Raw native-policy stage profile', '',
              'Equal scenario weight; **perturbed profile us/call (percent of attributed instrumentation CPU/samples)**. These are NOT measured unprofiled costs or shares. Python and Node methods differ; do not compare their totals as a speed ratio. Unattributed is separately shown. Node SDK/harness samples stop before an instrumentation ancestor, and Braintrust SSE means reviewed chunk aggregation, not traceStreamingCall.', '',
              '| Stage | Python traceAI | Python Braintrust | Node traceAI | Node Braintrust |', '|---|---:|---:|---:|---:|']
    for stage in STAGES:
        values = []
        for key in ('python/traceai', 'python/braintrust', 'node/traceai', 'node/braintrust'):
            cost = data['breakdown'].get(key, {}).get('stages', {}).get(stage)
            values.append('n/a' if cost is None else f"{cost['us_per_call']:.1f}" + (f" ({cost['percent_of_attributed']:.1f}%)" if cost['percent_of_attributed'] is not None else ' (excluded)'))
        lines.append('| ' + stage + ' | ' + ' | '.join(values) + ' |')
    lines += ['', 'GC zero means no observed Python explicit gc.collect/profile leaf or Node GC sample, NOT no allocation/GC cost. Node body-copy/JSON builtins can be charged to their parent stage; zero does not prove free.', '', '## Unprofiled paired added latency', '',
              'Each row pools within-repetition index-paired differences. Adjacent repetitions reverse the four-condition order; this is NOT the original six-permutation fair-ratio gate. No ratios or release conclusions.', '',
              '| Language/scenario/tool/processor | N | Added p50 us | Added p99 us | Parity |', '|---|---:|---:|---:|---|']
    keys = sorted({tuple(r[k] for k in ('language', 'scenario', 'tool', 'processor')) for r in data['overhead']})
    for key in keys:
        rows = [r for r in data['runs'] if tuple(r[k] for k in ('language', 'scenario', 'tool', 'processor')) == key and r['mode'] == 'plain']
        diffs = []
        for r in rows:
            b = next((x for x in data['runs'] if x['tool'] == 'baseline' and x['mode'] == 'plain' and all(x[k] == r[k] for k in ('language', 'scenario', 'repetition'))), None)
            if b is not None:
                diffs += [a['latency_us'] - c['latency_us'] for a, c in zip(r['calls'], b['calls'])]
        s = stats(diffs)
        parity = all(r['parity'] and not r['errors'] for r in data['overhead'] if tuple(r[k] for k in ('language', 'scenario', 'tool', 'processor')) == key)
        lines.append('| ' + '/'.join(key) + f" | {s['n']} | {s.get('p50', 0):.1f} | {s.get('p99', 0):.1f} | {parity} |")
    lines += ['', '## Fine-grained internal timing (separate perturbed processes)', '',
              'Python exclusive foreground thread CPU; Node exclusive synchronous wall. Whole-application codec/copy rows are explicitly *_all. Values cannot be subtracted from plain added latency. Private Node stages remain CPU-sample estimates above.', '',
              '| Language/tool/processor | Boundary | us/call | Calls/call |', '|---|---|---:|---:|']
    internal_keys = sorted({(r['language'], r['tool'], r['processor']) for r in data['runs'] if r['mode'] == 'internal'})
    for key in internal_keys:
        rows = [r for r in data['runs'] if r['mode'] == 'internal' and (r['language'], r['tool'], r['processor']) == key]
        stages = sorted({s for r in rows for s in r['profile'].get('stages_us', {})})
        for stage in stages:
            us = statistics.fmean(r['profile'].get('stages_us', {}).get(stage, 0) for r in rows)
            calls = statistics.fmean(r['profile'].get('stage_calls_per_call', {}).get(stage, 0) for r in rows)
            lines.append('| ' + '/'.join(key) + f' | {stage} | {us:.1f} | {calls:.2f} |')
    for key, functions in data['hot_functions'].items():
        lines += ['', '## Top 10 self-time hot functions: ' + key, '', '| Function | Measured file:line:column | us/call |', '|---|---|---:|']
        for f in functions:
            loc = f"{f['file']}:{f['line']}" + (f":{f['column']}" if f.get('column') is not None else '')
            lines.append(f"| {f['function']} | `{loc}` | {f['self_us_per_call']:.2f} |")
        if len(functions) < 10:
            lines.append(f"\nOnly {len(functions)} functions had attributable self samples; do not invent the rest.")
    lines += ['', '## ROUND6 S9 design recommendations', ''] + [f'{i+1}. {text}' for i, text in enumerate(RECOMMENDATIONS)]
    lines += ['', '## Planning hypotheses, NOT achievable estimates or accepted gates', '', TARGETS['status'], '',
              '| Language | Unverified planning p50 / p99 range us | Candidate absolute ceiling p50 / p99 us | Achievable p50 / p99 | Accepted gate |', '|---|---:|---:|---|---|']
    for language in ('python', 'node'):
        t = TARGETS[language]
        lines.append(f"| {language} | {t['planning_hypothesis_added_p50_us']} / {t['planning_hypothesis_added_p99_us']} | {t['candidate_ceiling_added_p50_us']} / {t['candidate_ceiling_added_p99_us']} | unresolved | none |")
    lines += ['', TARGETS['competitive_policy'], '',
              '| Language/scenario | Observed Braintrust added p50 us | Candidate ceiling us | Ceiling / Braintrust | Implied UNAPPROVED privacy surcharge us |',
              '|---|---:|---:|---:|---:|']
    for row in data['competitive_budget_context']:
        lines.append('| ' + row['language'] + '/' + row['scenario'] + ' | ' + ' | '.join(number(row[k]) for k in
                     ('braintrust_added_p50_us', 'candidate_traceai_ceiling_p50_us', 'candidate_to_braintrust_ratio', 'implied_unapproved_surcharge_us')) + ' |')
    lines += ['', 'Positive smoke overheads are NOT a stable attainable floor. In particular, a 500 us Python or 650 us Node absolute ceiling can permit a large Braintrust-relative gap; different privacy guarantees require an explicit justified surcharge, not an unexplained multiplier. No surcharge or attainable tail budget is established here. Numeric optimization savings are unresolved; the boundary times are not additive removable costs.', '',
              TARGETS['gate_protocol'], '',
              '## Provenance and validation', '', f"Scratch: `{data['work']}`. Raw profiles, logs, per-call data, timer call counts, exact commands, sampling counts and hashes are in profile.json.", '',
              'Failures: ' + str(sum(bool(r['errors']) for r in data['runs'])), '', '```json', json.dumps(data['provenance'], indent=2), '```', '']
    return '\n'.join(lines)


def reanalyze(args):
    """Recompute attribution from saved raw profiles without running either SDK."""
    data = json.loads(args.output.read_text())
    work = Path(data['work']).resolve()
    if not work.is_relative_to(SCRATCH.resolve()):
        raise ValueError('Saved workload must be in profiling scratch')
    guard = HostSafety(work)
    ranges = {}
    if any(r['language'] == 'node' for r in data['runs']):
        with CompareMocks() as mocks:
            env = environment(work, mocks, 'openai_chat', args)
            with guard.step():
                parsed = run_process([args.node, '--import', str(HERE / 'loopback.mjs'), str(HERE / 'profile_ranges.cjs'),
                                      str(args.node_deps), str(work / 'node_modules/@traceai/auto/agent/runtime.cjs')],
                                     env, work, work / 'reanalyze-ranges.stderr.log', 30)
        if parsed['exit_code'] or not parsed['records']:
            raise RuntimeError('Saved bundle attribution failed')
        ranges = parsed['records'][0]
    for row in data['runs']:
        if row['mode'] != 'cpu' or row['errors']:
            continue
        raw = Path(row['raw_profile']).resolve()
        if not raw.is_relative_to(work):
            raise ValueError('Raw profile must belong to this saved workload')
        if row['language'] == 'python':
            row['stages_us'] = python_stages(raw, row['n'])
        else:
            row['stages_us'], row['top_functions'], row['sampling'] = node_stages(raw, ranges, row['n'], row['tool'])
            row['top_functions'] = row['top_functions'][:50]
    data['breakdown'], data['hot_functions'], data['overhead'] = summarize(data['runs'])
    data['methodology'] = NOTES
    data['recommendations'] = RECOMMENDATIONS
    data['targets'] = TARGETS
    data['schema_version'] = 2
    data['observations'] = observations(data)
    data['observer_effect'] = observer_effect(data['runs'])
    data['competitive_budget_context'] = competitive_budget_context(data['runs'])
    data['reanalyzed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data['provenance']['analysis_sha256'] = {name: file_hash(HERE / name) for name in ('profile_compare.py', 'profile_ranges.cjs')}
    data['provenance']['analysis_note'] = 'Attribution recomputed from unchanged saved raw profiles; workload harness hashes remain those of the actual execution.'
    args.output.write_text(json.dumps(data, indent=2) + '\n')
    args.output.with_suffix('.md').write_text(render(data))
    print(str(args.output))
    return 1 if any(r['errors'] for r in data['runs']) else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quick', action='store_true', required=True)
    p.add_argument('--reanalyze', action='store_true', help='recompute saved --output attribution without SDK calls')
    p.add_argument('--languages', default='python,node')
    p.add_argument('--scenarios', default='openai_chat,openai_stream,embeddings')
    p.add_argument('--n', type=int, default=20)
    p.add_argument('--warmup', type=int, default=5)
    p.add_argument('--repetitions', type=int, default=2)
    p.add_argument('--modes', default='plain,cpu,internal')
    p.add_argument('--python', default=str(SCRATCH / 'python/bin/python'))
    p.add_argument('--node', default=shutil.which('node') or 'node')
    p.add_argument('--node-deps', type=Path, default=SCRATCH / 'profile-deps')
    p.add_argument('--settle-seconds', type=float, default=.3)
    p.add_argument('--timeout', type=float, default=40)
    p.add_argument('--output', type=Path, default=HERE / 'profile.json')
    args = p.parse_args()
    languages = args.languages.split(','); scenarios = args.scenarios.split(','); modes = args.modes.split(',')
    if (not 1 <= args.n <= 20 or not 1 <= args.warmup <= 5 or not 1 <= args.repetitions <= 2
            or not 0 <= args.settle_seconds <= 2 or not 1 <= args.timeout <= 60
            or not set(languages) <= {'python', 'node'} or not set(scenarios) <= {'openai_chat', 'openai_stream', 'embeddings'}
            or not set(modes) <= {'plain', 'cpu', 'internal'}):
        p.error('quick limits: N 1..20, warmup 1..5, repetitions 1..2, settle 0..2, timeout 1..60; only documented selections')
    args.node_deps = args.node_deps.resolve()
    if not args.node_deps.is_relative_to(SCRATCH.resolve()):
        p.error('Node profiling dependencies must be under external-drive scratch')
    if args.reanalyze:
        return reanalyze(args)
    work = SCRATCH / 'profiles' / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + f'-{os.getpid()}')
    work.mkdir(parents=True)
    (work / 'node_modules').mkdir()
    for name in ('braintrust', 'openai'):
        (work / 'node_modules' / name).symlink_to(SCRATCH / 'node_modules' / name, target_is_directory=True)
    guard = HostSafety(work)
    provenance = {'harness_sha256': {name: file_hash(HERE / name) for name in
                  ('profile_compare.py', 'profile_python.py', 'profile_node.mjs', 'profile_ranges.cjs', 'app.py', 'app.mjs', 'mocks.py', 'loopback.mjs')},
                  'runtime': {}, 'dependencies': {}}
    ranges = {}
    if 'node' in languages:
        source = HERE.parents[2] / 'typescript/packages/traceai_auto/dist/agent'
        before = tree_hash(source)
        provenance['node_snapshot'] = snapshot_node(work)
        if before != tree_hash(source):
            raise RuntimeError('Node payload changed during snapshot; rerun after build settles')
        (work / 'node_modules/acorn').symlink_to(args.node_deps / 'node_modules/acorn', target_is_directory=True)
        with CompareMocks() as parser_mocks:
            parser_env = environment(work, parser_mocks, scenarios[0], args)
            with guard.step():
                result = run_process([args.node, '--import', str(HERE / 'loopback.mjs'), str(HERE / 'profile_ranges.cjs'), str(args.node_deps),
                                      str(work / 'node_modules/@traceai/auto/agent/runtime.cjs')],
                                     parser_env, work, work / 'ranges.stderr.log', 30)
        if result['exit_code']:
            raise RuntimeError('Bundle attribution parser failed: ' + result['stderr_log'])
        ranges = result['records'][0]
        provenance['dependencies']['node_profile_lock_sha256'] = file_hash(args.node_deps / 'package-lock.json')
    # Use the existing loopback child environment even for version probing.
    with CompareMocks() as mocks:
        env = environment(work, mocks, scenarios[0], args)
        if 'python' in languages:
            code = 'import sys,json,importlib.metadata as m,fi_instrumentation,pathlib,hashlib; p=pathlib.Path(fi_instrumentation.__file__).parent; print(json.dumps({"kind":"versions","runtime":sys.version,"packages":{d.metadata["Name"]:d.version for d in m.distributions()},"source_sha256":{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob("*.py")}}))'
            with guard.step():
                probe = run_process([args.python, '-I', '-c', code], env, work, work / 'python-versions.stderr.log', 20)
            if probe['exit_code']:
                raise RuntimeError('Python provenance probe failed')
            provenance['runtime']['python'] = probe['records'][0]
        if 'node' in languages:
            with guard.step():
                probe = run_process([args.node, '-e', 'const fs=require("node:fs"); console.log(JSON.stringify({kind:"versions",runtime:process.version,packages:Object.fromEntries(["openai","braintrust"].map(n=>[n,JSON.parse(fs.readFileSync("node_modules/"+n+"/package.json","utf8")).version]))}))'], env, work, work / 'node-versions.stderr.log', 20)
            if probe['exit_code']:
                raise RuntimeError('Node provenance probe failed: ' + probe['stderr_log'])
            provenance['runtime']['node'] = probe['records'][0]
    runs = []
    try:
        for language in languages:
            for scenario in scenarios:
                args.current_scenario = scenario
                for mode in modes:
                    for rep in range(args.repetitions if mode == 'plain' else 1):
                        conditions = [('baseline', 'native'), ('traceai', 'batch' if language == 'python' else 'simple'),
                                      ('braintrust', 'native'), ('traceai', 'simple' if language == 'python' else 'batch')]
                        for tool, processor in conditions if rep % 2 == 0 else list(reversed(conditions)):
                            row = execute(language, tool, processor, mode, rep, args, work, guard, ranges)
                            runs.append(row)
                            if row['errors']:
                                raise RuntimeError('Profiling child failed; evidence saved: ' + row['stderr_log'])
    finally:
        breakdown, hot, overhead = summarize(runs)
        data = {'schema_version': 2, 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'config': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k != 'current_scenario'},
                'work': str(work), 'provenance': provenance, 'host_observations': guard.observations,
                'methodology': NOTES, 'breakdown': breakdown, 'hot_functions': hot, 'overhead': overhead,
                'recommendations': RECOMMENDATIONS, 'targets': TARGETS, 'runs': runs}
        data['observations'] = observations(data)
        data['observer_effect'] = observer_effect(runs)
        data['competitive_budget_context'] = competitive_budget_context(runs)
        args.output.write_text(json.dumps(data, indent=2) + '\n')
        args.output.with_suffix('.md').write_text(render(data))
        print(str(args.output))
    return 1 if any(r['errors'] for r in runs) else 0


if __name__ == '__main__':
    raise SystemExit(main())
