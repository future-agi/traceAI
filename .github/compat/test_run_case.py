import json
import tempfile
import unittest
from pathlib import Path

from run_case import apply_exceptions, copy_suite, execute, failure, junit, normalize, suite_files
from test_plan import NOW, ROOT, SHA, inputs, generate


class RunnerTests(unittest.TestCase):
    def test_missing_resolution_or_suite_is_blocked_not_passed(self):
        registry, discovery = inputs()
        plan = generate(registry, discovery, SHA, SHA, NOW)
        case = plan["cases"][0]
        target = next(t for t in registry["integrations"] if t["id"] == case["integration_id"])
        with tempfile.TemporaryDirectory() as directory:
            row = execute(ROOT, Path(directory), case, plan, target, Path(directory), Path("not-executed"))
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["failure"]["kind"], "resolution")
        self.assertEqual(row["contract"]["executed"], 0)

    def test_suite_digest_includes_helpers_config_and_no_source_shadow(self):
        registry, _ = inputs()
        target = next(t for t in registry["integrations"] if t["id"] == "python-langchain")
        paths = suite_files(ROOT, target["contract_test"])
        self.assertIn("python/frameworks/langchain/tests/regression/_helpers.py", paths)
        self.assertIn("python/frameworks/langchain/tests/regression/conftest.py", paths)
        self.assertIn("python/frameworks/langchain/pyproject.toml", paths)
        self.assertFalse(any("traceai_langchain/" in p or "fi_instrumentation/" in p for p in paths))
        with tempfile.TemporaryDirectory() as directory:
            digest = copy_suite(ROOT, Path(directory), paths)
            self.assertEqual(len(digest), 64)

    def test_canonical_junit_parameter_ids_and_distinct_failures(self):
        xml = '''<testsuites><testsuite><testcase classname="tests.test_sdk.TestSDK" name="test_stream[async]" file="tests/test_sdk.py" time="0.1"><failure message="AssertionError">E   AssertionError: missing token usage</failure></testcase><testcase classname="tests.test_sdk" name="test_skip" file="tests/test_sdk.py"><skipped/></testcase></testsuite></testsuites>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            path.write_text(xml)
            tests, details = junit(path)
        self.assertEqual(tests[0]["test_id"], "tests/test_sdk.py::TestSDK::test_stream[async]")
        self.assertEqual(tests[0]["outcome"], "failure")
        self.assertNotEqual(details[0]["signature"], failure("contract", "AssertionError: changed response", [tests[0]["test_id"]])["signature"])
        self.assertEqual(tests[1]["outcome"], "skipped")

    def test_normalization_removes_machine_details_not_actual_assertion(self):
        a = failure("contract", "AssertionError missing usage <object at 0x123> /home/runner/work/app.py 2026-10-28T00:00:00Z", ["tests/test.py::test_sdk"])
        b = failure("contract", "AssertionError missing usage <object at 0x456> /tmp/app.py 2026-10-29T01:00:00Z", ["tests/test.py::test_sdk"])
        self.assertEqual(a["signature"], b["signature"])
        self.assertNotEqual(a["signature"], failure("contract", "AssertionError missing model", ["tests/test.py::test_sdk"])["signature"])
        self.assertNotIn("sensitive-value", normalize("api_key=sensitive-value"))

    def test_long_failures_do_not_collapse_distinct_later_behavior(self):
        prefix = "AssertionError: " + "same " * 500
        a = failure("contract", prefix + "missing model", ["tests/test.py::test_sdk"])
        b = failure("contract", prefix + "missing usage", ["tests/test.py::test_sdk"])
        self.assertEqual(a["summary"], b["summary"])
        self.assertNotEqual(a["signature"], b["signature"])

    def test_junit_path_traversal_and_duplicates_fail_closed(self):
        for xml in ('<testsuite><testcase file="../../bad.py" name="test"/></testsuite>',
                    '<testsuite><testcase name="test"/><testcase name="test"/></testsuite>',
                    '<!DOCTYPE x><testsuite/>'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "junit.xml"
                path.write_text(xml)
                with self.assertRaises(ValueError):
                    junit(path)

    def test_case_aggregation_retains_complete_per_test_signatures(self):
        from run_case import aggregate_failure
        prefix = 'AssertionError: ' + 'same ' * 500
        a = failure('contract', prefix + 'missing model', ['tests/test.py::test_sdk'])
        b = failure('contract', prefix + 'missing usage', ['tests/test.py::test_sdk'])
        self.assertEqual(a['summary'], b['summary'])
        self.assertNotEqual(aggregate_failure([a], a['test_ids'])['signature'],
                            aggregate_failure([b], b['test_ids'])['signature'])
        self.assertEqual(aggregate_failure([a, b], a['test_ids'])['signature'],
                         aggregate_failure([b, a], a['test_ids'])['signature'])

    @unittest.skipUnless(__import__('sys').platform == 'linux', 'Linux CI proves the kernel boundary')
    def test_dependency_startup_hook_sees_unprivileged_isolated_filesystem_and_sockets(self):
        import os
        import shutil
        import socket
        import subprocess
        import sys
        from run_case import sandbox
        self.assertIsNotNone(shutil.which('bwrap'), 'Sandbox must be installed; no host fallback')
        with tempfile.TemporaryDirectory(prefix='compat-host-private-') as outside:
            outside = Path(outside)
            # These world-accessible fixture paths would remain reachable across
            # a netns alone. Their absence proves the filesystem boundary, not
            # merely ordinary DAC denial to host nobody.
            outside.chmod(0o755)
            marker = outside / 'world-readable-host-marker'
            marker.write_text('host-only marker')
            marker.chmod(0o644)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(outside / 'private.sock')); server.listen(1)
            (outside / 'private.sock').chmod(0o777)
            try:
                with tempfile.TemporaryDirectory(prefix='compat-hook-') as temporary:
                    root = Path(temporary); root.chmod(0o755)
                    shutil.copyfile(ROOT / '.github/compat/block_network.sh', root / 'block_network.sh')
                    consumer, output, venv = root / 'consumer', root / 'output', root / 'venv'
                    consumer.mkdir(); output.mkdir(mode=0o777); output.chmod(0o777)
                    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
                    python = venv / 'bin/python'
                    site = venv / 'lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
                    hook = '''import json, pathlib, socket
