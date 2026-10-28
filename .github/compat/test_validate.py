"""Registry/evidence and adversarial exact-exception tests (no SDK or network)."""
import copy
from datetime import date, timedelta
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from validate import (ROOT, load_registry, validate_registry, validate_results,
                      judge_tests, failure_signature, junit_tests, safe_path,
                      suite_digest, expected_test_nodes, invocation_tests, requested_version,
                      installed_distributions, native_distribution, sandbox_run)


def fixture():
    data = json.loads((ROOT / '.github/compat/fixtures/validation.json').read_text())
    for ex in data['target']['exceptions']:
        ex['added'] = date.today().isoformat()
        ex['expires'] = (date.today() + timedelta(days=7)).isoformat()
    return data


def write_invocations(case, artifacts, plan_sha):
    directory = artifacts / 'invocations'
    directory.mkdir(exist_ok=True)
    collection = artifacts / 'locks/collection.json'
    collection.write_text(json.dumps(dict(nodes=sorted(t['test_id'] for t in case['contract']['tests']), deselected=[], errors=[])))
    def record(relative):
        path = artifacts / relative
        return dict(exit_code=0, timed_out=False, started_at=case['started_at'], completed_at=case['completed_at'],
                    evidence_path=relative, evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest = dict(schema_version=1, case_id=case['case_id'], attempt=case['attempt'], source_sha=case['source_sha'],
                    plan_sha256=plan_sha, collection=record('locks/collection.json'), contract=record(case['contract']['junit_paths'][0]))
    path = directory / f'{case["case_id"]}-attempt-{case["attempt"]}.json'
    path.write_text(json.dumps(manifest))
    return path


def result_fixture(artifact_root=None):
    now = '2026-10-28T09:00:00Z'
    registry = load_registry(ROOT)
    target = next(t for t in registry['integrations'] if t['id'] == 'python-anthropic')
    requested = [dict(name=name, role=role, requirement='==' + version, selected_version=version)
                 for name, role, version in [('anthropic', 'upstream', '0.41.0'),
                    ('traceAI-anthropic', 'instrumentation', '0.1.10'), ('fi-instrumentation-otel', 'core', '1.2.1')]]
    case = dict(case_id='python-anthropic-floor', integration_id=target['id'], language='python',
                source_sha='1'*40, mode='source', selector='floor', qualification='pilot_pending',
                runtime=dict(implementation='cpython', version='3.11.9', os='linux', arch='x86_64'),
                requested=requested, resolved=[dict(name=r['name'], role=r['role'], version=r['selected_version'],
                    artifact_sha256=hashlib.sha256(r['name'].encode()).hexdigest()) for r in requested],
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
    contract = target['contract_test']
    nodes = sorted(expected_test_nodes(ROOT, contract))
    case['contract'].update(command=contract['command'], suite_sha256=suite_digest(ROOT, contract),
                            scenarios=contract['scenarios'], collected=len(nodes), executed=len(nodes),
                            tests=[dict(test_id=node, outcome='passed', duration_seconds=0.1,
                                        failure_signature=None, exception_id=None) for node in nodes])
    if artifact_root is not None:
        artifact_root = Path(artifact_root)
        (artifact_root / 'locks').mkdir(exist_ok=True)
        (artifact_root / 'junit').mkdir(exist_ok=True)
        lock = ('\n'.join(r['name'] + '==' + r['version'] for r in case['resolved']) + '\ntransitive==1.0\n').encode()
        (artifact_root / 'locks/freeze.txt').write_bytes(lock)
        case['dependency_lock']['sha256'] = hashlib.sha256(lock).hexdigest()
        root = ET.Element('testsuites')
        suite = ET.SubElement(root, 'testsuite')
        for node in nodes:
            path, name = node.split('::')
            ET.SubElement(suite, 'testcase', file=path, classname='test_anthropic', name=name, time='0.1')
        ET.ElementTree(root).write(artifact_root / 'junit/tests.xml')
        (artifact_root / 'junit/tests.xml.exit-code').write_text('0\n')
        write_invocations(case, artifact_root, run['plan_sha256'])
    return dict(schema_version=1, run=run, cases=[case]), registry


def plan_fixture(data, artifacts):
    c = data['cases'][0]
    distributions = [*c['resolved'], dict(name='transitive', version='1.0', artifact_sha256='b'*64)]
    report = dict(version='1', install=[dict(metadata=dict(name=r['name'], version=r['version']),
        download_info=dict(archive_info=dict(hashes=dict(sha256=r['artifact_sha256'])))) for r in distributions])
    report_bytes = json.dumps(report).encode()
    req_bytes = ('\n'.join(sorted(r['name'].lower().replace('_', '-') + '==' + r['version'] +
        ' --hash=sha256:' + r['artifact_sha256'] for r in distributions)) + '\n').encode()
    (artifacts / 'locks/resolution.report.json').write_bytes(report_bytes)
    (artifacts / 'locks/resolution.txt').write_bytes(req_bytes)
    resolution = dict(resolved=c['resolved'], report='locks/resolution.report.json',
        report_sha256=hashlib.sha256(report_bytes).hexdigest(), requirements='locks/resolution.txt',
        requirements_sha256=hashlib.sha256(req_bytes).hexdigest())
    plan = {key: data['run'][key] for key in ('source_sha', 'workflow_sha', 'cooldown_hours', 'cooldown_cutoff', 'self_test')}
    plan['cases'] = [{**{key: c[key] for key in ('case_id', 'integration_id', 'mode', 'selector', 'requested', 'discovery')},
                      'runtime': '3.11', 'problems': [], 'resolution': resolution}]
    raw = json.dumps(plan).encode()
    data['run']['plan_sha256'] = hashlib.sha256(raw).hexdigest()
    for case in data['cases']:
        if case['contract']['junit_paths']:
            write_invocations(case, artifacts, data['run']['plan_sha256'])
    return json.loads(raw), raw


class SandboxTests(unittest.TestCase):
    def test_namespace_failure_never_falls_back_to_host(self):
        with tempfile.TemporaryDirectory() as tmp, patch('validate.sys.platform', 'linux'), patch('validate.subprocess.run') as run:
            run.return_value.returncode = 90
            command = ['/trusted/venv/bin/python', '-I', '-m', 'pytest', 'test_contract.py']
            self.assertEqual(sandbox_run(command, ROOT, Path(tmp) / 'probe.json'), 90)
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(argv[:5], ['sudo', '--non-interactive', 'unshare', '--net', '--'])
            self.assertEqual(argv[-len(command):], command)
            self.assertIn('--no-new-privs', argv[7])
            self.assertIn('child_external', argv[-len(command)-3])


class RegistryTests(unittest.TestCase):
    def test_production_inventory_is_complete_and_valid(self):
        # Inventory completeness is verified against discovered manifests, not a
        # permanent count/status veto on additions, exceptions or qualification.
        load_registry(ROOT)

    def test_frozen_initial_snapshot(self):
        snapshot = json.loads((ROOT / '.github/compat/fixtures/validation.json').read_text())['initial_snapshot']
        self.assertEqual(snapshot, {'foundation_sha': '64e3a57ad273559849bbab0b8125bc57c7e7592e',
                                   'integrations': 146, 'exceptions': 0, 'qualified': 0})

    def test_owned_exception_and_qualification_are_not_prohibited(self):
        registry = load_registry(ROOT)
        target = next(t for t in registry['integrations'] if t['id'] == 'python-anthropic')
        ex = fixture()['target']['exceptions'][0]
        ex.update(id='fixture-review-only', test_ids=['.github/compat/contracts/test_anthropic.py::test_error_propagation'],
                  upstream_constraints=[{'name': 'anthropic', 'specifier': '==0.41.0'}])
        target['exceptions'] = [ex]
        validate_registry(registry, ROOT)
        target['exceptions'] = []
        # Synthetic registry only: prove the test harness permits the transition.
        target['qualification'].update(status='qualified', evidence=['fixture-only-execution-evidence'])
        target['upstream'][0]['runtime_range_source'] = None
        validate_registry(registry, ROOT)

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

    def test_native_identity_drift_and_inherited_maven_group(self):
        for language in ('java', 'csharp'):
            registry = load_registry(ROOT)
            target = next(t for t in registry['integrations'] if t['language'] == language)
            target['distribution'] = 'wrong:identity' if language == 'java' else 'wrong-identity'
            with self.subTest(language=language), self.assertRaisesRegex(ValueError, 'distribution drift'):
                validate_registry(registry, ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Inherited.csproj'
            path.write_text('<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>')
            self.assertEqual(native_distribution(path, 'csharp'), 'Inherited')
            path.write_text('<Project><PropertyGroup><AssemblyName>Assembly</AssemblyName></PropertyGroup></Project>')
            self.assertEqual(native_distribution(path, 'csharp'), 'Assembly')
            pom = Path(tmp) / 'pom.xml'
            pom.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>org.parent</groupId></parent><artifactId>child</artifactId></project>')
            self.assertEqual(native_distribution(pom, 'java'), 'org.parent:child')

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


def pytest_report(root, relative, source, label, *, runner_failure=False):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    if runner_failure:
        (Path(root) / 'conftest.py').write_text('def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n')
    report = Path(root) / (label + '.xml')
    run = subprocess.run([sys.executable, '-I', '-m', 'pytest', str(path), '-c', '/dev/null',
                          '--rootdir', str(root), '-o', 'junit_family=xunit1', '--junitxml', str(report),
                          '-p', 'no:cacheprovider'], cwd=root,
                         env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
                         capture_output=True, text=True)
    if not report.is_file():
        raise AssertionError(run.stdout + run.stderr)
    return report, run.returncode


class ScopeTests(unittest.TestCase):
    def judge(self, data):
        return judge_tests(data['scoped'], data['target'], data['runtime'], data['resolved'])

    def test_exact_failure_is_degraded_not_compatible(self):
        self.assertEqual(self.judge(fixture()), 'quarantined')

    def test_parameterized_exceptions_require_independent_nodes_and_signatures(self):
        data = fixture()
        original = data['target']['exceptions'][0]
        other_node = original['test_ids'][0].replace('[param]', '[other]')
        other_signature = failure_signature('failure', [other_node], 'RuntimeError: independent')
        other = {**original, 'id': original['id'] + '-other', 'test_ids': [other_node], 'failure_signature': other_signature}
        data['target']['exceptions'].append(other)
        data['scoped'].append({**data['scoped'][0], 'test_id': other_node, 'failure_signature': other_signature})
        self.assertEqual(self.judge(data), 'quarantined')
        data['scoped'].pop()
        with self.assertRaisesRegex(ValueError, 'did not execute'): self.judge(data)
        original['test_ids'].append(other_node)
        with self.assertRaisesRegex(ValueError, 'exactly one'): self.judge(data)

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

    def test_real_pytest_different_failure_in_same_excepted_node_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = 'python/frameworks/fixture/tests/test_contract.py'
            for error in ('RuntimeError("old SDK incompatibility")', 'TypeError("unrelated new bug")',
                          'RuntimeError("different incompatibility")'):
                report, rc = pytest_report(tmp, rel, 'def test_call():\n    assert True\n    raise ' + error + '\n', str(len(error)))
                self.assertEqual(rc, 1)
                rows = junit_tests(report)
                if error.startswith('RuntimeError("old'):
                    data = fixture()
                    data['target']['exceptions'][0].update(test_ids=[rows[0]['test_id']], failure_signature=rows[0]['failure_signature'])
                    original = rows[0]['failure_signature']
                    self.assertEqual(judge_tests(rows, data['target'], data['runtime'], data['resolved']), 'quarantined')
                else:
                    self.assertNotEqual(original, rows[0]['failure_signature'])
                    with self.assertRaisesRegex(ValueError, 'unmatched signature'):
                        judge_tests(rows, data['target'], data['runtime'], data['resolved'])

    def test_each_real_pytest_invocation_must_explain_its_own_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, rc_good = pytest_report(tmp, 'test_own.py', 'def test_pass():\n    assert True\n', 'own', runner_failure=True)
            failed, rc_failed = pytest_report(tmp, 'test_core.py', 'def test_known():\n    raise RuntimeError("known")\n', 'core')
            self.assertEqual((rc_good, rc_failed), (1, 1))
            # Max(exit)=1 plus a known failure used to hide the first runner failure.
            with self.assertRaisesRegex(ValueError, 'runner/JUnit outcome mismatch'):
                invocation_tests([good, failed], [rc_good, rc_failed])
            self.assertEqual(len(invocation_tests([good, failed], [0, 1])), 2)
            with self.assertRaisesRegex(ValueError, 'each invocation'):
                invocation_tests([good, failed], [1])

    def test_junit_rejects_empty_malformed_inconsistent_and_unsafe_reports(self):
        samples = ['<testsuites/>', 'not xml', '<html/>',
                   '<!DOCTYPE testsuites [<!ENTITY x "bad">]><testsuites/>',
                   '<testsuite tests="0"><testcase file="test_x.py" name="test_x"/></testsuite>',
                   '<testsuite><testcase file="test_x.py" name="test_x"><failure/><skipped/></testcase></testsuite>']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'junit.xml'
            for xml in samples:
                path.write_text(xml)
                with self.subTest(xml=xml), self.assertRaises(ValueError): junit_tests(path)

    def test_junit_preserves_parameter_and_class_node_id(self):
        xml = '<testsuites><testsuite><testcase file="python/frameworks/x/tests/test_sdk.py" classname="python.frameworks.x.tests.test_sdk.TestSDK" name="test_call[param]" time="0.1"/></testsuite></testsuites>'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'junit.xml'; path.write_text(xml)
            self.assertEqual(junit_tests(path)[0]['test_id'], 'python/frameworks/x/tests/test_sdk.py::TestSDK::test_call[param]')


class ResultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.artifacts = Path(tmp.name)

    def validate(self, data, registry, **kwargs):
        return validate_results(data, registry, artifact_root=self.artifacts, **kwargs)

    def test_valid_pending_evidence_stays_pending(self):
        data, registry = result_fixture(self.artifacts)
        self.assertIs(self.validate(data, registry), data)
        self.assertEqual(data['cases'][0]['qualification'], 'pilot_pending')

    def test_unexecuted_integration_cannot_pass(self):
        data, registry = result_fixture(self.artifacts); c = data['cases'][0]['contract']
        c.update(tests=[], executed=0, collected=0)
        with self.assertRaisesRegex(ValueError, 'nonzero execution'):
            self.validate(data, registry)

    def test_missing_unplanned_duplicate_and_source_mismatch(self):
        for mutation in ['missing', 'unplanned', 'duplicate', 'sha']:
            data, registry = result_fixture(self.artifacts)
            if mutation == 'missing': data['cases'] = []
            if mutation == 'unplanned': data['cases'][0]['case_id'] = 'rogue'
            if mutation == 'duplicate': data['cases'] *= 2
            if mutation == 'sha': data['cases'][0]['source_sha'] = '3'*40
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(data, registry)

    def test_requested_resolved_skip_failure_and_count_mismatch(self):
        for mutation in ['requested', 'skip', 'failure', 'count', 'lock', 'core', 'qualification']:
            data, registry = result_fixture(self.artifacts); c = data['cases'][0]
            if mutation == 'requested': c['requested'][0]['selected_version'] = '999'
            if mutation == 'skip': c['contract']['tests'][0]['outcome'] = 'skipped'
            if mutation == 'failure': c['contract']['tests'][0].update(outcome='failure', failure_signature='b'*64)
            if mutation == 'count': c['contract']['executed'] = 0
            if mutation == 'lock': c['dependency_lock'] = None
            if mutation == 'core': c['resolved'] = [r for r in c['resolved'] if r['role'] != 'core']
            if mutation == 'qualification': c['qualification'] = 'qualified'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(data, registry)

    def test_unrelated_partial_or_changed_suite_cannot_substitute_for_contract(self):
        for mutation in ('unrelated', 'partial', 'digest', 'scenario', 'command'):
            data, registry = result_fixture(self.artifacts)
            c = data['cases'][0]['contract']
            if mutation == 'unrelated': c['tests'][0]['test_id'] = 'python/frameworks/openai/tests/test_other.py::test_pass'
            if mutation == 'partial':
                c['tests'].pop(); c['collected'] -= 1; c['executed'] -= 1
            if mutation == 'digest': c['suite_sha256'] = 'b'*64
            if mutation == 'scenario': c['scenarios'] = ['sync']
            if mutation == 'command': c['command'] = ['python', '-m', 'pytest', 'test_other.py']
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'registered|trusted suite'):
                self.validate(data, registry)

    def test_reports_must_match_claimed_nodes_outcomes_signatures_durations_and_counts(self):
        for mutation in ('empty', 'malformed', 'node', 'outcome', 'duration', 'duplicate', 'signature'):
            data, registry = result_fixture(self.artifacts)
            path = self.artifacts / 'junit/tests.xml'
            tree = ET.parse(path)
            node = next(tree.iter('testcase'))
            if mutation == 'empty': path.write_text('<testsuites/>')
            elif mutation == 'malformed': path.write_text('not xml')
            elif mutation == 'duplicate':
                data['cases'][0]['contract']['junit_paths'] *= 2
            else:
                if mutation == 'node': node.set('name', 'test_other')
                if mutation == 'outcome': ET.SubElement(node, 'skipped')
                if mutation == 'duration': node.set('time', '999')
                if mutation == 'signature': ET.SubElement(node, 'failure', message='TypeError: unrelated')
                tree.write(path)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'JUnit'):
                self.validate(data, registry)
        data, registry = result_fixture(self.artifacts)
        with self.assertRaisesRegex(ValueError, 'artifact root'):
            validate_results(data, registry)

    def test_requested_requirement_uses_pep440_and_exact_native_pins(self):
        for req, valid in [('==0.1.0', False), ('anthropic==0.41.0', True), ('>=0.41,<1', True),
                           ('other==0.41.0', False), ('anthropic@https://example.invalid/file', False)]:
            row = dict(name='anthropic', requirement=req, selected_version='0.41.0')
            if valid: requested_version(row, 'python')
            else:
                with self.assertRaises(ValueError): requested_version(row, 'python')
        for language, req, version in [('typescript', '^1.0.0', '2.0.0'), ('go', '>=v1.0.0', 'v1.2.0'), ('java', '[1,3)', '2')]:
            with self.assertRaises(ValueError): requested_version(dict(name='sdk', requirement=req, selected_version=version), language)
        data, registry = result_fixture(self.artifacts)
        data['cases'][0]['requested'][0]['requirement'] = '==0.1.0'
        with self.assertRaisesRegex(ValueError, 'requirement excludes'):
            self.validate(data, registry)

    def test_latest_requires_complete_discovery_and_immutable_cooldown(self):
        for mutation in ('missing', 'duplicate', 'recent', 'unknown_age', 'first_seen_recent', 'cutoff', 'latest'):
            data, registry = result_fixture(self.artifacts)
            case = data['cases'][0]
            case['selector'] = 'latest_eligible'
            discovery = dict(name='anthropic', absolute_latest='0.41.0', latest_eligible='0.41.0',
                             requested_version='0.41.0', resolved_version='0.41.0',
                             published_at='2026-10-25T09:00:00Z', first_seen_at=None, yanked=False)
            case['discovery'] = [discovery]
            self.validate(data, registry)  # exactly on the cutoff is eligible
            if mutation == 'missing': case['discovery'] = []
            if mutation == 'duplicate': case['discovery'] *= 2
            if mutation == 'recent': discovery['published_at'] = '2026-10-28T08:59:00Z'
            if mutation == 'unknown_age': discovery['published_at'] = None
            if mutation == 'first_seen_recent':
                discovery.update(published_at=None, first_seen_at='2026-10-28T08:59:00Z')
            if mutation == 'cutoff': data['run']['cooldown_cutoff'] = '2026-10-28T09:00:00Z'
            if mutation == 'latest': discovery['latest_eligible'] = '0.42.0'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(data, registry)

    def test_trusted_plan_binds_requests_discovery_source_and_runtime(self):
        for mutation in ('digest', 'request', 'source', 'runtime', 'discovery'):
            data, registry = result_fixture(self.artifacts)
            plan, raw = plan_fixture(data, self.artifacts)
            self.validate(data, registry, expected_plan=plan, plan_bytes=raw)
            if mutation == 'digest': data['run']['plan_sha256'] = 'b'*64
            if mutation == 'request': data['cases'][0]['requested'][0]['requirement'] = '>=0.1'
            if mutation == 'source': data['run']['source_sha'] = '3'*40
            if mutation == 'runtime': data['cases'][0]['runtime']['version'] = '3.12.0'
            if mutation == 'discovery': data['cases'][0]['discovery'] = [dict(name='anthropic', absolute_latest='0.41.0',
                latest_eligible='0.41.0', requested_version='0.41.0', resolved_version='0.41.0',
                published_at=None, first_seen_at=None, yanked=False)]
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'plan|trusted'):
                self.validate(data, registry, expected_plan=plan, plan_bytes=raw)

    def test_cli_parses_original_reports_and_trusted_plan(self):
        data, registry = result_fixture(self.artifacts)
        sha = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        data['run']['source_sha'] = data['cases'][0]['source_sha'] = sha
        _, raw = plan_fixture(data, self.artifacts)
        (self.artifacts / 'compat-plan.json').write_bytes(raw)
        path = self.artifacts / 'results.json'
        path.write_text(json.dumps(data))
        def run():
            return subprocess.run([sys.executable, '-I', str(ROOT / '.github/compat/validate.py'),
                                   'results', str(path), '--root', str(ROOT)], capture_output=True, text=True)
        self.assertEqual(run().returncode, 0, run().stderr)
        for xml in ('<testsuites/>', 'not xml'):
            (self.artifacts / 'junit/tests.xml').write_text(xml)
            result = run()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('JUnit', result.stderr)

    def test_strict_cli_rejects_rehashed_lock_status_role_and_stability_controls(self):
        for mutation in ('lock', 'invocation', 'role', 'prerelease', 'flaky_empty'):
            data, registry = result_fixture(self.artifacts)
            sha = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
            case = data['cases'][0]
            data['run']['source_sha'] = case['source_sha'] = sha
            if mutation == 'role':
                for field in ('requested', 'resolved'):
                    case[field][1]['role'], case[field][2]['role'] = case[field][2]['role'], case[field][1]['role']
            if mutation == 'prerelease':
                version = '2.0.0rc1'
                case['selector'] = 'latest_eligible'
                case['requested'][0].update(requirement='==' + version, selected_version=version)
                case['resolved'][0]['version'] = version
                case['discovery'] = [dict(name='anthropic', absolute_latest=version, latest_eligible=version,
                    requested_version=version, resolved_version=version, published_at='2026-10-24T00:00:00Z', first_seen_at=None, yanked=False)]
                lock = self.artifacts / 'locks/freeze.txt'
                lock.write_text(lock.read_text().replace('anthropic==0.41.0', 'anthropic==' + version))
                case['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            _, raw = plan_fixture(data, self.artifacts)
            (self.artifacts / 'compat-plan.json').write_bytes(raw)
            if mutation == 'lock':
                lock = self.artifacts / 'locks/freeze.txt'
                lock.write_text(lock.read_text().replace('anthropic==0.41.0', 'anthropic==0.99.0'))
                case['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            if mutation == 'invocation':
                (self.artifacts / 'junit/tests.xml.exit-code').write_text('1\n')
                path = self.artifacts / f'invocations/{case["case_id"]}-attempt-1.json'
                manifest = json.loads(path.read_text()); manifest['contract']['exit_code'] = 1
                path.write_text(json.dumps(manifest))
            if mutation == 'flaky_empty':
                case.update(status='failed', failure=dict(kind='install', signature='a'*64, summary='failure', test_ids=[]))
                case['contract'].update(tests=[], junit_paths=[], executed=0, collected=0)
                second = copy.deepcopy(case); second.update(attempt=2, status='flaky', failure=None)
                data['cases'].append(second)
            path = self.artifacts / 'results.json'
            path.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, '-I', str(ROOT / '.github/compat/validate.py'),
                'results', str(path), '--root', str(ROOT)], capture_output=True, text=True)
            with self.subTest(mutation=mutation):
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('compatibility:', result.stderr)

    def retry_fixture(self):
        data, registry = result_fixture(self.artifacts)
        second = copy.deepcopy(data['cases'][0])
        second.update(attempt=2, status='flaky')
        first = data['cases'][0]
        first.update(status='blocked', resolved=[], dependency_lock=None,
            failure=dict(kind='install', signature='a'*64, summary='Install failed before observations', test_ids=[]))
        first['contract'].update(suite_sha256=hashlib.sha256(b'').hexdigest(), tests=[],
            junit_paths=[], collected=0, executed=0, unexpected_skips=0, scenarios=[])
        data['cases'].append(second)
        write_invocations(second, self.artifacts, data['run']['plan_sha256'])
        return data, registry

    def test_retry_cannot_re_resolve_or_launder_flake(self):
        data, registry = self.retry_fixture()
        self.validate(data, registry)  # Previously unknown observations may populate.
        data['cases'][1]['status'] = 'passed'
        with self.assertRaisesRegex(ValueError, 'flaky'): self.validate(data, registry)
        data['cases'][1]['status'] = 'flaky'
        data['cases'][0]['resolved'] = [{**data['cases'][1]['resolved'][0], 'version': '999'}]
        with self.assertRaisesRegex(ValueError, 'immutable'): self.validate(data, registry)

    def test_flaky_must_have_real_success_and_retained_failure(self):
        for mutation in ('empty', 'partial', 'skipped', 'failed', 'no_failure', 'first_flaky'):
            data, registry = self.retry_fixture()
            first, second = data['cases']
            contract = second['contract']
            if mutation == 'empty': contract.update(tests=[], junit_paths=[], executed=0, collected=0)
            if mutation == 'partial':
                contract['tests'].pop(); contract['executed'] -= 1; contract['collected'] -= 1
            if mutation == 'skipped':
                contract['tests'][0]['outcome'] = 'skipped'; contract['unexpected_skips'] = 1; contract['executed'] -= 1
            if mutation == 'failed': contract['tests'][0].update(outcome='failure', failure_signature='a'*64)
            if mutation == 'no_failure': first['failure'] = None
            if mutation == 'first_flaky': first['status'] = 'flaky'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.validate(data, registry)

    def test_lock_parsers_reject_malformed_duplicate_and_unknown_adapters(self):
        report = dict(version='1', install=[dict(metadata=dict(name='Sdk_Name', version='1.0'))])
        self.assertEqual(installed_distributions(json.dumps(report).encode(), 'pip-report'), {'sdk-name': '1.0'})
        invalid = [b'not-json', b'{"version":"1","version":"1","install":[]}',
            json.dumps({**report, 'install': report['install'] * 2}).encode(),
            json.dumps(dict(version='1', install=[dict(metadata=dict(name='sdk', version='not-version'))])).encode(),
            json.dumps(dict(version='1', install=[dict(metadata={})])).encode()]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError): installed_distributions(raw, 'pip-report')
        for raw in (b'', b'sdk>=1\n', b'sdk==1\nSDK==1\n', b'sdk @ file:///wheel\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError): installed_distributions(raw, 'pip-freeze')
        with self.assertRaisesRegex(ValueError, 'unqualified'): installed_distributions(b'{}', 'npm-lock')

    def test_complete_frozen_lock_cannot_be_rehashed_into_another_environment(self):
        for mutation in ('wrong_upstream', 'missing_core', 'missing_transitive', 'extra', 'duplicate', 'malformed'):
            data, registry = result_fixture(self.artifacts)
            plan, raw = plan_fixture(data, self.artifacts)
            self.validate(data, registry, expected_plan=plan, plan_bytes=raw)
            lock = self.artifacts / 'locks/freeze.txt'
            text = lock.read_text()
            if mutation == 'wrong_upstream': text = text.replace('anthropic==0.41.0', 'anthropic==0.99.0')
            if mutation == 'missing_core': text = text.replace('fi-instrumentation-otel==1.2.1\n', '')
            if mutation == 'missing_transitive': text = text.replace('transitive==1.0\n', '')
            if mutation == 'extra': text += 'unplanned==1.0\n'
            if mutation == 'duplicate': text += 'TRANSITIVE==1.0\n'
            if mutation == 'malformed': text += '--editable .\n'
            lock.write_text(text)
            data['cases'][0]['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'lock|freeze'):
                self.validate(data, registry, expected_plan=plan, plan_bytes=raw)

    def test_invocation_status_cannot_hide_runner_or_coverage_failure(self):
        for status in (None, '1\n', '2\n', '3\n', '5\n', '-1\n', '0\n1\n'):
            data, registry = result_fixture(self.artifacts)
            sidecar = self.artifacts / 'junit/tests.xml.exit-code'
            if status is None: sidecar.unlink()
            else: sidecar.write_text(status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'invocation|runner'):
                self.validate(data, registry)

    def test_collection_invocations_and_manifest_identity_are_required(self):
        for mutation in ('missing', 'nonzero', 'timeout', 'unknown_exit', 'source', 'plan', 'evidence', 'partial'):
            data, registry = result_fixture(self.artifacts)
            path = self.artifacts / f'invocations/{data["cases"][0]["case_id"]}-attempt-1.json'
            manifest = json.loads(path.read_text())
            if mutation == 'missing': path.unlink()
            else:
                if mutation == 'nonzero': manifest['collection']['exit_code'] = 1
                if mutation == 'timeout': manifest['collection']['timed_out'] = True
                if mutation == 'unknown_exit': manifest['collection']['exit_code'] = None
                if mutation == 'source': manifest['source_sha'] = 'f'*40
                if mutation == 'plan': manifest['plan_sha256'] = 'f'*64
                if mutation == 'evidence': manifest['collection']['evidence_sha256'] = 'f'*64
                if mutation == 'partial':
                    collection = self.artifacts / 'locks/collection.json'
                    collection.write_text(json.dumps(dict(nodes=[], deselected=[], errors=[])))
                    manifest['collection']['evidence_sha256'] = hashlib.sha256(collection.read_bytes()).hexdigest()
                path.write_text(json.dumps(manifest))
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'invocation'):
                self.validate(data, registry)

    def test_swapped_and_duplicate_distribution_roles_are_rejected(self):
        for mutation in ('swap', 'duplicate'):
            data, registry = result_fixture(self.artifacts)
            for field in ('requested', 'resolved'):
                rows = data['cases'][0][field]
                if mutation == 'swap': rows[1]['role'], rows[2]['role'] = rows[2]['role'], rows[1]['role']
                else: rows[2]['name'] = rows[1]['name']
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'role|identity'):
                self.validate(data, registry)

    def test_latest_eligible_stable_rejects_aged_prerelease_and_dev(self):
        for version in ('2.0.0rc1', '2.0.0.dev1'):
            data, registry = result_fixture(self.artifacts)
            case = data['cases'][0]
            case['selector'] = 'latest_eligible'
            case['requested'][0].update(requirement='==' + version, selected_version=version)
            case['resolved'][0]['version'] = version
            case['discovery'] = [dict(name='anthropic', absolute_latest=version, latest_eligible=version,
                requested_version=version, resolved_version=version, published_at='2026-10-24T00:00:00Z', first_seen_at=None, yanked=False)]
            lock = self.artifacts / 'locks/freeze.txt'
            lock.write_text(lock.read_text().replace('anthropic==0.41.0', 'anthropic==' + version))
            case['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'prerelease/dev'):
                self.validate(data, registry)


if __name__ == '__main__':
    unittest.main()
