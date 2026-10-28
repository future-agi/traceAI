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
            marker = outside / 'private-marker'
            marker.write_text('host-only marker')
            marker.chmod(0o600)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(outside / 'private.sock')); server.listen(1)
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
