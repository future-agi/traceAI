import json
import tempfile
import unittest
from pathlib import Path

from run_case import apply_exceptions, copy_suite, execute, failure, junit, normalize, suite_files
from test_plan import NOW, ROOT, SHA, inputs
from plan import generate


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
