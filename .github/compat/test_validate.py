"""Registry/evidence and adversarial exact-exception tests (no SDK or network)."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from validate import (ROOT, load_registry, validate_registry, validate_results,
                      judge_tests, failure_signature, junit_tests, safe_path)


def fixture():
    data = json.loads((ROOT / '.github/compat/fixtures/validation.json').read_text())
    for ex in data['target']['exceptions']:
        ex['added'] = date.today().isoformat()
        ex['expires'] = (date.today() + timedelta(days=7)).isoformat()
    return data


def result_fixture():
    now = '2026-10-28T09:00:00Z'
    registry = load_registry(ROOT)
    target = next(t for t in registry['integrations'] if t['id'] == 'python-anthropic')
    requested = [dict(name=name, role=role, requirement='==' + version, selected_version=version)
                 for name, role, version in [('anthropic', 'upstream', '0.41.0'),
                    ('traceAI-anthropic', 'instrumentation', '0.1.10'), ('fi-instrumentation-otel', 'core', '1.2.1')]]
    case = dict(case_id='python-anthropic-floor', integration_id=target['id'], language='python',
                source_sha='1'*40, mode='source', selector='floor', qualification='pilot_pending',
                runtime=dict(implementation='cpython', version='3.11.9', os='linux', arch='x86_64'),
                requested=requested, resolved=[dict(name=r['name'], role=r['role'], version=r['selected_version']) for r in requested],
                discovery=[], dependency_lock=dict(format='pip-freeze', path='locks/freeze.txt', sha256='a'*64),
                contract=dict(suite_sha256='a'*64, command=['python', '-m', 'pytest'], scenarios=['sync'],
                              junit_paths=['junit/tests.xml'], tests=[dict(test_id='.github/compat/contracts/test_anthropic.py::test_sync',
                              outcome='passed', duration_seconds=0.1, failure_signature=None, exception_id=None)],
                              collected=1, executed=1, unexpected_skips=0),
                status='passed', failure=None, started_at=now, completed_at=now, attempt=1,
                reproduction=['python', '-m', 'pytest'], notes=[])
    run = dict(repository='future-agi/traceAI', run_id='fixture', run_attempt=1, workflow='Compatibility canary',
               event='self_test', url='https://example.invalid/run', source_sha='1'*40, workflow_sha='2'*40,
               plan_sha256='a'*64, planned_case_ids=[case['case_id']], started_at=now, completed_at=now,
               cooldown_hours=72, cooldown_cutoff='2026-10-25T09:00:00Z', self_test=True)
    return dict(schema_version=1, run=run, cases=[case]), registry


class RegistryTests(unittest.TestCase):
    def test_full_inventory_and_no_fabricated_quarantines(self):
        registry = load_registry(ROOT)
        self.assertEqual(len(registry['integrations']), 146)
        self.assertFalse(any(t['exceptions'] for t in registry['integrations']))
        self.assertTrue(all(t['qualification']['status'] != 'qualified' for t in registry['integrations']))

    def test_duplicates_and_missing_packages_fail(self):
        data = load_registry(ROOT)
        data['integrations'].append(copy.deepcopy(data['integrations'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_registry(data, ROOT)
        data = load_registry(ROOT)
        data['integrations'] = [t for t in data['integrations'] if t['id'] != 'python-google-genai']
        with self.assertRaisesRegex(ValueError, 'inventory drift'):
            validate_registry(data, ROOT)

    def test_declaration_and_runtime_drift_fail(self):
        for key in ['declared_range', 'runtime_range']:
            data = load_registry(ROOT)
            target = next(t for t in data['integrations'] if t['id'] == 'python-openai')
            target['upstream'][0][key] = '>=999'
            with self.assertRaisesRegex(ValueError, 'drift'):
                validate_registry(data, ROOT)
        data = load_registry(ROOT)
        data['integrations'][0]['runtimes']['declared'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'drift'):
            validate_registry(data, ROOT)

    def test_contract_free_target_cannot_be_qualified(self):
        data = load_registry(ROOT)
        t = next(t for t in data['integrations'] if not t['contract_test'])
        t['qualification']['status'] = 'qualified'
        with self.assertRaisesRegex(ValueError, 'requires execution evidence'):
            validate_registry(data)

    def test_bad_paths_and_symlink_rejected(self):
        for path in ['../escape', '/tmp/escape', 'a/../../escape', 'a\\b']:
            with self.assertRaises(ValueError):
                safe_path(ROOT, path)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                safe_path(Path(tmp), 'link/secret')


class ScopeTests(unittest.TestCase):
    def judge(self, data):
        return judge_tests(data['scoped'], data['target'], data['runtime'], data['resolved'])

    def test_exact_failure_is_degraded_not_compatible(self):
        self.assertEqual(self.judge(fixture()), 'quarantined')

    def test_unrelated_failure_in_excepted_package_is_red(self):
        data = fixture()
        data['scoped'].append({**data['scoped'][0], 'test_id': 'python/frameworks/fixture/tests/test_contract.py::test_unrelated'})
        with self.assertRaisesRegex(ValueError, 'unmatched failure'):
            self.judge(data)

    def test_signature_outcome_and_scope_must_match(self):
        for mutation in ['signature', 'upstream', 'runtime', 'outcome', 'param']:
            data = fixture()
            if mutation == 'signature': data['scoped'][0]['failure_signature'] = 'b'*64
            if mutation == 'upstream': data['resolved'][0]['version'] = '2.0.0'
            if mutation == 'runtime': data['runtime']['version'] = '3.12.0'
            if mutation == 'outcome': data['scoped'][0]['outcome'] = 'error'
            if mutation == 'param': data['scoped'][0]['test_id'] = data['scoped'][0]['test_id'].replace('[param]', '[other]')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.judge(data)

    def test_empty_skipped_unexpected_pass_and_missing_expected_test_fail(self):
        for rows in [[], [{**fixture()['scoped'][0], 'outcome': 'skipped'}],
                     [{**fixture()['scoped'][0], 'outcome': 'passed'}],
                     [{**fixture()['scoped'][0], 'test_id': 'other::test_other', 'outcome': 'passed'}]]:
            data = fixture(); data['scoped'] = rows
            with self.assertRaises(ValueError): self.judge(data)

    def test_expiry_long_duration_and_future_exception_fail(self):
        for added, expires in [(-8, -1), (0, 15), (1, 7)]:
            data = fixture(); ex = data['target']['exceptions'][0]
            ex['added'] = (date.today() + timedelta(days=added)).isoformat()
            ex['expires'] = (date.today() + timedelta(days=expires)).isoformat()
            with self.assertRaisesRegex(ValueError, 'expired, future or over-14-day'): self.judge(data)

    def test_cli_fixture_gates_use_real_verdict(self):
        for kind in ['scoped', 'execution']:
            result = subprocess.run([sys.executable, str(ROOT/'.github/compat/validate.py'), 'fixture-gate', kind], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('quarantined', result.stdout)

    def test_signature_normalizes_paths_not_behaviors(self):
        a = failure_signature('failure', ['a::b'], 'AssertionError: /home/runner/work/repo/value 0xabc')
        b = failure_signature('failure', ['a::b'], 'AssertionError: /tmp/other/value 0xdef')
        self.assertEqual(a, b)
        self.assertNotEqual(a, failure_signature('failure', ['a::b'], 'AssertionError: different behavior'))

    def test_junit_preserves_parameter_and_class_node_id(self):
        xml = '<testsuites><testsuite><testcase file="python/frameworks/x/tests/test_sdk.py" classname="python.frameworks.x.tests.test_sdk.TestSDK" name="test_call[param]" time="0.1"/></testsuite></testsuites>'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'junit.xml'; path.write_text(xml)
            self.assertEqual(junit_tests(path)[0]['test_id'], 'python/frameworks/x/tests/test_sdk.py::TestSDK::test_call[param]')


class ResultTests(unittest.TestCase):
    def test_valid_pending_evidence_stays_pending(self):
        data, registry = result_fixture()
        self.assertIs(validate_results(data, registry), data)
        self.assertEqual(data['cases'][0]['qualification'], 'pilot_pending')

    def test_unexecuted_integration_cannot_pass(self):
        data, registry = result_fixture(); c = data['cases'][0]['contract']
        c.update(tests=[], executed=0, collected=0)
        with self.assertRaisesRegex(ValueError, 'nonzero execution'):
            validate_results(data, registry)

    def test_missing_unplanned_duplicate_and_source_mismatch(self):
        for mutation in ['missing', 'unplanned', 'duplicate', 'sha']:
            data, registry = result_fixture()
            if mutation == 'missing': data['cases'] = []
            if mutation == 'unplanned': data['cases'][0]['case_id'] = 'rogue'
            if mutation == 'duplicate': data['cases'] *= 2
            if mutation == 'sha': data['cases'][0]['source_sha'] = '3'*40
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): validate_results(data, registry)

    def test_requested_resolved_skip_failure_and_count_mismatch(self):
        for mutation in ['requested', 'skip', 'failure', 'count', 'lock', 'core', 'qualification']:
            data, registry = result_fixture(); c = data['cases'][0]
            if mutation == 'requested': c['requested'][0]['selected_version'] = '999'
            if mutation == 'skip': c['contract']['tests'][0]['outcome'] = 'skipped'
            if mutation == 'failure': c['contract']['tests'][0].update(outcome='failure', failure_signature='b'*64)
            if mutation == 'count': c['contract']['executed'] = 0
            if mutation == 'lock': c['dependency_lock'] = None
            if mutation == 'core': c['resolved'] = [r for r in c['resolved'] if r['role'] != 'core']
            if mutation == 'qualification': c['qualification'] = 'qualified'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): validate_results(data, registry)

    def test_retry_cannot_re_resolve_or_launder_flake(self):
        data, registry = result_fixture()
        data['cases'].append(copy.deepcopy(data['cases'][0])); data['cases'][1]['attempt'] = 2
        data['cases'][0]['status'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'flaky'): validate_results(data, registry)
        data['cases'][1]['status'] = 'flaky'
        validate_results(data, registry)
        data['cases'][1]['requested'][0]['selected_version'] = '999'
        with self.assertRaisesRegex(ValueError, 'immutable'): validate_results(data, registry)


if __name__ == '__main__':
    unittest.main()
