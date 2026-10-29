import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from report import (CANARY, REPO, ROOT, GitHub, MAX_EXPANDED, SafeRedirect, apply_actions,
                    extract_archive, github_results, junit_outcomes, load_json, lock_packages, main, safe_path,
                    canonical_validate, snapshot_source, validate_bundle, validate_plan_snapshot, verify_provenance, write_outputs)
from incidents import timestamp

HERE = Path(__file__).resolve().parent
REGISTRY = json.loads((HERE / 'registry.json').read_text())
RUNS = json.loads((HERE / 'fixtures/reporter-results.json').read_text())['runs']
NOW = timestamp('2026-10-28T12:00:00Z')


def mutated(change):
    bundle = copy.deepcopy(RUNS[-1])
    change(bundle, bundle['cases'][0])
    return bundle


def bundle_archive(bundle):
    bundle = copy.deepcopy(bundle)
    case = bundle['cases'][0]
    selected = {k: case[k] for k in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
    selected['runtime'] = '.'.join(case['runtime']['version'].split('.')[:2])
    selected['runtime_identity'] = case['runtime']
    plan = dict(source_sha=bundle['run']['source_sha'], workflow_sha=bundle['run']['workflow_sha'], self_test=False, cases=[selected])
    raw = json.dumps(plan).encode()
    bundle['run']['plan_sha256'] = hashlib.sha256(raw).hexdigest()
    freeze = ''.join(p['name'] + '==' + p['version'] + '\n' for p in case['resolved']).encode()
    for observation in bundle['cases']:
        observation['dependency_lock']['sha256'] = hashlib.sha256(freeze).hexdigest()
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('results.json', json.dumps(bundle))
        archive.writestr('compat-plan.json', raw)
        archive.writestr(case['dependency_lock']['path'], freeze)
        archive.writestr(case['contract']['junit_paths'][0], '<testsuite><testcase file="tests/test_fixture.py" name="test_stream"/></testsuite>')
    return bundle, out.getvalue()


class ValidationTests(unittest.TestCase):
    def test_fixture_bundles_conform(self):
        for bundle in RUNS:
            validate_bundle(bundle, REGISTRY, NOW)

    def test_empty_collection_skip_mismatch_and_missing_artifacts_fail(self):
        mutations = [
            lambda b, c: c['contract'].update(executed=0, collected=0, tests=[]),
            lambda b, c: c['contract']['tests'][0].update(outcome='skipped'),
            lambda b, c: c['requested'][0].update(selected_version='1.94.0'),
            lambda b, c: c.update(source_sha='f' * 40),
            lambda b, c: c.update(dependency_lock=None),
            lambda b, c: c['contract'].update(junit_paths=[]),
            lambda b, c: c['resolved'].pop(),
            lambda b, c: c['resolved'].append(copy.deepcopy(c['resolved'][0])),
            lambda b, c: c['discovery'][0].update(yanked=True),
            lambda b, c: c['discovery'][0].update(latest_eligible='1.94.0'),
            lambda b, c: b['cases'].append(copy.deepcopy(c)),
            lambda b, c: b['cases'].clear(),
            lambda b, c: c['dependency_lock'].update(path='../secret'),
            lambda b, c: c['contract'].update(junit_paths=['/tmp/absolute.xml']),
            lambda b, c: c['resolved'][0].update(version="1.0'; whoami; '"),
            lambda b, c: b['run'].update(completed_at='2099-01-01T00:00:00Z'),
            lambda b, c: b['run'].update(url='https://evil.example/'),
            lambda b, c: b['run'].update(cooldown_cutoff=b['run']['started_at']),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index), self.assertRaises(ValueError):
                validate_bundle(mutated(mutation), REGISTRY, NOW)

    def test_unexpected_companion_cannot_be_omitted_from_passing_tuple(self):
        registry = copy.deepcopy(REGISTRY)
        target = next(t for t in registry['integrations'] if t['id'] == 'python-openai')
        target['upstream'].append(dict(name='missing-companion'))
        with self.assertRaisesRegex(ValueError, 'companion tuple'):
            validate_bundle(RUNS[-1], registry, NOW)

    def test_lock_and_plan_bytes_are_verified(self):
        bundle = copy.deepcopy(RUNS[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = safe_path(root, bundle['cases'][0]['dependency_lock']['path'])
            lock.parent.mkdir()
            lock.write_text('openai==1.93.1\ntraceAI-openai==0.1.10\nfi-instrumentation-otel==1.1.0\n')
            junit = safe_path(root, bundle['cases'][0]['contract']['junit_paths'][0])
            junit.parent.mkdir()
            junit.write_text('<testsuite tests="1"><testcase file="tests/test_fixture.py" classname="tests.test_fixture" name="test_stream"/></testsuite>')
            selected = {key: bundle['cases'][0][key] for key in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
            selected['runtime'] = '3.11'
            plan = dict(source_sha=bundle['run']['source_sha'], workflow_sha=bundle['run']['workflow_sha'],
                        self_test=False, cases=[selected])
            data = json.dumps(plan).encode()
            bundle['run']['plan_sha256'] = hashlib.sha256(data).hexdigest()
            bundle['cases'][0]['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            validate_bundle(bundle, REGISTRY, NOW, artifact_root=root, plan_bytes=data)
            with self.assertRaisesRegex(ValueError, 'plan digest'):
                validate_bundle(bundle, REGISTRY, NOW, artifact_root=root, plan_bytes=data+b' ')
            lock.write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'lock digest'):
                validate_bundle(bundle, REGISTRY, NOW, artifact_root=root, plan_bytes=data)
            junit.unlink()
            with self.assertRaisesRegex(ValueError, 'missing referenced'):
                validate_bundle(bundle, REGISTRY, NOW, artifact_root=root, plan_bytes=data)

    def test_junit_content_not_just_existence_is_checked(self):
        bundle = copy.deepcopy(RUNS[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = safe_path(root, bundle['cases'][0]['dependency_lock']['path'])
            lock.parent.mkdir()
            lock.write_text('openai==1.93.1\ntraceAI-openai==0.1.10\nfi-instrumentation-otel==1.1.0\n')
            bundle['cases'][0]['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            junit = safe_path(root, bundle['cases'][0]['contract']['junit_paths'][0])
            junit.parent.mkdir()
            for content in ('<testsuite/>', '<testsuite><testcase><failure/></testcase></testsuite>',
                            '<!DOCTYPE x [<!ENTITY x "boom">]><testsuite/>', 'not XML'):
                junit.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    validate_bundle(bundle, REGISTRY, NOW, artifact_root=root)

    def test_wrong_instrumentation_core_package_names_are_not_passes(self):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['cases'][0]['resolved'][-1]['name'] = 'not-the-core-package'
        with self.assertRaisesRegex(ValueError, 'wrong instrumentation/core'):
            validate_bundle(bundle, REGISTRY, NOW)

    def test_failed_first_attempt_cannot_be_clean_pass_retry(self):
        bundle = copy.deepcopy(RUNS[1])
        retry = copy.deepcopy(RUNS[-1]['cases'][0])
        retry.update(attempt=2, completed_at=bundle['run']['completed_at'], started_at=bundle['run']['started_at'])
        bundle['cases'].append(retry)
        with self.assertRaisesRegex(ValueError, 'flaky'):
            validate_bundle(bundle, REGISTRY, NOW)

    def test_install_failure_without_junit_is_reportable(self):
        bundle = copy.deepcopy(RUNS[1])
        case = bundle['cases'][0]
        case.update(resolved=[], dependency_lock=None)
        case['failure']['kind'] = 'install'
        case['contract'].update(tests=[], executed=0, collected=0, junit_paths=[])
        validate_bundle(bundle, REGISTRY, NOW)

    def test_successful_flaky_retry_uses_the_retained_original_failure(self):
        bundle = copy.deepcopy(RUNS[-1])
        first = copy.deepcopy(bundle['cases'][0])
        first.update(status='failed', failure=copy.deepcopy(RUNS[1]['cases'][0]['failure']))
        first['contract']['tests'][0]['outcome'] = 'failure'
        first['contract']['tests'][0]['failure_signature'] = first['failure']['signature']
        bundle['cases'][0].update(attempt=2, status='flaky', failure=None)
        bundle['cases'].insert(0, first)
        validate_bundle(bundle, REGISTRY, NOW)
        missing_failure = copy.deepcopy(bundle)
        missing_failure['cases'][0]['failure'] = None
        with self.assertRaisesRegex(ValueError, 'signature'):
            validate_bundle(missing_failure, REGISTRY, NOW)
        for change in (lambda c: c['resolved'][0].update(version='0.0.1'),
                       lambda c: c['contract'].update(executed=0, collected=0, tests=[]),
                       lambda c: c['contract']['tests'][0].update(outcome='failure')):
            invalid = copy.deepcopy(bundle)
            change(invalid['cases'][1])
            with self.assertRaises(ValueError):
                validate_bundle(invalid, REGISTRY, NOW)
        # Flaky is degraded, never a recovery pass or install recommendation.
        from publish import build_index
        self.assertIsNone(build_index([bundle], REGISTRY, NOW)['observations'][0]['install_command'])

    def test_missing_boundary_upload_is_not_an_observed_runtime(self):
        bundle = copy.deepcopy(RUNS[1])
        case = bundle['cases'][0]
        case.update(status='blocked', resolved=[], dependency_lock=None)
        case['failure'].update(kind='automation', test_ids=[])
        case['contract'].update(tests=[], collected=0, executed=0, junit_paths=[], scenarios=[])
        selection = {k: case[k] for k in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
        selection['runtime'] = '3.13'
        data = json.dumps(dict(source_sha=bundle['run']['source_sha'], workflow_sha=bundle['run']['workflow_sha'],
                               self_test=False, cases=[selection])).encode()
        bundle['run']['plan_sha256'] = hashlib.sha256(data).hexdigest()
        validate_bundle(bundle, REGISTRY, NOW, plan_bytes=data)
        executed = copy.deepcopy(RUNS[-1])
        executed['run']['plan_sha256'] = hashlib.sha256(data).hexdigest()
        with self.assertRaisesRegex(ValueError, 'runtime differs'):
            validate_bundle(executed, REGISTRY, NOW, plan_bytes=data)

    def test_discovery_requires_complete_age_provenance(self):
        mutations = [lambda c: c.update(discovery=[]),
                     lambda c: c['discovery'][0].update(published_at=None, first_seen_at=None),
                     lambda c: c['discovery'][0].update(published_at='2026-10-28T04:00:00Z'),
                     lambda c: c['discovery'][0].update(requested_version='0.0.1'),
                     lambda c: c['discovery'].append(copy.deepcopy(c['discovery'][0]))]
        for change in mutations:
            bundle = copy.deepcopy(RUNS[-1])
            change(bundle['cases'][0])
            with self.assertRaises(ValueError):
                validate_bundle(bundle, REGISTRY, NOW)
        bundle = copy.deepcopy(RUNS[-1])
        bundle['cases'][0]['discovery'][0]['published_at'] = None
        validate_bundle(bundle, REGISTRY, NOW)  # old first-seen time is explicit fallback

    def test_junit_requires_exact_ids_outcomes_and_no_duplicates(self):
        bundle = copy.deepcopy(RUNS[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = safe_path(root, bundle['cases'][0]['dependency_lock']['path'])
            lock.parent.mkdir()
            lock.write_text('openai==1.93.1\ntraceAI-openai==0.1.10\nfi-instrumentation-otel==1.1.0\n')
            bundle['cases'][0]['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
            junit = safe_path(root, bundle['cases'][0]['contract']['junit_paths'][0])
            junit.parent.mkdir()
            good = '<testcase file="tests/test_fixture.py" classname="tests.test_fixture" name="test_stream"/>'
            for xml in ('<testsuite><testcase file="other.py" name="other"/></testsuite>',
                        '<testsuite>' + good + good + '</testsuite>',
                        '<testsuite>' + good.replace('/>', '><failure message="AssertionError: changed outcome">AssertionError: changed outcome</failure></testcase>') + '</testsuite>'):
                junit.write_text(xml)
                with self.assertRaisesRegex(ValueError, 'JUnit'):
                    validate_bundle(bundle, REGISTRY, NOW, artifact_root=root)
            junit.write_text('<testsuite>' + good + '</testsuite>')
            validate_bundle(bundle, REGISTRY, NOW, artifact_root=root)
        self.assertEqual(junit_outcomes(b'<testsuite><testcase file="x/test_a.py" classname="x.test_a.TestFoo" name="test_a[p.q]"/></testsuite>'),
                         {'x/test_a.py::TestFoo::test_a[p.q]': 'passed'})
        # Production requires repository-relative file attributes; a classname
        # alone cannot authenticate a canonical pytest node ID.
        with self.assertRaisesRegex(ValueError, 'JUnit.*file'):
            junit_outcomes(b'<testsuite><testcase classname="x.test_a" name="test_a[p.q]"/></testsuite>')

    def test_unknown_junit_codec_is_a_controlled_validation_error(self):
        raw = b'<?xml version="1.0" encoding="not-a-codec"?><testsuite><testcase file="tests/test_x.py" name="test_y"/></testsuite>'
        with self.assertRaisesRegex(ValueError, 'malformed JUnit'):
            junit_outcomes(raw)

    def test_invalid_unicode_scalars_are_rejected_at_json_boundaries(self):
        from incidents import decode_json
        for scalar in ('\ud800', '\udfff'):
            for value in (scalar, [scalar], {'nested': [{'summary': scalar}]}, {scalar: 'key'}):
                raw = json.dumps(value)
                for encoded in (raw, raw.encode('ascii')):
                    with self.subTest(value=repr(value), bytes=isinstance(encoded, bytes)), \
                            self.assertRaisesRegex(ValueError, 'Unicode scalar'):
                        decode_json(encoded)
        # Valid escaped surrogate pairs decode to an actual Unicode scalar.
        self.assertEqual(decode_json(b'"\\ud800\\udc00"'), '\U00010000')
        self.assertEqual(decode_json(json.dumps({'text': '\U0010ffff café'})), {'text': '\U0010ffff café'})

    def test_correctly_hashed_contradictory_lock_is_rejected(self):
        bundle = copy.deepcopy(RUNS[-1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = safe_path(root, bundle['cases'][0]['dependency_lock']['path'])
            lock.parent.mkdir()
            junit = safe_path(root, bundle['cases'][0]['contract']['junit_paths'][0])
            junit.parent.mkdir()
            junit.write_text('<testsuite><testcase file="tests/test_fixture.py" name="test_stream"/></testsuite>')
            for package in ('openai', 'traceAI-openai', 'fi-instrumentation-otel'):
                freeze = '\n'.join(p['name'] + '==' + ('0.0.1' if p['name'] == package else p['version'])
                                   for p in bundle['cases'][0]['resolved'])
                lock.write_text(freeze)
                bundle['cases'][0]['dependency_lock']['sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
                with self.subTest(package=package), self.assertRaisesRegex(ValueError, 'contradicts'):
                    validate_bundle(bundle, REGISTRY, NOW, artifact_root=root)
        self.assertEqual(lock_packages(b'traceAI-openai @ file:///tmp/traceai_openai-0.1.10-py3-none-any.whl#sha256=abc',
                                       'pip-freeze', 'python'), {'traceai-openai': '0.1.10'})
        report = json.dumps({'install': [{'metadata': {'name': 'Foo_Bar', 'version': '1.0'}}]}).encode()
        self.assertEqual(lock_packages(report, 'pip-report', 'python'), {'foo-bar': '1.0'})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            lock_packages(b'foo==1.0\nfoo==2.0', 'pip-freeze', 'python')
        with self.assertRaises(ValueError):
            lock_packages(b'foo @ file:///source', 'pip-freeze', 'python')

    def test_malformed_plan_shapes_raise_validation_errors(self):
        for plan in (None, [], {'cases': None}, dict(source_sha='a'*40, workflow_sha='b'*40, self_test=False, cases=[None])):
            bundle = copy.deepcopy(RUNS[-1])
            data = json.dumps(plan).encode()
            bundle['run']['plan_sha256'] = hashlib.sha256(data).hexdigest()
            with self.assertRaises(ValueError):
                validate_bundle(bundle, REGISTRY, NOW, plan_bytes=data)

    def test_deep_json_is_bounded_at_each_untrusted_boundary(self):
        nested = b'[' * 20000 + b'0' + b']' * 20000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested.json'
            path.write_bytes(nested)
            with self.assertRaisesRegex(ValueError, 'nesting'):
                load_json(path)
        with self.assertRaisesRegex(ValueError, 'nesting'):
            validate_plan_snapshot(nested)
        with self.assertRaisesRegex(ValueError, 'nesting'):
            lock_packages(nested, 'pip-report', 'python')
        # Brackets and escaped quotes in strings do not consume the depth budget.
        from incidents import decode_json
        value = {'text': '[\\"' * 20000}
        self.assertEqual(decode_json(json.dumps(value)), value)

    def test_plan_shape_does_not_impose_python_runtime_syntax_on_other_languages(self):
        for runtime in ('20', '21', '8.0'):
            selected = dict(case_id='native-deferred', integration_id='typescript-openai', mode='source',
                            selector='latest_eligible', requested=[], runtime=runtime)
            plan = dict(source_sha='a'*40, workflow_sha='b'*40, self_test=False, cases=[selected])
            self.assertEqual(validate_plan_snapshot(json.dumps(plan).encode()), plan)
        # Shape parsing does not enable native execution or claim qualification.

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.json'
            path.write_text('{"run": 1, "run": 2}')
            with self.assertRaisesRegex(ValueError, 'duplicate JSON'):
                load_json(path)


class ArchiveTests(unittest.TestCase):
    def archive(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, value in members:
                archive.writestr(name, value)
        return buffer.getvalue()

    def test_safe_archive_extracts_only_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extract_archive(self.archive([('results.json', '{}'), ('locks/example.txt', 'data')]), root)
            self.assertEqual((root/'results.json').read_text(), '{}')

    def test_traversal_absolute_windows_duplicates_and_symlink_rejected(self):
        for name in ('../escape', '/absolute', 'C:/windows', 'sub/../escape', r'sub\escape'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    extract_archive(self.archive([(name, 'x')]), Path(tmp))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            extract_archive(self.archive([('results.json', '{}'), ('RESULTS.json', '{}')]), Path(tmp))
        info = zipfile.ZipInfo('symlink')
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            extract_archive(self.archive([(info, '../target')]), Path(tmp))

    def test_archive_size_and_nonempty_destination_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'existing').write_text('x')
            with self.assertRaisesRegex(ValueError, 'nonempty'):
                extract_archive(self.archive([('results.json', '{}')]), root)
        with tempfile.TemporaryDirectory() as tmp, patch('report.MAX_EXPANDED', 1):
            with self.assertRaisesRegex(ValueError, 'expanded-size'):
                extract_archive(self.archive([('results.json', '{}')]), Path(tmp))

    def test_symlink_in_referenced_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'link').symlink_to('/tmp')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                safe_path(root, 'link/file')


class CanonicalBoundaryTests(unittest.TestCase):
    def test_missing_shared_validator_fails_closed(self):
        with patch('report.Path.is_file', return_value=False), self.assertRaisesRegex(ValueError, 'must be absorbed'):
            canonical_validate(RUNS[-1], REGISTRY, Path('/artifact'), b'{}', Path('/source'))

    def test_only_trusted_sibling_validator_receives_public_abi(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        bundle, _ = bundle_archive(RUNS[-1])
        selected = {k: bundle['cases'][0][k] for k in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
        selected['runtime'] = '3.11'
        raw = json.dumps(dict(source_sha=bundle['run']['source_sha'], workflow_sha=bundle['run']['workflow_sha'], self_test=False, cases=[selected])).encode()
        module = SimpleNamespace(validate_results=Mock(return_value=bundle))
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        with patch('report.Path.is_file', return_value=True), \
                patch('report.importlib.util.spec_from_file_location', return_value=spec) as load, \
                patch('report.importlib.util.module_from_spec', return_value=module):
            canonical_validate(bundle, REGISTRY, Path('/artifact'), raw, Path('/static-source'))
        self.assertEqual(load.call_args.args[1], HERE / 'validate.py')
        spec.loader.exec_module.assert_called_once_with(module)
        args, kwargs = module.validate_results.call_args
        self.assertEqual(args, (bundle, REGISTRY))
        self.assertEqual(kwargs['artifact_root'], Path('/artifact'))
        self.assertEqual(kwargs['source_root'], Path('/static-source'))
        self.assertEqual(kwargs['plan_bytes'], raw)
        self.assertEqual(kwargs['expected_case_ids'], bundle['run']['planned_case_ids'])

    def test_nested_canonical_sidecars_are_controlled_validation_errors(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for failure in (RecursionError, ImportError):
            module = SimpleNamespace(validate_results=Mock(side_effect=failure))
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
            selected = {k: RUNS[-1]['cases'][0][k] for k in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
            selected['runtime'] = '3.11'
            raw = json.dumps(dict(source_sha=RUNS[-1]['run']['source_sha'], workflow_sha=RUNS[-1]['run']['workflow_sha'],
                                  self_test=False, cases=[selected])).encode()
            with patch('report.Path.is_file', return_value=True), \
                    patch('report.importlib.util.spec_from_file_location', return_value=spec), \
                    patch('report.importlib.util.module_from_spec', return_value=module), self.assertRaises(ValueError):
                canonical_validate(RUNS[-1], REGISTRY, Path('/artifact'), raw, Path('/static-source'))

    def source_api(self, mode='100644', corrupt=False):
        import base64
        content = b'raise AssertionError("source is static data, never executed")\n'
        sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        class API:
            def __init__(self):
                self.paths = []
            def request(self, path):
                self.paths.append(path)
                if '/git/trees/' in path:
                    return dict(truncated=False, tree=[
                        dict(path='tests/test_trusted.py', mode=mode, type='blob', sha=sha, size=len(content)),
                        dict(path='unrelated/validate.py', mode='100644', type='blob', sha='f'*40, size=1)])
                return dict(encoding='base64', content=base64.b64encode(content + (b'x' if corrupt else b'')).decode())
        registry = copy.deepcopy(REGISTRY)
        next(t for t in registry['integrations'] if t['id'] == 'python-openai')['contract_test']['paths'] = ['tests/test_trusted.py']
        return API(), registry

    def test_source_snapshot_is_pinned_static_and_only_from_registered_paths(self):
        import sys
        api, registry = self.source_api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(snapshot_source(api, RUNS[-1], registry, root), root)
            self.assertIn('never executed', (root / 'tests/test_trusted.py').read_text())
            self.assertNotIn(str(root), sys.path)
            self.assertFalse((root / 'unrelated').exists())
        self.assertIn(RUNS[-1]['run']['source_sha'], api.paths[0])
        self.assertEqual(len(api.paths), 2)

    def test_historical_registry_is_authenticated_data_validated_by_current_code(self):
        import base64
        from types import SimpleNamespace
        from unittest.mock import Mock
        from report import snapshot_registry
        content = json.dumps(REGISTRY).encode()
        sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        class API:
            def __init__(self, corrupt=False):
                self.paths, self.corrupt = [], corrupt
            def request(self, path):
                self.paths.append(path)
                return dict(type='file', path='.github/compat/registry.json', encoding='base64', size=len(content),
                            sha=sha, content=base64.b64encode(content + (b'x' if self.corrupt else b'')).decode())
        validator = SimpleNamespace(validate_registry=Mock(return_value=REGISTRY))
        api = API()
        with tempfile.TemporaryDirectory() as tmp, patch('report.trusted_validator', return_value=validator):
            registry = snapshot_registry(api, 'a' * 40, Path(tmp))
            self.assertEqual(registry, REGISTRY)
            self.assertEqual((Path(tmp) / '.github/compat/registry.json').read_bytes(), content)
        validator.validate_registry.assert_called_once_with(REGISTRY)
        self.assertEqual(api.paths, [f'/repos/{REPO}/contents/.github/compat/registry.json?ref=' + 'a' * 40])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'identity mismatch'):
            snapshot_registry(API(True), 'a' * 40, Path(tmp))

    def test_symlink_or_tampered_source_snapshot_is_rejected(self):
        for mode, corrupt in [('120000', False), ('100644', True)]:
            api, registry = self.source_api(mode, corrupt)
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                snapshot_source(api, RUNS[-1], registry, Path(tmp))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        # These API/ZIP tests deliberately use synthetic one-test bundles, not a
        # registered SDK contract. Test the separate strict boundary below with
        # its exact public ABI; integration replay uses the actual PR1 module.
        self.strict = self.enterContext(patch('report.canonical_validate'))
        self.snapshot = self.enterContext(patch('report.snapshot_source', side_effect=lambda api, bundle, registry, root: root))
        self.registry_snapshot = self.enterContext(patch('report.snapshot_registry', return_value=REGISTRY))

    def metadata(self):
        run = RUNS[-1]['run']
        return dict(repository=dict(full_name=REPO), head_repository=dict(full_name=REPO), path=CANARY,
                    head_branch='main', event=run['event'], status='completed', id=int(run['run_id']),
                    run_attempt=run['run_attempt'], head_sha=run['workflow_sha'], html_url=run['url'], pull_requests=[])

    class API:
        def request(self, *args):
            return {'status': 'ahead'}

    def test_trusted_main_workflow_can_test_distinct_dev_sha(self):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['run']['source_sha'] = 'a' * 40
        verify_provenance(bundle, self.metadata(), self.API())

    def test_foreign_branch_pr_event_sha_and_ancestry_rejected(self):
        changes = [dict(head_branch='ci/compat-canary'), dict(event='pull_request'),
                   dict(head_repository=dict(full_name='outsider/traceAI')), dict(head_sha='a'*40),
                   dict(path='.github/workflows/unrelated.yml'), dict(pull_requests=[dict(number=42)])]
        for change in changes:
            meta = self.metadata()
            meta.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_provenance(RUNS[-1], meta, self.API())
        class Diverged(self.API):
            def request(self, *args):
                return {'status': 'diverged'}
        with self.assertRaisesRegex(ValueError, 'ancestry'):
            verify_provenance(RUNS[-1], self.metadata(), Diverged())

    def test_selftest_cannot_reach_issue_writes(self):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['run']['self_test'] = True
        with self.assertRaisesRegex(ValueError, 'self-test'):
            verify_provenance(bundle, self.metadata(), self.API())

    def test_rerun_retains_original_plan_but_checks_each_attempt_execution(self):
        bundle = copy.deepcopy(RUNS[-1])
        bundle['run'].update(run_attempt=2, started_at='2026-10-28T10:58:33Z', completed_at='2026-10-28T11:08:00Z')
        first = bundle['cases'][0]
        first.update(started_at='2026-10-28T10:59:00Z', completed_at='2026-10-28T11:00:00Z')
        second = copy.deepcopy(first)
        second.update(attempt=2, started_at='2026-10-28T11:07:00Z', completed_at='2026-10-28T11:08:00Z')
        bundle['cases'].append(second)
        metadata = self.metadata()
        metadata.update(run_attempt=2, run_started_at='2026-10-28T11:06:31Z', updated_at='2026-10-28T11:09:00Z')
        original = dict(metadata, run_attempt=1, run_started_at='2026-10-28T10:58:00Z', updated_at='2026-10-28T11:01:00Z')
        class API:
            def request(self, path):
                return original if path.endswith('/attempts/1') else {'status': 'ahead'}
        verify_provenance(bundle, metadata, API())
        second['started_at'] = '2026-10-28T10:59:00Z'
        with self.assertRaisesRegex(ValueError, 'its GitHub attempt'):
            verify_provenance(bundle, metadata, API())
        second['started_at'] = '2026-10-28T11:07:00Z'
        first['completed_at'] = '2026-10-28T11:07:00Z'
        with self.assertRaisesRegex(ValueError, 'its GitHub attempt'):
            verify_provenance(bundle, metadata, API())

    def test_malformed_downloaded_shapes_are_isolated_from_healthy_runs(self):
        def archive(results, plan=None):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as zip:
                zip.writestr('results.json', json.dumps(results))
                zip.writestr('compat-plan.json', json.dumps(plan))
                zip.writestr('locks/fixture.txt', 'openai==1.93.1\ntraceAI-openai==0.1.10\nfi-instrumentation-otel==1.1.0\n')
                zip.writestr('junit/fixture.xml', '<testsuite><testcase file="tests/test_fixture.py" name="test_stream"/></testsuite>')
            return buffer.getvalue()
        healthy = copy.deepcopy(RUNS[-1])
        case = healthy['cases'][0]
        selected = {k: case[k] for k in ('case_id', 'integration_id', 'mode', 'selector', 'requested')}
        selected['runtime'] = '3.11'
        plan = dict(source_sha=healthy['run']['source_sha'], workflow_sha=healthy['run']['workflow_sha'], self_test=False, cases=[selected])
        healthy['run']['plan_sha256'] = hashlib.sha256(json.dumps(plan).encode()).hexdigest()
        freeze = b'openai==1.93.1\ntraceAI-openai==0.1.10\nfi-instrumentation-otel==1.1.0\n'
        case['dependency_lock']['sha256'] = hashlib.sha256(freeze).hexdigest()
        archives = {1007: archive(healthy, plan)}
        malformed = [None, [], {'run': None}, {'run': []}, dict(healthy, cases=None)]
        malformed_plan = copy.deepcopy(healthy)
        malformed_plan['run']['plan_sha256'] = hashlib.sha256(b'null').hexdigest()
        for index, invalid in enumerate(malformed, 2000):
            archives[index] = archive(invalid)
        archives[3000] = archive(malformed_plan)
        base = dict(self.metadata(), created_at='2026-10-28T04:00:00Z')
        metadata = [dict(base, id=id, html_url=f'https://github.com/{REPO}/actions/runs/{id}') for id in archives]
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/workflows/'):
                    return metadata
                id = int(path.split('/')[2])
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archives[id]), id=id)]
            def request(self, path, **kwargs):
                if path.endswith('/zip'):
                    return archives[int(path.split('/')[-2])]
                return {'status': 'ahead'}
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(errors), 6)
        self.assertEqual({e['id'] for e in errors}, {f'invalid-run:{id}' for id in archives if id != 1007})

    def test_deep_malformed_run_does_not_discard_healthy_evidence(self):
        healthy, archive = bundle_archive(RUNS[-1])
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, 'w') as zip:
            zip.writestr('results.json', '[' * 20000 + '0' + ']' * 20000)
        metadata = [dict(self.metadata(), created_at='2026-10-28T04:00:00Z'),
                    dict(self.metadata(), id=2000, html_url=f'https://github.com/{REPO}/actions/runs/2000', created_at='2026-10-28T04:00:00Z')]
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/workflows/'):
                    return metadata
                id = int(path.split('/')[2])
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=id)]
            def request(self, path, **kwargs):
                if path.endswith('/zip'):
                    return archive if '/1007/' in path else bad.getvalue()
                return {'status': 'ahead'}
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual([b['run']['run_id'] for b in bundles], ['1007'])
        self.assertEqual([e['id'] for e in errors], ['invalid-run:2000'])
        self.strict.assert_called_once()
        self.assertEqual(self.strict.call_args.args[0]['run']['run_id'], '1007')
        self.snapshot.assert_called_once()
        # A schema/private-validator pass cannot bypass canonical invocation,
        # registered-suite or complete-frozen-lock rejection.
        self.strict.side_effect = ValueError('missing invocation evidence')
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual(bundles, [])
        self.assertEqual({e['id'] for e in errors}, {'invalid-run:1007', 'invalid-run:2000'})

    def test_text_boundary_failures_preserve_healthy_run_through_final_output(self):
        for defect in ('xml-codec', 'high-surrogate', 'low-surrogate'):
            good, healthy_archive = bundle_archive(RUNS[-1])
            bad = copy.deepcopy(RUNS[1])
            bad['run'].update(run_id='2000', url=f'https://github.com/{REPO}/actions/runs/2000')
            if defect != 'xml-codec':
                bad['cases'][0]['failure']['summary'] = 'Invalid diagnostic ' + chr(0xd800 if defect == 'high-surrogate' else 0xdfff)
            bad, bad_archive = bundle_archive(bad)
            changed = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(bad_archive)) as source, zipfile.ZipFile(changed, 'w') as dest:
                for entry in source.infolist():
                    content = source.read(entry)
                    if entry.filename.endswith('.xml'):
                        content = b'<testsuite><testcase file="tests/test_fixture.py" name="test_stream"><failure message="AssertionError: fixture failure">AssertionError: fixture failure</failure></testcase></testsuite>'
                        if defect == 'xml-codec':
                            content = b'<?xml version="1.0" encoding="not-a-codec"?>' + content
                    dest.writestr(entry, content)
            archives = {1007: healthy_archive, 2000: changed.getvalue()}
            metadata = [dict(self.metadata(), created_at='2026-10-28T04:00:00Z'),
                        dict(self.metadata(), id=2000, html_url=bad['run']['url'], created_at='2026-10-28T04:00:00Z')]
            class API:
                def pages(self, path, key=None, **kwargs):
                    if path.startswith('actions/workflows/'):
                        return metadata
                    if path.startswith('actions/artifacts?') or path.startswith('issues?'):
                        return []
                    ident = int(path.split('/')[2])
                    return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archives[ident]), id=ident)]
                def request(self, path, **kwargs):
                    return archives[int(path.split('/')[-2])] if path.endswith('/zip') else {'status': 'ahead'}
            # Exercise the normal read-only CLI through final UTF-8 Markdown and
            # JSON writing. Only the synthetic fixture's canonical bundle check
            # is mocked by setUp; XML parsing and JSON decoding are unmocked.
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp, \
                    patch('report.GitHub', return_value=API()), patch('builtins.print'), \
                    patch('sys.argv', ['report', '--github', '--output', tmp, '--now', NOW.isoformat()]):
                self.strict.reset_mock()
                main()
                index = load_json(Path(tmp) / 'results-index.json')
                self.assertEqual([row['run_id'] for row in index['observations']], [good['run']['run_id']])
                self.assertIn('invalid-run:2000', [h['id'] for h in index['health']])
                self.assertIn(good['run']['url'], (Path(tmp) / 'compatibility.md').read_text(encoding='utf-8'))
                self.assertTrue((Path(tmp) / 'incident-actions.json').is_file())
                self.strict.assert_called_once()

    def test_explicit_old_run_and_recent_rerun_are_not_silently_dropped(self):
        from datetime import timedelta
        from incidents import iso
        for recent in (False, True):
            bundle = copy.deepcopy(RUNS[-1])
            if not recent:
                def earlier(value):
                    if isinstance(value, dict):
                        return {k: earlier(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [earlier(v) for v in value]
                    if isinstance(value, str) and value.startswith('2026-') and value.endswith('Z'):
                        return iso(timestamp(value) - timedelta(days=30))
                    return value
                bundle = earlier(bundle)
            else:
                # Immutable plan starts a month ago; the attempt-2 execution is
                # fresh, with the original successful attempt retained as well.
                fresh = copy.deepcopy(bundle['cases'][0])
                original_start = iso(timestamp(bundle['run']['started_at']) - timedelta(days=30))
                original_end = iso(timestamp(bundle['run']['completed_at']) - timedelta(days=30))
                bundle['run'].update(run_attempt=2, started_at=original_start,
                                     cooldown_cutoff=iso(timestamp(bundle['run']['cooldown_cutoff']) - timedelta(days=30)))
                original = bundle['cases'][0]
                original.update(started_at=original_start, completed_at=original_end)
                for discovery in original['discovery']:
                    for key in ('published_at', 'first_seen_at'):
                        if discovery[key]:
                            discovery[key] = iso(timestamp(discovery[key]) - timedelta(days=30))
                fresh.update(attempt=2, discovery=copy.deepcopy(original['discovery']))
                bundle['cases'].append(fresh)
            bundle, archive = bundle_archive(bundle)
            meta = dict(self.metadata(), created_at='2026-09-28T04:00:00Z', run_attempt=bundle['run']['run_attempt'])
            meta.update(run_started_at=bundle['cases'][-1]['started_at'], updated_at=bundle['run']['completed_at'])
            class API:
                def pages(self, path, key, **kwargs):
                    if path.startswith('actions/workflows/'):
                        return []
                    return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=123)]
                def request(self, path, **kwargs):
                    if path.endswith('/zip'):
                        return archive
                    if path.endswith('/attempts/1'):
                        return dict(meta, run_attempt=1, run_started_at=bundle['cases'][0]['started_at'],
                                    updated_at=bundle['cases'][0]['completed_at'])
                    if '/compare/' in path:
                        return {'status': 'ahead'}
                    return meta
            plans = {}
            with self.subTest(recent=recent):
                bundles, errors = github_results(API(), REGISTRY, NOW, selected_run='1007', plans=plans)
                self.assertEqual(errors, [])
                self.assertEqual(len(bundles), 1)
                self.assertIn('1007', plans)
                from publish import build_index
                row = build_index(bundles, REGISTRY, NOW)['observations'][0]
                self.assertEqual(row['status'], 'passed' if recent else 'stale')

    def test_historical_evidence_uses_source_registry_not_current_qualification_or_suite(self):
        _, archive = bundle_archive(RUNS[-1])
        current = copy.deepcopy(REGISTRY)
        target = next(t for t in current['integrations'] if t['id'] == 'python-openai')
        target['qualification'].update(status='qualified', evidence=['reviewed current qualification'])
        target['contract_test']['paths'] = ['tests/new_contract.py']
        meta = dict(self.metadata(), created_at='2026-10-28T04:00:00Z')
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/workflows/'):
                    return [meta]
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=123)]
            def request(self, path, **kwargs):
                return archive if path.endswith('/zip') else {'status': 'ahead'}
        bundles, errors = github_results(API(), current, NOW)
        self.assertEqual(errors, [])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(self.strict.call_args.args[1], REGISTRY)
        self.assertEqual(self.snapshot.call_args.args[2], REGISTRY)
        self.registry_snapshot.assert_called_once()
        # Evidence identity remains historical; a source record cannot self-promote.
        self.assertEqual(bundles[0]['cases'][0]['qualification'], 'pilot_pending')
        from publish import build_index
        self.assertEqual(build_index(bundles, current, NOW)['observations'][0]['qualification'], 'qualified')

    def test_scheduled_discovery_rediscovers_old_run_with_recent_artifact(self):
        healthy, archive = bundle_archive(RUNS[-1])
        meta = dict(self.metadata(), created_at='2026-09-01T00:00:00Z',
                    run_started_at=healthy['run']['started_at'])
        artifact = dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=123,
                        created_at='2026-10-28T05:01:00Z', workflow_run=dict(id=1007))
        paths = []
        class API:
            def pages(self, path, key, **kwargs):
                paths.append(path)
                if path.startswith('actions/workflows/'):
                    return []  # created_at filtering excludes the old original run
                return [artifact]
            def request(self, path, **kwargs):
                if path.endswith('/zip'):
                    return archive
                if '/compare/' in path:
                    return {'status': 'ahead'}
                return meta
        # First explicit ingestion followed by ordinary scheduled discovery.
        for selected in ('1007', None):
            bundles, errors = github_results(API(), REGISTRY, NOW, selected_run=selected)
            self.assertEqual(errors, [])
            self.assertEqual([b['run']['run_id'] for b in bundles], ['1007'])
        self.assertTrue(any(p.startswith('actions/artifacts?') for p in paths))

    def test_malformed_compression_is_isolated_from_healthy_runs(self):
        import struct
        _, archive = bundle_archive(RUNS[-1])
        broken = bytearray(archive)
        struct.pack_into('<H', broken, broken.index(b'PK\x03\x04') + 8, 99)
        struct.pack_into('<H', broken, broken.index(b'PK\x01\x02') + 10, 99)
        metadata = [dict(self.metadata(), created_at='2026-10-28T04:00:00Z'),
                    dict(self.metadata(), id=2000, html_url=f'https://github.com/{REPO}/actions/runs/2000',
                         created_at='2026-10-28T04:00:00Z')]
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/workflows/'):
                    return metadata
                ident = int(path.split('/')[2])
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=ident)]
            def request(self, path, **kwargs):
                if path.endswith('/zip'):
                    return archive if '/1007/' in path else bytes(broken)
                return {'status': 'ahead'}
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual([b['run']['run_id'] for b in bundles], ['1007'])
        self.assertEqual([e['id'] for e in errors], ['invalid-run:2000'])

    def test_unavailable_rejected_or_expired_requested_evidence_is_explicit_health(self):
        for kind in ('unavailable', 'branch', 'expired', 'wrong-id'):
            meta = dict(self.metadata(), created_at='2026-09-01T00:00:00Z')
            if kind == 'branch':
                meta['head_branch'] = 'ci/untrusted'
            if kind == 'wrong-id':
                meta['id'] = 999
            class API:
                def pages(self, path, key, **kwargs):
                    return []
                def request(self, path, **kwargs):
                    if kind == 'unavailable':
                        raise ValueError('GitHub API GET failed with HTTP 404')
                    return meta
            with self.subTest(kind=kind):
                bundles, errors = github_results(API(), REGISTRY, NOW, selected_run='1007')
                self.assertEqual(bundles, [])
                self.assertEqual(len(errors), 1)
                self.assertIn('1007', errors[0]['id'])

    def test_normal_github_dry_run_includes_existing_issue_and_repair_pr_without_writes(self):
        from incidents import REPORTER_USER, reconcile
        failed = copy.deepcopy(RUNS[1])
        action = reconcile([failed], [], REGISTRY, NOW)['actions'][0]
        issue = dict(number=42, body=action['body'], state=action['state'], labels=action['labels'], user=dict(REPORTER_USER))
        pr = dict(number=43, state='open', pull_request={}, body='<!-- traceai-compat-repair:' + action['key'] + ' -->')
        class API:
            def __init__(self):
                self.reads = []
            def pages(self, path, *args, **kwargs):
                self.reads.append(path)
                return [issue, pr]
            def request(self, path, data=None, method='GET', **kwargs):
                if method != 'GET':
                    raise AssertionError('dry-run attempted a write')
        api = API()
        with tempfile.TemporaryDirectory() as tmp, patch('report.GitHub', return_value=api), \
                patch('report.github_results', return_value=([failed], [])), \
                patch('sys.argv', ['report', '--github', '--output', tmp, '--now', NOW.isoformat()]), patch('builtins.print'):
            main()
            handoff = json.loads((Path(tmp) / 'remediation-handoff.json').read_text())[0]
        self.assertEqual(api.reads, ['issues?state=all'])
        self.assertEqual(handoff['issue_number'], 42)
        self.assertEqual(handoff['existing_prs'], [43])
        self.assertEqual(handoff['guard'], 'blocked_existing_pr')
        self.assertFalse(handoff['automatic_execution'])

    def test_fixture_apply_cannot_construct_an_api_or_mutate_issues(self):
        with tempfile.TemporaryDirectory() as tmp, patch('report.GitHub') as api, \
                patch('sys.argv', ['report', '--fixtures', '--apply', '--output', tmp]):
            with self.assertRaisesRegex(ValueError, 'dry-run only'):
                main()
            api.assert_not_called()

    def test_isolated_incident_health_is_retained_in_report_outputs(self):
        from incidents import body_for
        def bounded(state):
            if state['kind'] == 'compatibility':
                raise ValueError('oversized')
            return body_for(state)
        with tempfile.TemporaryDirectory() as tmp, patch('incidents.body_for', side_effect=bounded):
            write_outputs(Path(tmp), RUNS[:2], REGISTRY, NOW)
            index = json.loads((Path(tmp) / 'results-index.json').read_text())
            actions = json.loads((Path(tmp) / 'incident-actions.json').read_text())['actions']
            self.assertTrue(any(a['id'].startswith('incident-size:') for a in index['health']))
            self.assertTrue(any('exceeds' in a['body'] for a in actions))
            self.assertTrue((Path(tmp) / 'compatibility.md').is_file())

    def test_unresolved_incident_remains_in_customer_outputs_after_artifact_history_expires(self):
        from incidents import REPORTER_USER, reconcile
        failed = copy.deepcopy(RUNS[1])
        action = reconcile([failed], [], REGISTRY, NOW)['actions'][0]
        issue = dict(number=42, body=action['body'], state=action['state'], labels=action['labels'], user=dict(REPORTER_USER))
        later = timestamp('2026-11-28T12:00:00Z')
        with tempfile.TemporaryDirectory() as tmp:
            result = write_outputs(Path(tmp), [], REGISTRY, later, [issue])
            index = json.loads((Path(tmp) / 'results-index.json').read_text())
            page = (Path(tmp) / 'compatibility.md').read_text()
        self.assertEqual(index['observations'], [])
        self.assertEqual(len(index['known_bad']), 1)
        bad = index['known_bad'][0]
        self.assertEqual(bad['status'], 'open')
        self.assertEqual(bad['tuple'], [['openai', 'upstream', '1.93.1']])
        self.assertEqual(bad['issue_number'], 42)
        self.assertGreater(bad['age_days'], 30)
        self.assertEqual(bad['scopes'][0]['evidence']['run_id'], failed['run']['run_id'])
        self.assertNotIn('test_ids', bad['scopes'][0]['evidence'])
        self.assertNotIn('reproduction', bad['scopes'][0]['evidence'])
        self.assertIn('Unrecovered known-bad tuples', page)
        self.assertIn('1.93.1', page)
        self.assertIn('aged', page)
        self.assertEqual(len(result['handoffs']), 1)
        # A copied public marker must not inject a customer known-bad record.
        issue['user'] = dict(id=123, login='outsider', type='User')
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(Path(tmp), [], REGISTRY, later, [issue])
            self.assertEqual(json.loads((Path(tmp) / 'results-index.json').read_text())['known_bad'], [])

    def test_issue_writer_only_upserts_labels_and_issues_and_preserves_human_labels(self):
        class FakeAPI:
            def __init__(self):
                self.calls = []
            def request(self, path, data=None, method='GET'):
                self.calls.append((path, data, method))
                if method == 'GET':
                    return {'labels': [{'name': 'human-triaged'}, {'name': 'compat:release-pending'}]}
                return {'number': 42}
        api = FakeAPI()
        action = dict(number=42, title='Recovered', body='Evidence', state='closed',
                      labels=['compatibility', 'compat:contract-regression'])
        apply_actions(api, [action])
        patches = [(p, d) for p, d, m in api.calls if m == 'PATCH']
        self.assertEqual(len(patches), 1)
        path, payload = patches[0]
        self.assertEqual(path, '/repos/future-agi/traceAI/issues/42')
        self.assertEqual(payload['state'], 'closed')
        self.assertIn('human-triaged', payload['labels'])
        self.assertNotIn('compat:release-pending', payload['labels'])
        self.assertTrue(all('/labels' in p or '/issues/' in p for p, _, _ in api.calls))

    def test_api_is_fixed_repository_only(self):
        with self.assertRaisesRegex(ValueError, 'fixed repository'):
            GitHub('not-a-real-token').request('/repos/outsider/project/issues')

    def test_signed_download_redirect_strips_bearer(self):
        import urllib.request
        req = urllib.request.Request('https://api.github.com/repos/future-agi/traceAI/actions/artifacts/1/zip',
                                     headers={'Authorization':'Bearer not-a-real-token'})
        redirected = SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://example.blob.core.windows.net/file')
        self.assertFalse(redirected.has_header('Authorization'))


class ResumeBoundaryTests(unittest.TestCase):
    """Layout controls are not fabricated executions; API tests never write."""
    setUp = ProvenanceTests.setUp
    metadata = ProvenanceTests.metadata
    def test_weekly_archive_retains_failed_only_and_all_case_retry_layouts(self):
        # 21 pilot cells, 18 retained files/cell plus shared/root references.
        # The real weekly replay has 399 members; full retained retries have 772.
        originals = {'results.json': b'{}', 'compat-plan.json': b'{}'}
        for cell in range(21):
            for item in range(18):
                originals[f'evidence/case-{cell}/attempt-1/item-{item}.json'] = b'original'
        originals.update({f'locks/shared-{i}.txt': b'lock' for i in range(19)})
        self.assertEqual(len(originals), 399)
        for retried in (range(7), range(21)):
            files = dict(originals)
            for cell in retried:
                for item in range(18):
                    files[f'evidence/case-{cell}/attempt-2/item-{item}.json'] = b'retry'
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, content in files.items():
                    archive.writestr(relative, content)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                extract_archive(memory.getvalue(), root)
                self.assertEqual({p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}, files)
        # Count remains fail-closed and byte/path budgets are unchanged.
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w') as archive:
            for i in range(4097):
                archive.writestr(f'{i}.txt', b'')
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'file/expanded-size'):
            extract_archive(memory.getvalue(), Path(tmp))

    def test_overflowing_timestamp_run_is_isolated_from_healthy_run(self):
        good, archive = bundle_archive(RUNS[-1])
        bad = copy.deepcopy(good)
        bad['run'].update(run_id='2000', url=f'https://github.com/{REPO}/actions/runs/2000',
                          completed_at='9999-12-31T23:59:59-01:00')
        _, bad_archive = bundle_archive(bad)
        metadata = [dict(self.metadata(), created_at='2026-10-28T04:00:00Z'),
                    dict(self.metadata(), id=2000, html_url=bad['run']['url'], created_at='2026-10-28T04:00:00Z')]
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/workflows/'):
                    return metadata
                ident = int(path.split('/')[2])
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=ident)]
            def request(self, path, **kwargs):
                return (archive if '/1007/' in path else bad_archive) if path.endswith('/zip') else {'status': 'ahead'}
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual([b['run']['run_id'] for b in bundles], ['1007'])
        self.assertEqual([e['id'] for e in errors], ['invalid-run:2000'])

    def test_active_artifact_discovered_retry_is_not_a_production_failure(self):
        bundle, archive = bundle_archive(RUNS[-1])
        meta = dict(self.metadata(), status='in_progress', run_attempt=2)
        class API:
            def pages(self, path, key, **kwargs):
                if path.startswith('actions/workflows/'):
                    return []
                return [dict(name='compatibility-results', expired=False, size_in_bytes=len(archive), id=123,
                             created_at='2026-10-28T05:10:00Z', workflow_run=dict(id=1007))]
            def request(self, path, **kwargs):
                return archive if path.endswith('/zip') else meta
        bundles, errors = github_results(API(), REGISTRY, NOW)
        self.assertEqual((bundles, errors), ([], []))
        self.strict.assert_not_called()

    def test_selftest_workflow_run_and_apply_cannot_reconcile_or_write_heartbeat(self):
        from report import main
        bundle = copy.deepcopy(RUNS[-1])
        bundle['run']['self_test'] = True
        bundle, archive = bundle_archive(bundle)
        # The retained plan and aggregate must agree, not simply a self_test bit.
        raw = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(raw, 'w') as dest:
            plan = json.loads(source.read('compat-plan.json'))
            plan['self_test'] = True
            plan_bytes = json.dumps(plan).encode()
            bundle['run']['plan_sha256'] = hashlib.sha256(plan_bytes).hexdigest()
            for entry in source.infolist():
                content = source.read(entry)
                if entry.filename == 'results.json':
                    content = json.dumps(bundle).encode()
                elif entry.filename == 'compat-plan.json':
                    content = plan_bytes
                dest.writestr(entry.filename, content)
        meta = dict(self.metadata(), created_at='2026-10-28T04:00:00Z')
        class API:
            def pages(self, path, key=None, **kwargs):
                if path.startswith('actions/workflows/'):
                    return [meta]
                if path.startswith('actions/artifacts?'):
                    return []
                if path.startswith('actions/runs/'):
                    return [dict(name='compatibility-results', expired=False, size_in_bytes=len(raw.getvalue()), id=123)]
                raise AssertionError('self-test must not even read incident context')
            def request(self, path, data=None, method='GET', **kwargs):
                if method != 'GET':
                    raise AssertionError('self-test attempted a write')
                return raw.getvalue() if path.endswith('/zip') else {'status': 'ahead'}
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as tmp, \
                    patch('report.GitHub', return_value=API()), patch('report.reconcile') as reconcile, \
                    patch('report.apply_actions') as writes, patch('builtins.print'), \
                    patch.dict('os.environ', dict(COMPAT_REPORT_ALLOW_WRITES='true', GITHUB_REF='refs/heads/main',
                                                  GITHUB_REPOSITORY=REPO, GITHUB_EVENT_NAME='workflow_run')), \
                    patch('sys.argv', ['report', '--github', '--trigger-run-id', '1007', '--output', tmp] + (['--apply'] if apply else [])):
                main()
                reconcile.assert_not_called()
                writes.assert_not_called()
                decision = json.loads((Path(tmp) / 'trigger-decision.json').read_text())
                self.assertFalse(decision['production'])
                self.assertEqual(decision['reason'], 'authenticated_self_test')
                self.assertEqual(json.loads((Path(tmp) / 'incident-actions.json').read_text())['actions'], [])

    def test_trigger_gate_is_revalidated_by_apply_and_workflow_requires_production_output(self):
        from incidents import reconcile
        for decision, applied in [('production', True), ('unverified_trigger', False)]:
            def results(api, registry, now, selected, *, plans, diagnostics):
                diagnostics['trigger'] = decision
                return ([RUNS[-1]] if applied else []), []
            class API:
                def pages(self, *args, **kwargs):
                    return []
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp, \
                    patch('report.GitHub', return_value=API()), patch('report.github_results', side_effect=results), \
                    patch('report.apply_actions') as writes, patch('builtins.print'), \
                    patch.dict('os.environ', dict(COMPAT_REPORT_ALLOW_WRITES='true', GITHUB_REF='refs/heads/main',
                                                  GITHUB_REPOSITORY=REPO, GITHUB_EVENT_NAME='workflow_run')), \
                    patch('sys.argv', ['report', '--github', '--apply', '--trigger-run-id', '1007', '--output', tmp]):
                main()
                self.assertEqual(writes.called, applied)
        workflow = (ROOT / '.github/workflows/compatibility-report.yml').read_text()
        self.assertIn("needs.render.outputs.production == 'true'", workflow)
        self.assertEqual(workflow.count('--trigger-run-id "$CANARY_TRIGGER_RUN_ID"'), 2)
        self.assertIn('production: ${{ steps.evidence.outputs.production }}', workflow)

    def test_preexecution_missing_suite_preserves_unrelated_executed_contract(self):
        failed = copy.deepcopy(RUNS[1])
        case = failed['cases'][0]
        case.update(status='blocked', resolved=[], dependency_lock=None)
        case['failure'].update(kind='resolution', test_ids=[])
        case['contract'].update(tests=[], junit_paths=[], scenarios=[], collected=0, executed=0, suite_sha256=hashlib.sha256(b'').hexdigest())
        registry = copy.deepcopy(REGISTRY)
        next(t for t in registry['integrations'] if t['id'] == 'python-openai')['contract_test']['paths'] = ['missing/test_absent.py']
        class Empty:
            def request(self, path):
                return dict(truncated=False, tree=[])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(snapshot_source(Empty(), failed, registry, Path(tmp)), Path(tmp))
        # A different healthy integration still needs its complete authenticated suite.
        api, healthy_registry = CanonicalBoundaryTests().source_api()
        next(t for t in healthy_registry['integrations'] if t['id'] == 'python-anthropic')['contract_test']['paths'] = ['missing/test_absent.py']
        missing = copy.deepcopy(case)
        missing.update(integration_id='python-anthropic', case_id='anthropic-blocked')
        mixed = copy.deepcopy(RUNS[-1])
        mixed['cases'].append(missing)
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_source(api, mixed, healthy_registry, Path(tmp))
            self.assertTrue((Path(tmp) / 'tests/test_trusted.py').is_file())
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'missing registered'):
            snapshot_source(Empty(), RUNS[-1], registry, Path(tmp))

    def test_junit_nested_and_parameterized_identities_match_canonical_semantics(self):
        for classname, name, expected in [
                ('tests.test_x', 'test_y', 'tests/test_x.py::test_y'),
                ('tests.test_x.TestOuter', 'test_y[p.q]', 'tests/test_x.py::TestOuter::test_y[p.q]'),
                ('tests.test_x.TestOuter.TestInner', 'test_y[p.q]', 'tests/test_x.py::TestOuter::TestInner::test_y[p.q]')]:
            raw = f'<testsuite><testcase file="tests/test_x.py" classname="{classname}" name="{name}"/></testsuite>'.encode()
            self.assertEqual(junit_outcomes(raw), {expected: 'passed'})
        # Declaration rejection cannot depend on the XML byte encoding.
        xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE testsuite [<!ENTITY x "test_y">]><testsuite><testcase file="tests/test_x.py" name="&x;"/></testsuite>'
        with self.assertRaisesRegex(ValueError, 'DTD|declaration'):
            junit_outcomes(xml.encode('utf-16'))


