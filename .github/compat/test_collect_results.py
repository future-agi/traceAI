import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collect_results import collect, validate_case
from plan import sha256, write_json
from run_case import empty_case, failure, run_header, contract_command
from test_plan import NOW, SHA, inputs, generate, RUNTIMES

SCHEMA = json.loads(Path(__file__).with_name("results.schema.json").read_text())
IDS = ["python/frameworks/openai/tests/test_openai_compatible_spans.py::test_real_sdk",
       "python/frameworks/openai/tests/test_stream_context_manager.py::test_stream"]


def evidence(directory):
    registry, discovery = inputs()
    plan = generate(registry, discovery, SHA, SHA, NOW, self_test=True)
    plan["cases"] = [next(c for c in plan["cases"] if c["integration_id"] == "python-openai")]
    selected = plan["cases"][0]
    selected["requested"][1]["selected_version"] = "0.1.10"
    selected["requested"][2]["selected_version"] = "1.2.1"
    for req in selected["requested"]:
        req["requirement"] = req["name"] + "==" + req["selected_version"]
    plan_dir, artifacts, output = directory / "plan", directory / "cases", directory / "aggregate"
    plan_dir.mkdir(); artifacts.mkdir()
    target = next(t for t in registry["integrations"] if t["id"] == "python-openai")
    row = empty_case(selected, plan, target)
    row.update(status="passed", runtime={k: RUNTIMES["3.11"][k] for k in ("implementation", "version", "os", "arch")},
               resolved=[{"name": r["name"], "role": r["role"], "version": r["selected_version"], "artifact_sha256": "a" * 64}
                         for r in selected["requested"]])
    row["contract"].update(suite_sha256="f" * 64, command=contract_command(target["contract_test"], "junit/attempt1.xml"), scenarios=target["contract_test"]["scenarios"],
        collected=2, executed=2, junit_paths=["junit/attempt1.xml"], tests=[{
        "test_id": node, "outcome": "passed", "duration_seconds": 0.1, "failure_signature": None, "exception_id": None} for node in IDS])
    distributions = [(r["name"], r["selected_version"]) for r in selected["requested"]] + [("transitive-sdk-helper", "1.0.0")]
    from packaging.utils import canonicalize_name
    requirements = "\n".join(sorted(f"{canonicalize_name(n)}=={v} --hash=sha256:{'a'*64}" for n, v in distributions)) + "\n"
    report = {"version": "1", "install": [{"metadata": {"name": n, "version": v},
        "download_info": {"archive_info": {"hashes": {"sha256": "a" * 64}}}} for n, v in distributions]}
    (plan_dir / "locks").mkdir()
    write_json(plan_dir / "locks/case.report.json", report)
    (plan_dir / "locks/case.txt").write_text(requirements)
    selected["resolution"] = {"resolved": row["resolved"], "report": "locks/case.report.json", "requirements": "locks/case.txt",
        "report_sha256": sha256((plan_dir / "locks/case.report.json").read_bytes()), "requirements_sha256": sha256(requirements.encode()), "wheelhouse": "wheels/case"}
    write_json(plan_dir / "registry.json", registry)
    plan["registry_sha256"] = sha256((plan_dir / "registry.json").read_bytes())
    write_json(plan_dir / "compat-plan.json", plan)
    raw = (plan_dir / "compat-plan.json").read_bytes()
    freeze = "\n".join(n + "==" + v for n, v in distributions) + "\n"
    row["dependency_lock"] = {"format": "pip-freeze", "path": "locks/case.freeze.txt", "sha256": sha256(freeze.encode())}
    case_dir = artifacts / ("compatibility-case-" + selected["case_id"] + "-attempt-1")
    (case_dir / "locks").mkdir(parents=True); (case_dir / "junit").mkdir()
    for key in ("report", "requirements"):
        shutil.copyfile(plan_dir / selected["resolution"][key], case_dir / selected["resolution"][key])
    (case_dir / "locks/case.freeze.txt").write_text(freeze)
    write_json(case_dir / ("locks/" + selected["case_id"] + ".collection.json"), dict(nodes=sorted(IDS), deselected=[]))
    (case_dir / "junit/attempt1.xml").write_text('<testsuite>' + ''.join(
        f'<testcase file="{node.split("::")[0]}" name="{node.split("::")[1]}" time="0.1"/>' for node in IDS) + '</testsuite>')
    result = {"schema_version": 1, "run": run_header(plan, raw), "cases": [row]}
    write_json(case_dir / "results.json", result)
    return plan_dir, artifacts, output, case_dir, result