mapping = pathlib.Path('/proc/self/uid_map').read_text().split()
assert mapping == ['0', '65534', '1'], mapping
assert not pathlib.Path(MARKER).exists(), 'Host private file visible'
s = socket.socket(socket.AF_UNIX)
try:
    s.connect(SOCKET)
except OSError:
    pass
else:
    raise AssertionError('Host pathname socket reachable')
finally:
    s.close()
pathlib.Path(OUTPUT).write_text(json.dumps(dict(host_uid=65534, private_file='absent', host_socket='denied')))
'''.replace('MARKER', repr(str(marker))).replace('SOCKET', repr(str(outside / 'private.sock'))).replace('OUTPUT', repr(str(output / 'startup.json')))
                    (site / 'compat_hook.py').write_text(hook)
                    (site / 'startup.pth').write_text('import compat_hook\n')
                    # This is dependency startup code, not a test fixture imported
                    # after Python has already had an opportunity to exfiltrate.
                    for command in ([str(python), '-I', '-c', 'import importlib.metadata; print("inventory")'],
                                    [str(python), '-I', '-c', 'import runpy; print("freeze")']):
                        result = subprocess.run(sandbox(root, output, Path(sys.base_prefix).resolve(), consumer, command),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('"host_uid": 65534', result.stderr)
                        self.assertEqual(json.loads((output / 'startup.json').read_text())['host_socket'], 'denied')
                    self.assertFalse((root / 'escape-write').exists())
            finally:
                server.close()

    def test_parent_retains_nonzero_exit_even_with_passing_junit(self):
        import subprocess
        from unittest.mock import patch
        from run_case import invoke
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'sandbox.xml'; source.write_text('<testsuite/>')
            retained = root / 'junit/test.xml'
            with patch('run_case.subprocess.run', return_value=subprocess.CompletedProcess([], 7)):
                record = invoke(['not-executed'], cwd=root, log=root / 'run.log', evidence=source,
                                retained=retained, output=root)
            self.assertEqual(record['exit_code'], 7)
            self.assertEqual(Path(str(retained) + '.exit-code').read_text(), '7\n')
            self.assertEqual(retained.read_bytes(), source.read_bytes())

    def test_collection_diagnostics_retain_affected_identity_and_distinct_signatures(self):
        import subprocess
        import sys
        from run_case import COLLECTION_CODE, collection_failure
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            signatures = []
            for missing in ('_error_description', 'GatewayError'):
                (root / 'test_import.py').write_text('raise ImportError("cannot import name ' + missing + '")\n')
                output = root / 'collection.json'
                process = subprocess.run([sys.executable, '-I', '-c', COLLECTION_CODE, str(output),
                    'test_import.py', '--rootdir=.', '-c', '/dev/null', '--collect-only'], cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self.assertNotEqual(process.returncode, 0)
                data = json.loads(output.read_text())
                self.assertEqual(data['errors'][0]['test_id'], 'test_import.py')
                self.assertIn(missing, data['errors'][0]['diagnostic'])
                diagnostic = collection_failure(data, root / 'absent.log')
                self.assertEqual(diagnostic['test_ids'], ['test_import.py'])
                self.assertIn(missing, diagnostic['summary'])
                signatures.append(diagnostic['signature'])
            self.assertNotEqual(*signatures)

    def test_real_startup_and_initial_conftest_failures_retain_diagnostics(self):
        import subprocess
        import sys
        from run_case import COLLECTION_CODE, collection_failure
        from plan import clean_env
        for phase in ('plugin', 'conftest'):
            signatures = []
            for missing in ('_error_description', 'GatewayError'):
                with self.subTest(phase=phase, missing=missing), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    module = 'broken_plugin.py' if phase == 'plugin' else 'conftest.py'
                    (root / module).write_text('raise ImportError("cannot import name ' + missing + '")\n')
                    (root / 'test_api.py').write_text('def test_api(): assert True\n')
                    output, log = root / 'collection.json', root / 'collection.log'
                    # Explicit fixture-only plugin import path; -I prevents an
                    # implicit working-directory or PYTHONPATH import otherwise.
                    code = 'import sys; sys.path.insert(0, sys.argv.pop(1));\n' + COLLECTION_CODE
                    command = [sys.executable, '-I', '-c', code, str(root), str(output),
                               'test_api.py', '--rootdir=.', '-c', '/dev/null', '--collect-only']
                    if phase == 'plugin':
                        command.extend(['-p', 'broken_plugin'])
                    with log.open('w') as stream:
                        process = subprocess.run(command, cwd=root, env=clean_env(), stdout=stream, stderr=subprocess.STDOUT)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertFalse(output.exists(), 'Probe must exercise the pre-collection fallback')
                    diagnostic = collection_failure({}, log)
                    self.assertIn(missing, diagnostic['summary'])
                    self.assertIn(module, diagnostic['test_ids'])
                    self.assertNotIn(str(root), diagnostic['summary'])
                    signatures.append(diagnostic['signature'])
            self.assertNotEqual(*signatures)

    def test_invalid_first_attempt_schema_does_not_prevent_frozen_retry(self):
        import copy
        import os
        import sys
        from unittest.mock import patch
        from run_case import main
        from test_collect_results import evidence, trusted_validator
        for corruption in ('json', 'schema', 'deep-json'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                plan, _, output, previous, data = evidence(Path(temporary))
                case_id = data['cases'][0]['case_id']
                retry = copy.deepcopy(data['cases'][0])
                retry['attempt'] = 2
                if corruption == 'schema':
                    del data['cases'][0]['runtime']
                    (previous / 'results.json').write_text(json.dumps(data))
                elif corruption == 'deep-json':
                    (previous / 'results.json').write_text('[' * 2000 + '0' + ']' * 2000)
                else:
                    (previous / 'results.json').write_text('{broken')
                before = (previous / 'results.json').read_bytes()
                argv = ['run_case', '--root', str(plan / 'source'), '--plan-dir', str(plan),
                        '--case-id', case_id, '--out', str(output), '--previous', str(previous)]
                with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}), \
                     patch('run_case.subprocess.check_output', return_value=SHA), \
                     patch('run_case.semantic_validator', return_value=trusted_validator()), \
                     patch('run_case.execute', return_value=retry) as execution:
                    self.assertEqual(main(), 1)
                execution.assert_called_once()
                self.assertEqual(execution.call_args.args[2]['case_id'], case_id)
                self.assertEqual(execution.call_args.args[-1], 2)
                result = json.loads((output / 'results.json').read_text())
                self.assertEqual(result['cases'][0]['status'], 'flaky')
                self.assertEqual((previous / 'results.json').read_bytes(), before)

    def test_canonical_exception_semantics_are_delegated_before_attribution(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        ex = {'id': 'exact', 'test_ids': ['tests/test.py::test_sdk[old]']}
        target = {'exceptions': [ex]}
        tests = [dict(test_id=ex['test_ids'][0], outcome='failure', exception_id=None)]
        validator = SimpleNamespace(validate_exception=Mock(), judge_tests=Mock(return_value='quarantined'),
                                    applicable=Mock(return_value=True))
        self.assertEqual(apply_exceptions(tests, target, [], {}, validator), 'quarantined')
        validator.validate_exception.assert_called_once_with(ex, target)
        validator.judge_tests.assert_called_once_with(tests, target, {}, [])
        self.assertEqual(tests[0]['exception_id'], 'exact')
        # Expiry, omitted parameters, overlap and signatures are all canonical
        # failures. None can attach an exception before PR1 has accepted the set.
        for message in ('expired', 'excepted test did not execute', 'overlap', 'unmatched signature'):
            tests[0]['exception_id'] = None
            validator.judge_tests.side_effect = ValueError(message)
            with self.assertRaisesRegex(ValueError, message):
                apply_exceptions(tests, target, [], {}, validator)
            self.assertIsNone(tests[0]['exception_id'])

    def test_complete_quarantined_suite_has_exercised_scenarios(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from run_case import finish_contract
        target = dict(exceptions=[], contract_test=dict(scenarios=['real-sdk', 'privacy'], minimum_executed=1))
        row = dict(contract=dict(tests=[dict(test_id='tests/test.py::test_sdk', outcome='failure')], executed=1,
                                 unexpected_skips=0, scenarios=[]), resolved=[], runtime={}, notes=[], failure=None)
        validator = SimpleNamespace(validate_contract=Mock(), judge_tests=Mock(return_value='quarantined'))
        with patch('run_case.semantic_validator', return_value=validator):
            finish_contract(row, target, ROOT, dict(nodes=['tests/test.py::test_sdk'], deselected=[]), 1, [])
        self.assertEqual(row['status'], 'quarantined')
        self.assertEqual(row['contract']['scenarios'], ['real-sdk', 'privacy'])
        validator.validate_contract.assert_called_once()

    def test_source_sha_is_checked_before_loading_any_source_validator_code(self):
        from unittest.mock import patch
        import sys
        import run_case
        from test_collect_results import evidence
        with tempfile.TemporaryDirectory() as temporary:
            plan, _, output, _, data = evidence(Path(temporary))
            argv = ['run_case', '--root', str(plan / 'source'), '--plan-dir', str(plan),
                    '--case-id', data['cases'][0]['case_id'], '--out', str(output)]
            with patch.object(sys, 'argv', argv), patch('run_case.subprocess.check_output', return_value='2' * 40), \
                 patch('run_case.semantic_validator', side_effect=AssertionError('must not load mismatched source code')) as validator:
                with self.assertRaisesRegex(ValueError, 'Source SHA does not match frozen plan'):
                    run_case.main()
                validator.assert_not_called()
