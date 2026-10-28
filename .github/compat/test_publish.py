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

    def test_release_note_inputs_need_human_confirmation(self):
        notes = release_notes(build_index(RUNS, REGISTRY, NOW, fixture=True))
        self.assertTrue(notes['requires_maintainer_review'])
        self.assertTrue(any(i['release_pending'] for i in notes['inputs']))
        self.assertTrue(all('exact_packages' in i and 'confirm_before_release' in i for i in notes['inputs']))

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
