import copy
import json
from pathlib import Path
import unittest

from incidents import (REPORTER_USER, body_for, classify, heartbeat, incident_key, parse_issue,
                       reconcile, text, timestamp, upstream_tuple)

HERE = Path(__file__).resolve().parent
REGISTRY = json.loads((HERE / 'registry.json').read_text())
RUNS = json.loads((HERE / 'fixtures/reporter-results.json').read_text())['runs']
NOW = timestamp('2026-10-28T12:00:00Z')


def issue_for(action, number=42):
    return dict(number=number, body=action['body'], state=action['state'], labels=action['labels'], user=dict(REPORTER_USER))


def lifecycle(runs, issues=()):
    return reconcile(copy.deepcopy(runs), copy.deepcopy(issues), REGISTRY, NOW)


class ResumeRecoveryTests(unittest.TestCase):
    def test_invalid_unicode_in_owned_state_is_isolated_through_output_writing(self):
        import base64
        import tempfile
        import zlib
        from report import write_outputs
        first = lifecycle(RUNS[:2])['actions'][0]
        for version in (1, 2):
            for field in ('summary', 'owner', 'test_id'):
                state = parse_issue(first)
                scalar = chr(0xd800 if field != 'owner' else 0xdfff)
                if field == 'test_id':
                    next(iter(state['scopes'].values()))['failure']['test_ids'][0] += scalar
                else:
                    state[field] += scalar
                raw = json.dumps(state).encode('ascii')
                encoded = base64.urlsafe_b64encode(zlib.compress(raw) if version == 2 else raw).decode()
                issue = issue_for(first)
                issue['body'] = f'<!-- traceai-compat:v{version} {encoded} -->'
                with self.subTest(version=version, field=field), tempfile.TemporaryDirectory() as tmp:
                    self.assertIsNone(parse_issue(issue))
                    result = write_outputs(Path(tmp), [RUNS[-1]], REGISTRY, NOW, [issue])
                    self.assertIn('issue-state:42', [h['id'] for h in result['health']])
                    index = json.loads((Path(tmp) / 'results-index.json').read_text())
                    self.assertEqual([r['run_id'] for r in index['observations']], [RUNS[-1]['run']['run_id']])
                    self.assertIn(RUNS[-1]['run']['url'], (Path(tmp) / 'compatibility.md').read_text(encoding='utf-8'))

    def test_display_text_is_utf8_safe_even_for_in_memory_invalid_scalars(self):
        value = 'diagnostic ' + chr(0xd800) + chr(0xdfff)
        displayed = text(value)
        self.assertEqual(displayed, 'diagnostic ��')
        self.assertEqual(displayed.encode('utf-8').decode('utf-8'), displayed)

    def test_extreme_timezone_offsets_in_owned_state_are_isolated(self):
        import base64
        first = lifecycle(RUNS[:2])['actions'][0]
        for field in ('first_seen', 'evidence', 'watermark', 'last_known_good'):
            state = parse_issue(first)
            scope = next(iter(state['scopes'].values()))
            extreme = '9999-12-31T23:59:59-01:00'
            if field == 'first_seen':
                state['first_seen'] = state['last_seen'] = extreme
            elif field == 'evidence':
                scope['failure']['at'] = extreme
            elif field == 'watermark':
                scope['watermark'][3] = extreme
            else:
                state['last_known_good']['at'] = extreme
            issue = issue_for(first)
            issue['body'] = '<!-- traceai-compat:v1 ' + base64.urlsafe_b64encode(json.dumps(state).encode()).decode() + ' -->'
            with self.subTest(field=field):
                self.assertIsNone(parse_issue(issue))
                result = lifecycle([RUNS[1]], [issue])
                self.assertTrue(any(s['kind'] == 'health' for s in result['states']))
                self.assertTrue(any(s['kind'] == 'compatibility' for s in result['states']))

    def test_daily_recovery_uses_same_freshness_as_publication_including_persisted_passes(self):
        from datetime import timedelta
        from incidents import iso
        from publish import build_index
        def dated(template, at, mode, selector='latest_eligible', runtime='3.11.9'):
            bundle = copy.deepcopy(template)
            bundle['cases'][0].update(mode=mode, selector=selector, completed_at=at,
                                      started_at=iso(timestamp(at) - timedelta(minutes=10)))
            bundle['cases'][0]['runtime']['version'] = runtime
            return bundle
        for failure_mode in ('source', 'published'):
            for persisted in (False, True):
                failed = dated(RUNS[1], '2026-10-21T04:00:00Z', failure_mode)
                old = dated(RUNS[3], '2026-10-22T04:00:00Z', 'source')
                fresh = dated(RUNS[4], '2026-10-28T05:00:00Z', 'source')
                issue = issue_for(lifecycle([failed])['actions'][0])
                if persisted:
                    action = reconcile([old], [issue], REGISTRY, timestamp('2026-10-22T12:00:00Z'))['actions'][0]
                    issue = issue_for(action)
                    observations = [fresh]
                else:
                    observations = [old, fresh]
                self.assertEqual(build_index([old], REGISTRY, NOW)['observations'][0]['status'], 'stale')
                result = lifecycle(observations, [issue])
                state = next(s for s in result['states'] if s['kind'] == 'compatibility')
                with self.subTest(mode=failure_mode, persisted=persisted):
                    self.assertEqual(state['status'], 'open')
                    passes = [p['at'] for s in state['scopes'].values() for p in s['passes'] + s['source_passes']]
                    self.assertEqual(passes, ['2026-10-28T05:00:00Z'])
        # Weekly boundaries retain an older, still-fresh observation; daily Friday
        # evidence is still fresh before Sunday has actually missed its deadline.
        for runtime, old_at, new_at, now in [
                ('3.13.9', '2026-10-22T04:00:00Z', '2026-10-28T05:00:00Z', NOW),
                ('3.11.9', '2026-10-23T04:00:00Z', '2026-10-24T05:00:00Z', timestamp('2026-10-25T02:00:00Z'))]:
            failed = dated(RUNS[1], '2026-10-21T04:00:00Z', 'source', runtime=runtime)
            passes = [dated(RUNS[3], old_at, 'source', runtime=runtime), dated(RUNS[4], new_at, 'source', runtime=runtime)]
            issue = issue_for(reconcile([failed], [], REGISTRY, now)['actions'][0])
            self.assertEqual(reconcile(passes, [issue], REGISTRY, now)['states'][0]['status'], 'recovered')