def semantic_check(row, target, root):
    # Synthetic trusted two-node suite for collector units. Workflow cases use
    # the real PR1 interface from the immutable source checkout, not this mock.
    if row["contract"]["suite_sha256"] != "f" * 64:
        raise ValueError("trusted suite digest mismatch")
    if row["contract"]["command"] != contract_command(target["contract_test"], "junit/attempt1.xml"):
        raise ValueError("registered contract command mismatch")
    if {t["test_id"] for t in row["contract"]["tests"]} != set(IDS):
        raise ValueError("registered contract test nodes mismatch")
    if row["contract"]["scenarios"] != target["contract_test"]["scenarios"]:
        raise ValueError("registered scenarios mismatch")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch("collect_results.semantic_validator", return_value=SimpleNamespace(validate_contract=semantic_check))
        self.mock.start(); self.addCleanup(self.mock.stop)
        self.env = patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"})
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop("GITHUB_RUN_ID", None)

    def collect(self, plan, artifacts, output, **kwargs):
        return collect(plan, artifacts, output, SCHEMA, source_root=plan, **kwargs)

    def run_control(self, mutate):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            mutate(data, folder)
            write_json(folder / "results.json", data)
            result, blocked = self.collect(plan, artifacts, output)
            self.assertTrue(blocked)
            self.assertNotEqual(result["cases"][-1]["status"], "passed")
            return result

    def test_complete_schema_valid_evidence_passes_and_stays_unqualified(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, _, _ = evidence(Path(temporary))
            result, blocked = self.collect(plan, artifacts, output)
            self.assertFalse(blocked)
            self.assertEqual(result["cases"][0]["qualification"], "pilot_pending")
            self.assertTrue((output / "locks/case.freeze.txt").is_file())

    def test_requested_resolved_mismatch_can_never_report_latest_pass(self):
        result = self.run_control(lambda data, _: data["cases"][0]["resolved"][0].update(version="3.23.0"))
        self.assertIn("requested_resolved_mismatch", result["cases"][0]["failure"]["summary"])

    def test_selected_incompatibility_is_failed_without_integration_code_change(self):
        def mutate(data, folder):
            row = data["cases"][0]
            row["status"] = "failed"
            row["failure"] = failure("contract", "E AssertionError: upstream removed response field", [IDS[0]])
            row["contract"]["tests"][0].update(outcome="failure", failure_signature=row["failure"]["signature"])
            xml = (folder / "junit/attempt1.xml").read_text().replace('time="0.1"/>', 'time="0.1"><failure>E AssertionError: upstream removed response field</failure></testcase>', 1)
            (folder / "junit/attempt1.xml").write_text(xml)
        result = self.run_control(mutate)
        self.assertEqual(result["cases"][0]["status"], "failed")
        self.assertEqual(result["cases"][0]["failure"]["kind"], "contract")

    def test_missing_case_becomes_explicit_blocked_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, _ = evidence(Path(temporary))
            shutil.rmtree(folder)
            result, blocked = self.collect(plan, artifacts, output)
            self.assertTrue(blocked)
            self.assertIn("Missing planned", result["cases"][0]["failure"]["summary"])

    def test_missing_junit_lock_mutation_source_mismatch_empty_skip_and_qualification_fail(self):
        mutations = [lambda data, folder: (folder / "junit/attempt1.xml").unlink(),
                     lambda data, folder: (folder / "locks/case.freeze.txt").write_text("tampered"),
                     lambda data, folder: data["cases"][0].update(source_sha="2" * 40),
                     lambda data, folder: data["cases"][0]["contract"].update(executed=0),
                     lambda data, folder: data["cases"][0]["contract"]["tests"][0].update(outcome="skipped"),
                     lambda data, folder: data["cases"][0].update(qualification="qualified")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.run_control(mutation)

    def test_report_requirements_and_artifact_identity_are_bound_to_plan(self):
        for mutate in [lambda d, f: (f / "locks/case.report.json").write_text('{}'),
                      lambda d, f: (f / "locks/case.txt").write_text('openai==0.0.1'),
                      lambda d, f: d["cases"][0]["resolved"][0].update(artifact_sha256="b" * 64)]:
            self.run_control(mutate)

    def test_complete_installed_freeze_must_match_even_with_updated_digest(self):
        for old, new in [('openai==3.24.0', 'openai==0.0.1'), ('transitive-sdk-helper==1.0.0\n', ''), ('', 'extra-package==9\n')]:
            def mutate(data, folder, old=old, new=new):
                path = folder / 'locks/case.freeze.txt'
                text = path.read_text()
                path.write_text(text.replace(old, new) if old else text + new)
                data['cases'][0]['dependency_lock']['sha256'] = sha256(path.read_bytes())
            result = self.run_control(mutate)
            self.assertIn('Complete installed distribution set', result['cases'][0]['failure']['summary'])

    def test_partial_suite_digest_and_command_cannot_claim_all_scenarios(self):
        for mutate in [lambda d, f: d['cases'][0]['contract'].update(tests=d['cases'][0]['contract']['tests'][:1], collected=1, executed=1),
                      lambda d, f: d['cases'][0]['contract'].update(suite_sha256='b'*64),
                      lambda d, f: d['cases'][0]['contract']['command'].extend(['-k', 'one_test'])]:
            self.run_control(mutate)

    def test_pass_claim_with_changed_junit_fails_closed(self):
        result = self.run_control(lambda data, folder: (folder / "junit/attempt1.xml").write_text('<testsuite/>'))
        self.assertIn("do not match original JUnit", result["cases"][0]["failure"]["summary"])

    def test_artifact_symlinks_fail_closed(self):
        self.run_control(lambda data, folder: (folder / "evil").symlink_to("/tmp"))

    def test_reversed_fail_then_pass_is_not_ordinary_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan_dir, _, _, _, data = evidence(Path(temporary))
            plan = json.loads((plan_dir / 'compat-plan.json').read_text())
            second = copy.deepcopy(data['cases'][0]); second['attempt'] = 2
            data['cases'][0].update(status='failed', failure=failure('contract', 'intermittent'))
            data['cases'].append(second)
            for order in [data['cases'], list(reversed(data['cases']))]:
                with self.assertRaisesRegex(ValueError, 'labelled flaky'):
                    validate_case({**data, 'cases': order}, plan, (plan_dir / 'compat-plan.json').read_bytes(), plan['cases'][0], SCHEMA)

    def test_stale_pass_cannot_mask_failed_current_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            cid = data['cases'][0]['case_id']
            jobs = [dict(id=1, name=f'Canary ({cid})', run_attempt=1, status='completed', conclusion='success'),
                    dict(id=2, name=f'Canary ({cid})', run_attempt=2, status='completed', conclusion='failure')]
            with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                result, blocked = self.collect(plan, artifacts, output, jobs=jobs)
            self.assertTrue(blocked)
            self.assertEqual([(r['attempt'], r['status']) for r in result['cases']], [(1, 'passed'), (2, 'blocked')])

    def test_old_pass_is_reused_only_for_job_not_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, _, data = evidence(Path(temporary))
            cid = data['cases'][0]['case_id']
            jobs = [dict(id=1, name=f'Canary ({cid})', run_attempt=1, status='completed', conclusion='success')]
            with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                result, blocked = self.collect(plan, artifacts, output, jobs=jobs)
            self.assertFalse(blocked)
            self.assertEqual(result['cases'][0]['attempt'], 1)

    def test_partial_retry_output_does_not_destroy_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            before = (folder / 'results.json').read_bytes()
            retry = artifacts / folder.name.replace('attempt-1', 'attempt-2')
            retry.mkdir(); (retry / 'install.log').write_text('interrupted')
            with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                result, blocked = self.collect(plan, artifacts, output)
            self.assertTrue(blocked)
            self.assertEqual((folder / 'results.json').read_bytes(), before)
            self.assertEqual([(r['attempt'], r['status']) for r in result['cases']], [(1, 'passed'), (2, 'blocked')])

    def test_full_collection_rejects_partial_execution_even_when_function_check_allows_it(self):
        def mutate(data, folder):
            contract = data['cases'][0]['contract']
            contract.update(tests=contract['tests'][:1], collected=1, executed=1)
            path, name = IDS[0].split('::')
            (folder / 'junit/attempt1.xml').write_text(f'<testsuite><testcase file="{path}" name="{name}" time="0.1"/></testsuite>')
        # Function-level AST validation alone cannot detect a deselected parameter.
        with patch('collect_results.semantic_validator', return_value=SimpleNamespace(validate_contract=lambda *args: None)):
            result = self.run_control(mutate)
        self.assertIn('full collected identities', result['cases'][0]['failure']['summary'])

    def test_github_success_clone_is_not_a_rerun_but_new_or_failed_execution_is(self):
        for changed in (None, 'steps', 'conclusion', 'created_at'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                plan, artifacts, output, _, data = evidence(Path(temporary))
                cid = data['cases'][0]['case_id']
                old = dict(id=1, name=f'Canary ({cid})', run_attempt=1, status='completed', conclusion='success',
                    created_at='2026-10-28T01:00:00Z', started_at='2026-10-28T01:00:01Z', completed_at='2026-10-28T01:01:00Z',
                    runner_id=123, head_sha=SHA, steps=[dict(name='contract', conclusion='success', started_at='2026-10-28T01:00:02Z')])
                clone = {**old, 'id': 2, 'run_attempt': 2, 'created_at': '2026-10-28T02:00:00Z'}
                if changed == 'steps': clone['steps'] = [dict(name='checkout', conclusion='failure')]
                if changed == 'conclusion': clone['conclusion'] = 'failure'
                if changed == 'created_at': clone['created_at'] = '2026-10-28T00:59:00Z'
                with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                    result, blocked = self.collect(plan, artifacts, output, jobs=[old, clone])
                self.assertEqual(bool(blocked), changed is not None)
                self.assertEqual(result['cases'][-1]['status'], 'passed' if changed is None else 'blocked')