class ResourceBoundaryTests(unittest.TestCase):
    def test_schema_stops_after_first_error(self):
        from jsonschema import Draft202012Validator
        original = Draft202012Validator.iter_errors
        yielded = []
        def counted(validator, *args, **kwargs):
            for error in original(validator, *args, **kwargs):
                yielded.append(error)
                yield error
        from report import schema_validate
        data = dict(schema_version=1, run=RUNS[-1]['run'], cases=[{} for _ in range(500)])
        with patch.object(Draft202012Validator, 'iter_errors', counted), self.assertRaises(ValueError):
            schema_validate(data, 'results.schema.json')
        self.assertEqual(len(yielded), 1)

    def test_collection_limits_precede_schema_iteration(self):
        from jsonschema import Draft202012Validator
        from report import schema_validate
        invalid = [dict(cases=[{}] * 513), dict(run=dict(planned_case_ids=['x'] * 257)),
                   dict(cases=[dict(contract=dict(tests=[{}] * 10001))])]
        for data in invalid:
            with self.subTest(data_size=len(str(data))), patch.object(Draft202012Validator, 'iter_errors') as iterate:
                with self.assertRaisesRegex(ValueError, 'collection limit'):
                    schema_validate(data, 'results.schema.json')
                iterate.assert_not_called()

    def test_zip_compression_and_deflate_failures_are_validation_errors(self):
        import struct
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('results.json', 'content' * 100)
        valid = data.getvalue()
        unsupported = bytearray(valid)
        struct.pack_into('<H', unsupported, unsupported.index(b'PK\x03\x04') + 8, 99)
        struct.pack_into('<H', unsupported, unsupported.index(b'PK\x01\x02') + 10, 99)
        broken = bytearray(valid)
        offset = 30 + len('results.json')
        broken[offset:offset + 4] = b'\xff\xff\xff\xff'
        for raw in (bytes(unsupported), bytes(broken)):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                extract_archive(raw, Path(tmp))


if __name__ == '__main__':
    unittest.main()