class IncidentTests(unittest.TestCase):
    def test_repeats_update_one_incident_and_replay_is_noop(self):
        first = lifecycle(RUNS[:2])
        self.assertEqual(len(first['actions']), 1)
        issue = issue_for(first['actions'][0])
        second = lifecycle(RUNS[:3], [issue])
        self.assertEqual(len(second['actions']), 1)
        self.assertEqual(second['actions'][0]['number'], 42)
        self.assertEqual(second['actions'][0]['key'], first['actions'][0]['key'])
        updated = issue_for(second['actions'][0])
        self.assertEqual(lifecycle(RUNS[:3], [updated])['actions'], [])

    def test_two_source_runs_leave_published_failure_release_pending(self):
        issue = issue_for(lifecycle(RUNS[:2])['actions'][0])
        result = lifecycle(RUNS[:5], [issue])
        action = result['actions'][0]
        self.assertEqual(action['state'], 'open')
        self.assertEqual(parse_issue(action)['status'], 'release_pending')
        self.assertIn('compat:release-pending', action['labels'])

    def test_two_published_passes_close_and_replay_cannot_reopen(self):
        issue = issue_for(lifecycle(RUNS[:2])['actions'][0])
        one = lifecycle(RUNS[:6], [issue])['actions'][0]
        self.assertEqual(one['state'], 'open')
        recovered = lifecycle(RUNS, [issue_for(one)])['actions'][0]
        self.assertEqual(recovered['state'], 'closed')
        self.assertEqual(recovered['notification'], 'recovery')
        self.assertEqual(lifecycle(RUNS, [issue_for(recovered)])['actions'], [])

    def test_never_open_already_recovered_historical_incident(self):
        self.assertEqual(lifecycle(RUNS)['actions'], [])

    def test_same_run_rerun_does_not_count_twice(self):
        runs = copy.deepcopy(RUNS[:6])
        retry = copy.deepcopy(RUNS[6])
        retry['run']['run_id'] = runs[-1]['run']['run_id']
        retry['run']['run_attempt'] = 2
        runs.append(retry)
        issue = issue_for(lifecycle(RUNS[:2])['actions'][0])
        self.assertEqual(lifecycle(runs, [issue])['actions'][0]['state'], 'open')

    def test_old_single_pass_cannot_combine_with_new_run_to_recover(self):
        issue = issue_for(lifecycle(RUNS[:6])['actions'][0])
        state = parse_issue(issue)
        scope = next(iter(state['scopes'].values()))
        scope['passes'][0]['at'] = '2026-10-01T04:00:00Z'
        issue['body'] = body_for(state)
        result = lifecycle([RUNS[-1]], [issue])
        self.assertEqual(result['actions'][0]['state'], 'open')
        self.assertEqual(len(next(iter(result['states'][0]['scopes'].values()))['passes']), 1)

    def test_newer_upstream_does_not_erase_known_bad_tuple(self):
        runs = copy.deepcopy(RUNS)
        for bundle in runs[3:]:
            for p in bundle['cases'][0]['resolved']:
                if p['role'] == 'upstream':
                    p['version'] = '1.94.0'
        issue = issue_for(lifecycle(RUNS[:2])['actions'][0])
        state = lifecycle(runs, [issue])['states'][0]
        self.assertEqual(state['status'], 'open')

    def test_suite_runtime_mode_and_failed_test_coverage_are_comparable(self):
        for variation in ('suite', 'runtime', 'coverage', 'scenarios'):
            with self.subTest(variation=variation):
                runs = copy.deepcopy(RUNS)
                for bundle in runs[3:]:
                    case = bundle['cases'][0]
                    if variation == 'suite':
                        case['contract']['suite_sha256'] = 'b' * 64
                    elif variation == 'runtime':
                        case['runtime']['version'] = '3.13.0'
                    elif variation == 'coverage':
                        case['contract']['tests'][0]['test_id'] = 'some_other_test'
                    else:
                        case['contract']['scenarios'] = []
                state = lifecycle(runs)['states'][0]
                self.assertEqual(state['status'], 'open')

    def test_distinct_failures_do_not_collapse(self):
        runs = copy.deepcopy(RUNS[:3])
        runs[-1]['cases'][0]['failure']['signature'] = 'f' * 64
        self.assertEqual(len(lifecycle(runs)['actions']), 2)

    def test_complete_companion_tuple_and_python_normalization(self):
        case = copy.deepcopy(RUNS[1]['cases'][0])
        case['resolved'].append(dict(name='Foo_Bar', role='companion', version='2.0'))
        case['requested'].append(dict(name='foo.bar', role='companion', selected_version='2.0'))
        self.assertIn(('foo-bar', 'companion', '2.0'), upstream_tuple(case))
        self.assertNotEqual(incident_key(case), incident_key(RUNS[1]['cases'][0]))

    def test_classification_and_last_known_good_hint(self):
        action = lifecycle(RUNS[:2])['actions'][0]
        state = parse_issue(action)
        self.assertEqual(state['classification'], 'contract-regression')
        self.assertEqual(state['last_known_good']['run_id'], '1001')
        self.assertIn('Do not resolve a fresh latest', action['body'])
        case = copy.deepcopy(RUNS[1]['cases'][0])
        for kind, summary, expected in (
                ('resolution', 'resolver failed', 'resolution-install'),
                ('install', 'install failed', 'resolution-install'),
                ('infrastructure', 'namespace unavailable', 'infra'),
                ('contract', 'AttributeError: has no attribute send', 'upstream-api-change')):
            case['failure'].update(kind=kind, summary=summary)
            self.assertEqual(classify(case), expected)

    def test_repair_pr_guard_never_schedules_an_agent(self):
        first = lifecycle(RUNS[:2])
        issue = issue_for(first['actions'][0])
        key = first['actions'][0]['key']
        pr = dict(number=43, state='open', pull_request={}, body=f'<!-- traceai-compat-repair:{key} -->')
        result = lifecycle(RUNS[:3], [issue, pr])
        handoff = result['handoffs'][0]
        self.assertEqual(handoff['guard'], 'blocked_existing_pr')
        self.assertEqual(handoff['existing_prs'], [43])
        self.assertFalse(handoff['automatic_execution'])

    def test_public_marker_copy_cannot_block_or_supply_incident_state(self):
        action = lifecycle(RUNS[:2])['actions'][0]
        for labels in ([], ['compatibility']):
            copied = issue_for(action, 44)
            copied.update(user=dict(id=123, login='outsider', type='User'), labels=labels)
            result = lifecycle(RUNS[:3], [issue_for(action), copied])
            self.assertEqual(len(result['actions']), 1)
            self.assertEqual(result['actions'][0]['number'], 42)
            self.assertEqual(len(result['states']), 1)

    def test_conflicting_owned_records_isolate_only_their_key(self):
        action = lifecycle(RUNS[:2])['actions'][0]
        runs = copy.deepcopy(RUNS[:3])
        runs[-1]['cases'][0]['failure']['signature'] = 'f' * 64
        result = lifecycle(runs, [issue_for(action), issue_for(action, 44)])
        self.assertFalse(any(a['number'] in (42, 44) for a in result['actions']))
        self.assertTrue(any(s['kind'] == 'health' and 'Conflicting' in s['summary'] for s in result['states']))
        self.assertTrue(any(s.get('signature') == 'f' * 64 for s in result['states']))

    def test_incomplete_owned_state_is_isolated_without_aborting(self):
        action = lifecycle(RUNS[:2])['actions'][0]
        for field, value in [('scopes', None), ('tuple', []), ('last_seen', 'bad-date'), ('signature', 'bad')]:
            state = parse_issue(action)
            state[field] = value
            # Build the marker directly: malformed state is never a valid body input.
            import base64
            marker = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
            issue = issue_for(action)
            issue['body'] = '<!-- traceai-compat:v1 ' + marker + ' -->'
            self.assertIsNone(parse_issue(issue))
            result = lifecycle(RUNS[:3], [issue])
            self.assertTrue(any(s['kind'] == 'health' for s in result['states']))
            self.assertTrue(any(s['kind'] == 'compatibility' for s in result['states']))

    def test_resolution_failure_with_empty_suite_recovers_but_contract_failure_does_not(self):
        import hashlib
        failed = copy.deepcopy(RUNS[1])
        case = failed['cases'][0]
        case.update(resolved=[], dependency_lock=None, status='blocked')
        case['failure'].update(kind='resolution', test_ids=[])
        case['contract'].update(suite_sha256=hashlib.sha256(b'').hexdigest(), tests=[], collected=0, executed=0,
                                junit_paths=[], scenarios=[])
        issue = issue_for(lifecycle([failed])['actions'][0])
        result = lifecycle([RUNS[-2], RUNS[-1]], [issue])
        self.assertEqual(result['actions'][0]['state'], 'closed')
        case['failure']['kind'] = 'contract'
        issue = issue_for(lifecycle([failed])['actions'][0])
        self.assertEqual(lifecycle([RUNS[-2], RUNS[-1]], [issue])['states'][0]['status'], 'open')

    def test_repeated_published_failure_preserves_fresh_source_fix(self):
        pending = lifecycle(RUNS[:5])
        issue = issue_for(pending['actions'][0])
        failure = copy.deepcopy(RUNS[2])
        failure['run'].update(run_id='2000', url='https://github.com/future-agi/traceAI/actions/runs/2000')
        failure['cases'][0]['completed_at'] = '2026-10-28T06:00:00Z'
        result = lifecycle([failure], [issue])
        state = result['states'][0]
        self.assertEqual(state['status'], 'release_pending')
        self.assertEqual(len(next(iter(state['scopes'].values()))['source_passes']), 2)
        updated = issue_for(result['actions'][0])
        self.assertEqual(lifecycle([failure], [updated])['actions'], [])

    def test_source_failure_invalidates_source_fix_without_erasing_published_history(self):
        pending = lifecycle(RUNS[:5])
        source = copy.deepcopy(RUNS[2])
        source['cases'][0].update(mode='source', completed_at='2026-10-28T06:00:00Z')
        state = lifecycle([source], [issue_for(pending['actions'][0])])['states'][0]
        self.assertEqual(state['status'], 'open')
        self.assertTrue(all(not s['source_passes'] for s in state['scopes'].values()))

    def test_unseen_old_failure_survives_reporter_outage_and_is_stale_in_index(self):
        from publish import build_index
        failed = copy.deepcopy(RUNS[1])
        failed['cases'][0]['completed_at'] = '2026-10-19T04:00:00Z'
        failed['run']['completed_at'] = '2026-10-19T04:00:00Z'
        result = lifecycle([failed])
        self.assertEqual(result['actions'][0]['state'], 'open')
        self.assertEqual(build_index([failed], REGISTRY, NOW)['observations'][0]['status'], 'stale')

    def test_future_and_old_evidence_do_not_recover(self):
        runs = copy.deepcopy(RUNS)
        for bundle in runs[3:]:
            bundle['cases'][0]['completed_at'] = '2026-10-29T12:00:00Z'
        state = lifecycle(runs)['states'][0]
        self.assertEqual(state['status'], 'open')

    def test_large_multiruntime_recovery_is_lossless_bounded_and_idempotent(self):
        failed = copy.deepcopy(RUNS[1])
        failed['cases'] = []
        for mode, runtime in [('source', '3.10.9'), ('source', '3.11.9'),
                              ('source', '3.13.9'), ('published', '3.11.9')]:
            case = copy.deepcopy(RUNS[1]['cases'][0])
            case.update(mode=mode, runtime=dict(case['runtime'], version=runtime), case_id=mode + runtime)
            ids = ['python/frameworks/langchain/tests/test_instrumentor.py::test_real_graph_streaming_and_cancellation[' + str(i) + ']'
                   for i in range(55)]
            case['failure']['test_ids'] = ids
            case['contract']['tests'] = [dict(case['contract']['tests'][0], test_id=node) for node in ids]
            case['contract'].update(collected=55, executed=55)
            failed['cases'].append(case)
        first = lifecycle([failed])['actions'][0]
        self.assertLess(len(first['body'].encode()), 60000)
        issue = issue_for(first)
        from report import validate_bundle
        passes = []
        for template in RUNS[-2:]:
            bundle = copy.deepcopy(template)
            bundle['cases'] = copy.deepcopy(failed['cases'])
            bundle['run']['planned_case_ids'] = [c['case_id'] for c in bundle['cases']]
            for case in bundle['cases']:
                case.update(status='passed', failure=None, started_at=bundle['run']['started_at'], completed_at=bundle['run']['completed_at'])
                for node in case['contract']['tests']:
                    node.update(outcome='passed', failure_signature=None)
            validate_bundle(bundle, REGISTRY, NOW)
            passes.append(bundle)
        for count, bundle in enumerate(passes, 1):
            result = lifecycle([bundle], [issue])
            action = result['actions'][0]
            self.assertLess(len(action['body'].encode()), 60000)
            parsed = parse_issue(action)
            self.assertIsNotNone(parsed)
            self.assertEqual(len(parsed['scopes']), 4)
            self.assertTrue(all(len(s['failure']['test_ids']) == 55 for s in parsed['scopes'].values()))
            self.assertTrue(all(len(s['passes']) == count for s in parsed['scopes'].values()))
            issue = issue_for(action)
            self.assertEqual(lifecycle([bundle], [issue])['actions'], [])
        self.assertEqual(issue['state'], 'closed')

    def test_legacy_state_and_bounded_compressed_state_are_read_safely(self):
        import base64
        import zlib
        state = parse_issue(lifecycle(RUNS[:2])['actions'][0])
        marker = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
        legacy = dict(body='<!-- traceai-compat:v1 ' + marker + ' -->')
        self.assertEqual(parse_issue(legacy), state)
        for raw in (b'x' * (2 * 1024 * 1024 + 1), b'[' * 100 + b'0' + b']' * 100):
            marker = base64.urlsafe_b64encode(zlib.compress(raw)).decode()
            self.assertIsNone(parse_issue(dict(body='<!-- traceai-compat:v2 ' + marker + ' -->')))
        for raw in (zlib.compress(json.dumps(state).encode()) + b'trailing', b'not-zlib'):
            marker = base64.urlsafe_b64encode(raw).decode()
            self.assertIsNone(parse_issue(dict(body='<!-- traceai-compat:v2 ' + marker + ' -->')))

    def test_oversized_incident_is_health_not_global_reporter_failure(self):
        from unittest.mock import patch
        runs = copy.deepcopy(RUNS[:3])
        runs[-1]['cases'][0]['failure']['signature'] = 'f' * 64
        key = incident_key(runs[0]['cases'][0]) if runs[0]['cases'][0]['failure'] else incident_key(runs[1]['cases'][0])
        def bounded(state):
            if state['key'] == key:
                raise ValueError('oversized')
            return body_for(state)
        with patch('incidents.body_for', side_effect=bounded):
            result = lifecycle(runs)
        self.assertEqual(len(result['actions']), 2)
        self.assertTrue(any(s['kind'] == 'health' and 'exceeds' in s['summary'] for s in result['states']))
        self.assertTrue(any(s.get('signature') == 'f' * 64 for s in result['states']))
        self.assertFalse(any(h['incident_key'] == key for h in result['handoffs']))
        self.assertTrue(any(a['id'] == 'incident-size:' + key for a in result['health']))

    def test_missing_upload_uses_planned_not_collector_runtime_for_recovery(self):
        import hashlib
        failed = copy.deepcopy(RUNS[1])
        case = failed['cases'][0]
        case.update(status='blocked', resolved=[], dependency_lock=None)
        case['failure'].update(kind='automation', test_ids=[])
        case['contract'].update(suite_sha256=hashlib.sha256(b'').hexdigest(), tests=[], junit_paths=[], scenarios=[], collected=0, executed=0)
        identity = dict(case['runtime'], version='3.13.9')
        plans = {failed['run']['run_id']: dict(cases=[dict(case_id=case['case_id'], runtime='3.13', runtime_identity=identity)])}
        first = reconcile([failed], [], REGISTRY, NOW, plans=plans)['actions'][0]
        bad = next(iter(parse_issue(first)['scopes'].values()))['failure']
        self.assertEqual(bad['runtime']['version'], '3.11.9')
        self.assertEqual(bad['planned_runtime_identity'], identity)
        passes = copy.deepcopy(RUNS[-2:])
        for bundle in passes:
            bundle['cases'][0]['runtime'] = identity
        issue = issue_for(first)
        self.assertEqual(lifecycle(passes[:1], [issue])['states'][0]['status'], 'open')
        closed = lifecycle(passes, [issue])['actions'][0]
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(lifecycle(passes, [issue_for(closed)])['actions'], [])
        for variation in ('runtime', 'tuple', 'mode', 'arch'):
            wrong = copy.deepcopy(passes)
            for bundle in wrong:
                candidate = bundle['cases'][0]
                if variation == 'runtime':
                    candidate['runtime']['version'] = '3.10.9'
                elif variation == 'arch':
                    candidate['runtime']['arch'] = 'aarch64'
                elif variation == 'mode':
                    candidate['mode'] = 'source'
                else:
                    next(p for p in candidate['resolved'] if p['role'] == 'upstream')['version'] = '1.94.0'
            with self.subTest(variation=variation):
                expected = 'release_pending' if variation == 'mode' else 'open'
                self.assertEqual(lifecycle(wrong, [issue])['states'][0]['status'], expected)
        # No plan must not turn a collector runtime into an execution identity.
        unplanned = issue_for(lifecycle([failed])['actions'][0])
        self.assertEqual(lifecycle(RUNS[-2:], [unplanned])['states'][0]['status'], 'open')

    def test_mismatch_recovery_requires_intended_tuple_and_keeps_actual_observation(self):
        failed = copy.deepcopy(RUNS[1])
        case = failed['cases'][0]
        case['failure'].update(kind='requested_resolved_mismatch', test_ids=[])
        case['contract'].update(tests=[], junit_paths=[], collected=0, executed=0, scenarios=[])
        next(p for p in case['resolved'] if p['role'] == 'upstream')['version'] = '1.92.0'
        first = lifecycle([failed])['actions'][0]
        state = parse_issue(first)
        self.assertEqual(state['tuple'], [['openai', 'upstream', '1.93.1']])
        bad = next(iter(state['scopes'].values()))['failure']
        self.assertEqual(next(p for p in bad['resolved'] if p['role'] == 'upstream')['version'], '1.92.0')
        self.assertEqual(lifecycle(RUNS[-2:], [issue_for(first)])['actions'][0]['state'], 'closed')
        wrong = copy.deepcopy(RUNS[-2:])
        for bundle in wrong:
            next(p for p in bundle['cases'][0]['resolved'] if p['role'] == 'upstream')['version'] = '1.92.0'
        self.assertEqual(lifecycle(wrong, [issue_for(first)])['states'][0]['status'], 'open')
        # A mismatch with executed assertions is not an empty-suite exemption.
        case['contract'] = copy.deepcopy(RUNS[1]['cases'][0]['contract'])
        case['failure']['test_ids'] = [case['contract']['tests'][0]['test_id']]
        strict = issue_for(lifecycle([failed])['actions'][0])
        wrong = copy.deepcopy(RUNS[-2:])
        for bundle in wrong:
            bundle['cases'][0]['contract']['suite_sha256'] = 'e' * 64
        self.assertEqual(lifecycle(wrong, [strict])['states'][0]['status'], 'open')

    def test_release_upgrade_requires_replay_of_retained_failed_suite(self):
        from report import validate_bundle
        issue = issue_for(lifecycle(RUNS[:2])['actions'][0])
        upgraded = copy.deepcopy(RUNS[-2:])
        for bundle in upgraded:
            case = bundle['cases'][0]
            for package in case['resolved']:
                if package['role'] in ('instrumentation', 'core'):
                    package['version'] = '9.0.0'
                    next(p for p in case['requested'] if p['role'] == package['role']).update(
                        selected_version='9.0.0', requirement=package['name'] + '==9.0.0')
            case['contract']['suite_sha256'] = 'd' * 64
            validate_bundle(bundle, REGISTRY, NOW)
        self.assertEqual(lifecycle(upgraded, [issue])['states'][0]['status'], 'open')
        # A clean consumer may install a fixed instrumentation/core release while
        # replaying tests/configuration from the retained failing source snapshot.
        # The failed exact IDs/scenarios stay intact; no suite-evolution waiver.
        retained = RUNS[1]['cases'][0]['contract']['suite_sha256']
        for bundle in upgraded:
            bundle['cases'][0]['contract']['suite_sha256'] = retained
            validate_bundle(bundle, REGISTRY, NOW)
        result = lifecycle(upgraded, [issue])
        self.assertEqual(result['actions'][0]['state'], 'closed')
        self.assertEqual(lifecycle(upgraded, [issue_for(result['actions'][0])])['actions'], [])

    def test_mentions_and_hidden_markers_are_escaped_in_visible_text(self):
        self.assertNotIn('@', text('@someone <script>'))
        self.assertNotIn('<script>', text('@someone <script>'))


