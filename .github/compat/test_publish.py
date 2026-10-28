import copy
import json
from pathlib import Path
import unittest

from incidents import timestamp
from publish import build_index, release_notes, render_markdown
from report import schema_validate

HERE = Path(__file__).resolve().parent
REGISTRY = json.loads((HERE / 'registry.json').read_text())
RUNS = json.loads((HERE / 'fixtures/reporter-results.json').read_text())['runs']
NOW = timestamp('2026-10-28T12:00:00Z')


class PublishTests(unittest.TestCase):
    def test_exact_published_combination_and_last_checked_date(self):
        index = build_index(RUNS, REGISTRY, NOW, fixture=True)
        schema_validate(index, 'results-index.schema.json')
        page = render_markdown(index)
        self.assertIn("'traceAI-openai==0.1.10'", page)
        self.assertIn("'fi-instrumentation-otel==1.1.0'", page)
        self.assertIn("'openai==1.93.1'", page)
        self.assertIn('2026-10-28T05:00:00Z', page)
        self.assertIn('SYNTHETIC FIXTURE PREVIEW', page)
        self.assertIn('pilot_pending', page)
        self.assertEqual(len(index['inventory']), 146)

    def test_source_pass_is_not_advertised_as_installable_release(self):
        index = build_index([RUNS[4]], REGISTRY, NOW)
        self.assertIsNone(index['observations'][0]['install_command'])
        self.assertIn('Source rows are unreleased', render_markdown(index))

    def test_pending_target_cannot_self_promote_from_artifact(self):
        runs = copy.deepcopy(RUNS)
        runs[-1]['cases'][0]['qualification'] = 'qualified'
        index = build_index(runs, REGISTRY, NOW)
        self.assertEqual({r['qualification'] for r in index['observations']}, {'pilot_pending'})

    def test_selftest_and_stale_evidence_are_visible(self):
        runs = copy.deepcopy(RUNS)
        for bundle in runs:
            bundle['run']['self_test'] = True
        page = render_markdown(build_index(runs, REGISTRY, NOW, fixture=True))
        self.assertIn('self-test, not a support claim', page)
        self.assertIn('stale', page)
        page = render_markdown(build_index(RUNS, REGISTRY, timestamp('2026-11-10T12:00:00Z')))
        self.assertNotIn('```sh', page)

    def test_known_bad_tuple_preserved_when_different_tuple_passes(self):
        runs = copy.deepcopy(RUNS[:3])
        passing = copy.deepcopy(RUNS[-1])
        for p in passing['cases'][0]['resolved']:
            if p['role'] == 'upstream':
                p['version'] = '1.94.0'
        runs.append(passing)
        index = build_index(runs, REGISTRY, NOW)
        self.assertTrue(any(r['observed_status'] == 'failed' for r in index['observations']))
        self.assertTrue(any(r['observed_status'] == 'passed' for r in index['observations']))

    def test_distinct_unresolved_selected_tuples_survive_in_index(self):
        runs = copy.deepcopy(RUNS[:2])
        for i, bundle in enumerate(runs):
            case = bundle['cases'][0]
            case.update(resolved=[], dependency_lock=None, status='blocked', failure=copy.deepcopy(RUNS[1]['cases'][0]['failure']))
            case['requested'][0]['selected_version'] = ('1.69.0', '1.93.1')[i]
            case['requested'][0]['requirement'] = 'openai==' + case['requested'][0]['selected_version']
            case['contract']['suite_sha256'] = 'a' * 64
        index = build_index(runs, REGISTRY, NOW)
        self.assertEqual(len(index['observations']), 2)
        self.assertEqual({row['requested'][0]['selected_version'] for row in index['observations']}, {'1.69.0', '1.93.1'})
        self.assertTrue(all(row['install_command'] is None for row in index['observations']))

    def test_incomplete_weekly_coverage_does_not_erase_fresh_exact_published_pass(self):
        index = build_index([RUNS[-1]], REGISTRY, NOW)
        self.assertTrue(any(a['id'].endswith(':weekly') for a in index['health']))
        row = index['observations'][0]
        self.assertEqual(row['status'], 'passed')
        self.assertIn('openai==1.93.1', row['install_command'])

    def test_boundary_freshness_uses_own_weekly_scope_not_missing_daily_runtime(self):
        for selector, runtime in [('latest_eligible', '3.13.9'), ('floor', '3.11.9')]:
            bundle = copy.deepcopy(RUNS[-1])
            case = bundle['cases'][0]
            case.update(mode='source', selector=selector)
            case['runtime']['version'] = runtime
            index = build_index([bundle], REGISTRY, NOW)
            self.assertTrue(any(a['id'].endswith(':daily') for a in index['health']))
            self.assertEqual(index['observations'][0]['status'], 'passed')
            # A two-day boundary pass remains fresh, but nine days is stale.
            case['completed_at'] = '2026-10-26T05:00:00Z'
            self.assertEqual(build_index([bundle], REGISTRY, NOW)['observations'][0]['status'], 'passed')
            case['completed_at'] = '2026-10-19T05:00:00Z'
            self.assertEqual(build_index([bundle], REGISTRY, NOW)['observations'][0]['status'], 'stale')

    def test_weekly_observation_ages_out_at_the_eight_day_deadline(self):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['cases'][0]['completed_at'] = '2026-10-20T12:00:00Z'
        self.assertEqual(build_index([bundle], REGISTRY, NOW)['observations'][0]['status'], 'stale')

    def test_daily_observation_due_grace_and_weekend_are_per_scope(self):
        bundle = copy.deepcopy(RUNS[4])
        case = bundle['cases'][0]
        for checked, now, expected in [
                ('2026-10-26T04:00:00Z', '2026-10-28T16:00:00Z', 'stale'),
                ('2026-10-27T04:00:00Z', '2026-10-28T12:00:00Z', 'passed'),
                ('2026-10-23T04:00:00Z', '2026-10-25T02:00:00Z', 'passed'),
                ('2026-10-23T04:00:00Z', '2026-10-26T16:00:00Z', 'stale')]:
            case['completed_at'] = checked
            with self.subTest(checked=checked, now=now):
                self.assertEqual(build_index([bundle], REGISTRY, timestamp(now))['observations'][0]['status'], expected)

    def test_recovered_source_note_does_not_invent_outstanding_release_obligation(self):
        from incidents import reconcile
        self.assertEqual(reconcile(RUNS, [], REGISTRY, NOW)['states'][0]['status'], 'recovered')
        notes = release_notes(build_index(RUNS, REGISTRY, NOW))
        source = next(i for i in notes['inputs'] if i['mode'] == 'source')
        self.assertTrue(source['source_build_unreleased'])
        self.assertNotIn('release_pending', source)

    def test_release_note_inputs_need_human_confirmation(self):
        notes = release_notes(build_index(RUNS, REGISTRY, NOW, fixture=True))
        self.assertTrue(notes['requires_maintainer_review'])
        self.assertTrue(any(i['source_build_unreleased'] for i in notes['inputs']))
        self.assertTrue(all('release_pending' not in i for i in notes['inputs']))
        self.assertTrue(all('exact_packages' in i and 'confirm_before_release' in i for i in notes['inputs']))

    def test_different_environment_and_build_identities_do_not_overwrite_failures(self):
        for field in ('lock', 'source', 'artifact'):
            failed, passed = copy.deepcopy(RUNS[1]), copy.deepcopy(RUNS[-1])
            failed['cases'][0]['mode'] = passed['cases'][0]['mode'] = 'source'
            if field == 'lock':
                passed['cases'][0]['dependency_lock']['sha256'] = 'b' * 64
            elif field == 'source':
                passed['cases'][0]['source_sha'] = passed['run']['source_sha'] = 'b' * 40
            else:
                passed['cases'][0]['resolved'][0]['artifact_sha256'] = 'b' * 64
            with self.subTest(field=field):
                rows = build_index([failed, passed], REGISTRY, NOW)['observations']
                self.assertEqual(len(rows), 2)
                self.assertEqual({r['observed_status'] for r in rows}, {'failed', 'passed'})

    def test_fixture_golden_matches(self):
        page = render_markdown(build_index(RUNS, REGISTRY, NOW, fixture=True))
        self.assertEqual(page, (HERE / 'fixtures/expected-compatibility.md').read_text())

    def test_baseline_contains_no_invented_observation(self):
        page = render_markdown(build_index([], REGISTRY, NOW), baseline=True)
        self.assertIn('No observed combinations yet', page)
        self.assertNotIn('```sh', page)
        self.assertNotIn('Generated:', page)
        self.assertEqual(page, (HERE.parents[1] / 'docs/compatibility.md').read_text())


if __name__ == '__main__':
    unittest.main()
