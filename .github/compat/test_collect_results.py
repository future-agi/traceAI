import copy
import json
import tempfile
import unittest
from pathlib import Path

from collect_results import collect
from plan import generate, sha256, write_json
from run_case import empty_case, failure, run_header
from test_plan import NOW, SHA, inputs

SCHEMA = json.loads(Path(__file__).with_name("results.schema.json").read_text())


def evidence(directory):
    registry, discovery = inputs()
    plan = generate(registry, discovery, SHA, SHA, NOW, self_test=True)
    # One cell isolates each negative control; no production target inventory fork.
    plan["cases"] = [next(c for c in plan["cases"] if c["integration_id"] == "python-openai")]
    selected = plan["cases"][0]
    selected["requested"][1]["selected_version"] = "0.1.10"
    selected["requested"][2]["selected_version"] = "1.2.1"
    for req in selected["requested"]:
        req["requirement"] = req["name"] + "==" + req["selected_version"]
    plan_dir, artifacts, output = directory / "plan", directory / "cases", directory / "aggregate"
    plan_dir.mkdir()
    artifacts.mkdir()
    write_json(plan_dir / "registry.json", registry)
    plan["registry_sha256"] = sha256((plan_dir / "registry.json").read_bytes())
    write_json(plan_dir / "compat-plan.json", plan)
    raw = (plan_dir / "compat-plan.json").read_bytes()
    target = next(t for t in registry["integrations"] if t["id"] == "python-openai")
    row = empty_case(selected, plan, target)
    row.update(status="passed", runtime={"implementation": "cpython", "version": "3.11.9", "os": "linux", "arch": "x86_64"},
               resolved=[{"name": r["name"], "role": r["role"], "version": r["selected_version"]} for r in selected["requested"]])
    row["contract"].update(collected=1, executed=1, junit_paths=["junit/case.xml"], tests=[{
        "test_id": "python/frameworks/openai/tests/test_openai_compatible_spans.py::test_real_sdk", "outcome": "passed", "duration_seconds": 0.1,
        "failure_signature": None, "exception_id": None}])
    lock = b"openai==3.24.0\ntraceai-openai==0.1.10\nfi-instrumentation-otel==1.2.1\n"
    row["dependency_lock"] = {"format": "pip-freeze", "path": "locks/case.txt", "sha256": sha256(lock)}
    case_dir = artifacts / ("compatibility-case-" + selected["case_id"])
    (case_dir / "locks").mkdir(parents=True)
    (case_dir / "junit").mkdir()
    (case_dir / "locks/case.txt").write_bytes(lock)
    (case_dir / "junit/case.xml").write_text('<testsuite><testcase file="python/frameworks/openai/tests/test_openai_compatible_spans.py" name="test_real_sdk" time="0.1"/></testsuite>')
    result = {"schema_version": 1, "run": run_header(plan, raw), "cases": [row]}
    write_json(case_dir / "results.json", result)
    return plan_dir, artifacts, output, case_dir, result


class CollectorTests(unittest.TestCase):
    def run_control(self, mutate):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            mutate(data, folder)
            write_json(folder / "results.json", data)
            result, blocked = collect(plan, artifacts, output, SCHEMA)
            self.assertTrue(blocked)
            self.assertNotEqual(result["cases"][0]["status"], "passed")
            return result

    def test_complete_schema_valid_evidence_passes_and_stays_unqualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, _, _ = evidence(Path(temporary))
            result, blocked = collect(plan, artifacts, output, SCHEMA)
            self.assertFalse(blocked)
            self.assertEqual(result["cases"][0]["qualification"], "pilot_pending")
            self.assertTrue((output / "locks/case.txt").is_file())
            self.assertTrue((output / "compat-plan.json").is_file())

    def test_requested_resolved_mismatch_can_never_report_latest_pass(self):
        result = self.run_control(lambda data, _: data["cases"][0]["resolved"][0].update(version="3.23.0"))
        self.assertIn("requested_resolved_mismatch", result["cases"][0]["failure"]["summary"])

    def test_selected_incompatibility_is_failed_without_integration_code_change(self):
        def mutate(data, folder):
            row = data["cases"][0]
            row["status"] = "failed"
            row["failure"] = failure("contract", "E AssertionError: upstream removed response field", ["python/frameworks/openai/tests/test_openai_compatible_spans.py::test_real_sdk"])
            row["contract"]["tests"][0].update(outcome="failure", failure_signature=row["failure"]["signature"])
            (folder / "junit/case.xml").write_text('<testsuite><testcase file="python/frameworks/openai/tests/test_openai_compatible_spans.py" name="test_real_sdk" time="0.1"><failure>E AssertionError: upstream removed response field</failure></testcase></testsuite>')
        result = self.run_control(mutate)
        self.assertEqual(result["cases"][0]["status"], "failed")
        self.assertEqual(result["cases"][0]["failure"]["kind"], "contract")

    def test_missing_case_becomes_explicit_blocked_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, _ = evidence(Path(temporary))
            import shutil
            shutil.rmtree(folder)
            result, blocked = collect(plan, artifacts, output, SCHEMA)
            self.assertTrue(blocked)
            self.assertEqual(result["cases"][0]["status"], "blocked")
            self.assertIn("Missing planned", result["cases"][0]["failure"]["summary"])

    def test_missing_junit_lock_mutation_source_mismatch_empty_and_skip_fail(self):
        mutations = [lambda data, folder: (folder / "junit/case.xml").unlink(),
                     lambda data, folder: (folder / "locks/case.txt").write_text("tampered"),
                     lambda data, folder: data["cases"][0].update(source_sha="2" * 40),
                     lambda data, folder: data["cases"][0]["contract"].update(executed=0),
                     lambda data, folder: data["cases"][0]["contract"]["tests"][0].update(outcome="skipped"),
                     lambda data, folder: data["cases"][0].update(qualification="qualified")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.run_control(mutation)

    def test_pass_claim_with_changed_junit_fails_closed(self):
        result = self.run_control(lambda data, folder: (folder / "junit/case.xml").write_text('<testsuite><testcase file="python/frameworks/openai/tests/test_openai_compatible_spans.py" name="test_real_sdk" time="0.1"><failure>AssertionError</failure></testcase></testsuite>'))
        self.assertIn("do not match original JUnit", result["cases"][0]["failure"]["summary"])

    def test_artifact_symlinks_fail_closed(self):
        self.run_control(lambda data, folder: (folder / "evil").symlink_to("/tmp"))

    def test_fail_then_pass_is_not_ordinary_pass(self):
        def mutate(data, _):
            first = data["cases"][0]
            second = copy.deepcopy(first)
            first.update(status="failed", failure=failure("contract", "AssertionError: intermittent"))
            second["attempt"] = 2
            data["cases"].append(second)
        self.run_control(mutate)