class HeartbeatTests(unittest.TestCase):
    def enabled(self):
        registry = copy.deepcopy(REGISTRY)
        registry['integrations'] = [t for t in registry['integrations'] if t['id'] == 'python-openai']
        return registry

    def test_missing_run_is_stale(self):
        alerts = heartbeat([], self.enabled(), NOW)
        self.assertTrue(any(a['id'] == 'heartbeat:python-openai:daily' for a in alerts))
        self.assertTrue(any(a['id'] == 'heartbeat:python-openai:weekly' for a in alerts))

    def test_selftest_never_satisfies_heartbeat(self):
        runs = copy.deepcopy(RUNS)
        for bundle in runs:
            bundle['run']['self_test'] = True
        self.assertEqual(heartbeat(runs, self.enabled(), NOW), heartbeat([], self.enabled(), NOW))

    def complete_weekly(self, at='2026-10-18T04:00:00Z'):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['cases'] = []
        for runtime in ('3.10', '3.11', '3.13'):
            for selector in ('floor', 'latest_eligible'):
                case = copy.deepcopy(RUNS[4]['cases'][0])
                case.update(mode='source', selector=selector, completed_at=at)
                case['runtime']['version'] = runtime + '.9'
                bundle['cases'].append(case)
        case = copy.deepcopy(RUNS[-1]['cases'][0])
        case['completed_at'] = at
        bundle['cases'].append(case)
        return bundle

    def test_weekend_after_complete_friday_and_sunday_is_not_stale(self):
        runs = [copy.deepcopy(RUNS[4]), self.complete_weekly()]
        runs[0]['cases'][0]['completed_at'] = '2026-10-23T04:00:00Z'
        for now in ('2026-10-24T18:00:00Z', '2026-10-25T02:00:00Z'):
            self.assertEqual(heartbeat(runs, self.enabled(), timestamp(now)), [])

    def test_one_source_floor_or_partial_weekly_cannot_clear_freshness(self):
        complete = self.complete_weekly('2026-10-27T04:00:00Z')
        self.assertEqual(heartbeat([complete], self.enabled(), NOW), [])
        for subset in ([complete['cases'][2]], complete['cases'][:-1], complete['cases'][1:]):
            bundle = copy.deepcopy(complete)
            bundle['cases'] = subset
            self.assertTrue(any(a['id'].endswith(':weekly') for a in heartbeat([bundle], self.enabled(), NOW)))

    def test_missing_case_placeholder_is_not_complete_weekly_evidence(self):
        bundle = self.complete_weekly('2026-10-27T04:00:00Z')
        case = bundle['cases'][0]
        case.update(status='blocked', failure=dict(kind='automation'), resolved=[], dependency_lock=None)
        case['contract'].update(executed=0, collected=0, tests=[], junit_paths=[])
        self.assertTrue(any(a['id'].endswith(':weekly') for a in heartbeat([bundle], self.enabled(), NOW)))

    def test_health_repeats_do_not_spam_and_fresh_health_closes(self):
        health = [dict(id='heartbeat:python-openai:weekly', summary='Missing complete weekly run')]
        first = reconcile([], [], REGISTRY, NOW, health)['actions'][0]
        issue = issue_for(first)
        self.assertEqual(reconcile([], [issue], REGISTRY, NOW, health)['actions'], [])
        # An absent alert without new execution is not a successful heartbeat.
        self.assertEqual(reconcile([], [issue], REGISTRY, NOW, [])['actions'], [])
        complete = self.complete_weekly('2026-10-27T04:00:00Z')
        closed = reconcile([complete], [issue], REGISTRY, NOW, [])['actions'][0]
        self.assertEqual(closed['state'], 'closed')

    def test_weekly_deadline_is_eight_days_since_last_complete_scope(self):
        complete = self.complete_weekly('2026-10-18T04:00:00Z')
        for at, stale in [('2026-10-25T23:00:00Z', False), ('2026-10-26T04:00:00Z', True),
                          ('2026-10-28T12:00:00Z', True)]:
            with self.subTest(at=at):
                alerts = heartbeat([complete], self.enabled(), timestamp(at))
                self.assertEqual(any(a['id'].endswith(':weekly') for a in alerts), stale)

    def test_health_omission_without_recovery_evidence_does_not_close(self):
        for ident in ('requested-run:1007', 'invalid-run:1007', 'unknown:incident'):
            alert = dict(id=ident, summary='Evidence unavailable')
            action = reconcile([], [], REGISTRY, NOW, [alert])['actions'][0]
            issue = issue_for(action)
            result = reconcile([], [issue], REGISTRY, NOW)
            self.assertEqual(next(s for s in result['states'] if s['key'] == action['key'])['status'], 'open')
            self.assertFalse(any(a['notification'] == 'recovery' for a in result['actions']))
            if ident.startswith(('requested-run:', 'invalid-run:')):
                # A different validated run is not recovery of this specific run.
                self.assertFalse(any(a['state'] == 'closed' for a in reconcile([RUNS[0]], [issue], REGISTRY, NOW)['actions']))
                recovered = reconcile([RUNS[-1]], [issue], REGISTRY, NOW)['actions']
                self.assertTrue(any(a['key'] == action['key'] and a['state'] == 'closed' for a in recovered))

    def test_legacy_run_health_requires_the_same_positive_validation(self):
        from incidents import body_for
        action = reconcile([], [], REGISTRY, NOW, [dict(id='invalid-run:1007', summary='Invalid artifact')])['actions'][0]
        legacy = parse_issue(action)
        legacy.pop('health_id')
        issue = issue_for(dict(action, body=body_for(legacy)))
        self.assertEqual(reconcile([], [issue], REGISTRY, NOW)['actions'], [])
        self.assertEqual(reconcile([RUNS[-1]], [issue], REGISTRY, NOW)['actions'][0]['state'], 'closed')

    def test_exception_health_needs_registry_removal_or_unexpired_replacement(self):
        health = [dict(id='exception:expired-test', summary='Expired exception')]
        issue = issue_for(reconcile([], [], REGISTRY, NOW, health)['actions'][0])
        registry = self.enabled()
        registry['integrations'][0]['exceptions'] = [dict(id='expired-test', expires='2026-10-28')]
        self.assertEqual(reconcile([], [issue], registry, NOW)['actions'], [])
        registry['integrations'][0]['exceptions'][0]['expires'] = '2026-10-29'
        self.assertEqual(reconcile([], [issue], registry, NOW)['actions'][0]['state'], 'closed')
        self.assertEqual(reconcile([], [issue], REGISTRY, NOW)['actions'][0]['state'], 'closed')

    def test_expired_exception_is_explicit(self):
        for expires in ('2026-10-27', '2026-10-28'):
            registry = self.enabled()
            registry['integrations'][0]['exceptions'] = [dict(id='expired-test', expires=expires)]
            self.assertTrue(any(a['id'] == 'exception:expired-test' for a in heartbeat([], registry, NOW)))


if __name__ == '__main__':
    unittest.main()
