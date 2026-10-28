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
        health = [dict(id='heartbeat:example:daily', summary='Missing example run')]
        first = reconcile([], [], REGISTRY, NOW, health)['actions'][0]
        issue = issue_for(first)
        self.assertEqual(reconcile([], [issue], REGISTRY, NOW, health)['actions'], [])
        closed = reconcile([], [issue], REGISTRY, NOW, [])['actions'][0]
        self.assertEqual(closed['state'], 'closed')

    def test_expired_exception_is_explicit(self):
        for expires in ('2026-10-27', '2026-10-28'):
            registry = self.enabled()
            registry['integrations'][0]['exceptions'] = [dict(id='expired-test', expires=expires)]
            self.assertTrue(any(a['id'] == 'exception:expired-test' for a in heartbeat([], registry, NOW)))


if __name__ == '__main__':
    unittest.main()
