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
from run_case import empty_case, failure, run_header, contract_command, invocation_path, junit
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
    source = plan_dir / "source/.github/compat"
    source.mkdir(parents=True)
    shutil.copyfile(plan_dir / "registry.json", source / "registry.json")
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
    collection_path = "locks/" + selected["case_id"] + "-attempt1.collection.json"
    write_json(case_dir / collection_path, dict(nodes=sorted(IDS), deselected=[], errors=[]))
    (case_dir / "junit/attempt1.xml").write_text('<testsuite>' + ''.join(
        f'<testcase file="{node.split("::")[0]}" name="{node.split("::")[1]}" time="0.1"/>' for node in IDS) + '</testsuite>')
    for discovered in row["discovery"]:
        discovered["resolved_version"] = next(r["version"] for r in row["resolved"] if r["name"] == discovered["name"])
    (case_dir / 'junit/attempt1.xml.exit-code').write_text('0\n')
    (case_dir / (collection_path + '.exit-code')).write_text('0\n')
    invocation = dict(exit_code=0, timed_out=False, started_at=plan["generated_at"], completed_at=plan["generated_at"])
    write_json(case_dir / invocation_path(selected["case_id"], 1), dict(schema_version=1, case_id=selected["case_id"], attempt=1,
        source_sha=plan["source_sha"], plan_sha256=sha256(raw),
        collection={**invocation, "evidence_path": collection_path, "evidence_sha256": sha256((case_dir / collection_path).read_bytes())},
        contract={**invocation, "evidence_path": "junit/attempt1.xml", "evidence_sha256": sha256((case_dir / "junit/attempt1.xml").read_bytes())}))
    result = {"schema_version": 1, "run": run_header(plan, raw), "cases": [row]}
    write_json(case_dir / "results.json", result)
    return plan_dir, artifacts, output, case_dir, result


def semantic_check(row, target, root):
    # Synthetic trusted two-node suite for collector units. Workflow cases use
    # the real PR1 interface from the immutable source checkout, not this mock.
    if row["contract"]["suite_sha256"] != "f" * 64:
        raise ValueError("trusted suite digest mismatch")
    if row["contract"]["command"] != contract_command(target["contract_test"], row["contract"]["junit_paths"][0]):
        raise ValueError("registered contract command mismatch")
    if {t["test_id"] for t in row["contract"]["tests"]} != set(IDS):
        raise ValueError("registered contract test nodes mismatch")
    if row["contract"]["scenarios"] != target["contract_test"]["scenarios"]:
        raise ValueError("registered scenarios mismatch")


def reconcile(data, plan, raw):
    for row in data['cases']:
        selected = next(c for c in plan['cases'] if c['case_id'] == row['case_id'])
        expected = [{**d, 'resolved_version': next((r['version'] for r in row['resolved'] if r['name'] == d['name']), None)} for d in selected['discovery']]
        if row['discovery'] != expected:
            raise ValueError('trusted discovery mismatch')


def invocation_tests(paths, exits):
    tests = [t for path in paths for t in junit(path)[0]]
    if exits not in ([0], [1]) or (exits == [0]) != all(t['outcome'] == 'passed' for t in tests):
        raise ValueError('runner/JUnit outcome mismatch')
    return tests


def judge(tests, target, runtime, resolved):
    if any(t['outcome'] != 'passed' or t.get('exception_id') for t in tests):
        raise ValueError('unmatched failure/false exception attribution')
    return 'passed'


def trusted_validator():
    return SimpleNamespace(validate_contract=semantic_check, validate_registry=lambda data, root: data,
        reconcile_plan=reconcile, judge_tests=judge, invocation_tests=invocation_tests,
        validate_results=lambda *args, **kwargs: None, junit_tests=lambda path: junit(path)[0])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.mock = patch("collect_results.semantic_validator", return_value=trusted_validator())
        self.mock.start(); self.addCleanup(self.mock.stop)
        self.env = patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1"})
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop("GITHUB_RUN_ID", None)

    def collect(self, plan, artifacts, output, **kwargs):
        return collect(plan, artifacts, output, SCHEMA, source_root=plan / 'source', **kwargs)

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
        self.assertIn("Invocation evidence digest mismatch", result["cases"][0]["failure"]["summary"])

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
        validator = trusted_validator()
        validator.validate_contract = lambda *args: None
        with patch('collect_results.semantic_validator', return_value=validator):
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

    def test_complete_registry_bytes_are_bound_before_contract_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, _, _ = evidence(Path(temporary))
            registry = json.loads((plan / 'registry.json').read_text())
            target = next(t for t in registry['integrations'] if t['id'] == 'python-openai')
            target['contract_test']['paths'] = target['contract_test']['paths'][:1]
            write_json(plan / 'registry.json', registry)
            frozen = json.loads((plan / 'compat-plan.json').read_text())
            frozen['registry_sha256'] = sha256((plan / 'registry.json').read_bytes())
            write_json(plan / 'compat-plan.json', frozen)
            with self.assertRaisesRegex(ValueError, 'immutable source registry'):
                self.collect(plan, artifacts, output)

    def test_discovery_reconciliation_cannot_be_replaced_by_direct_pin_equality(self):
        for mutation in (dict(yanked=True), dict(latest_eligible='99.0.0'), dict(resolved_version='0.0.0'),
                         dict(published_at='2026-10-28T00:00:00Z')):
            with self.subTest(mutation=mutation):
                result = self.run_control(lambda d, f: d['cases'][0]['discovery'][0].update(mutation))
                self.assertIn('trusted discovery mismatch', result['cases'][0]['failure']['summary'])

    def test_unregistered_quarantine_is_not_accepted(self):
        def mutate(data, folder):
            data['cases'][0]['status'] = 'quarantined'
            data['cases'][0]['contract']['tests'][0]['exception_id'] = 'invented'
        result = self.run_control(mutate)
        self.assertIn('false exception attribution', result['cases'][0]['failure']['summary'])

    def test_missing_or_nonzero_invocation_exit_evidence_blocks_passing_junit(self):
        def change_exit(data, folder):
            relative = invocation_path(data['cases'][0]['case_id'], 1)
            status = json.loads((folder / relative).read_text())
            status['contract']['exit_code'] = 1
            write_json(folder / relative, status)
            (folder / 'junit/attempt1.xml.exit-code').write_text('1\n')
        for mutate in (lambda d, f: (f / 'junit/attempt1.xml.exit-code').unlink(), change_exit):
            result = self.run_control(mutate)
            self.assertEqual(result['cases'][0]['status'], 'blocked')

    def test_root_junit_and_invocation_statuses_are_copied_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            result, bad = self.collect(plan, artifacts, output)
            self.assertFalse(bad)
            for relative in ('junit/attempt1.xml', 'junit/attempt1.xml.exit-code',
                             invocation_path(data['cases'][0]['case_id'], 1)):
                self.assertEqual((folder / relative).read_bytes(), (output / relative).read_bytes())

    def test_malformed_attempts_are_independent_and_every_planned_row_is_emitted(self):
        for malformed in (1, 2):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary:
                plan, artifacts, output, first, data = evidence(Path(temporary))
                second = artifacts / first.name.replace('attempt-1', 'attempt-2')
                shutil.copytree(first, second)
                retry = copy.deepcopy(data)
                retry['run']['run_attempt'] = 2
                retry['cases'][0]['attempt'] = 2
                write_json(second / 'results.json', retry)
                old = second / invocation_path(data['cases'][0]['case_id'], 1)
                status = json.loads(old.read_text()); old.unlink()
                status['attempt'] = 2
                write_json(second / invocation_path(data['cases'][0]['case_id'], 2), status)
                (first if malformed == 1 else second).joinpath('results.json').write_text('{broken')
                with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                    result, bad = self.collect(plan, artifacts, output)
                self.assertTrue(bad)
                self.assertTrue((output / 'results.json').is_file())
                self.assertEqual([r['attempt'] for r in result['cases']], [1, 2])
                self.assertEqual([r['status'] for r in result['cases']],
                                 ['blocked', 'flaky'] if malformed == 1 else ['passed', 'blocked'])
                healthy = result['cases'][1 if malformed == 1 else 0]
                self.assertEqual(healthy['contract']['junit_paths'], ['junit/attempt1.xml'])
                self.assertEqual((output / 'junit/attempt1.xml').read_bytes(),
                                 (second if malformed == 1 else first).joinpath('junit/attempt1.xml').read_bytes())

    def test_malformed_invocation_shapes_block_only_the_affected_attempt(self):
        mutations = {
            'manifest-list': lambda s: [],
            'manifest-null': lambda s: None,
            'collection-list': lambda s: {**s, 'collection': []},
            'contract-list': lambda s: {**s, 'contract': []},
            'path-list': lambda s: {**s, 'collection': {**s['collection'], 'evidence_path': []}},
        }
        for malformed in (1, 2):
            for name, mutate in mutations.items():
                with self.subTest(attempt=malformed, shape=name), tempfile.TemporaryDirectory() as temporary:
                    plan, artifacts, output, first, data = evidence(Path(temporary))
                    cid = data['cases'][0]['case_id']
                    second = artifacts / first.name.replace('attempt-1', 'attempt-2')
                    shutil.copytree(first, second)
                    retry = copy.deepcopy(data)
                    retry['run']['run_attempt'] = retry['cases'][0]['attempt'] = 2
                    write_json(second / 'results.json', retry)
                    status = json.loads((first / invocation_path(cid, 1)).read_text())
                    write_json(second / invocation_path(cid, 2), {**status, 'attempt': 2})
                    folder = first if malformed == 1 else second
                    relative = invocation_path(cid, malformed)
                    original = json.dumps(mutate(json.loads((folder / relative).read_text())))
                    (folder / relative).write_text(original)
                    before = (first / 'results.json').read_bytes()
                    with patch.dict(os.environ, {'GITHUB_RUN_ATTEMPT': '2'}):
                        result, bad = self.collect(plan, artifacts, output)
                    self.assertTrue(bad)
                    self.assertEqual([r['status'] for r in result['cases']],
                                     ['blocked', 'flaky'] if malformed == 1 else ['passed', 'blocked'])
                    invalid = result['cases'][malformed - 1]
                    self.assertEqual(invalid['resolved'], [])
                    self.assertIsNone(invalid['dependency_lock'])
                    self.assertEqual(invalid['contract']['junit_paths'], [])
                    self.assertEqual((first / 'results.json').read_bytes(), before)
                    self.assertEqual((output / f'evidence/{cid}/attempt-{malformed}' / relative).read_text(), original)
                    self.assertTrue((output / 'results.json').is_file())

    def test_malformed_collection_evidence_is_a_per_attempt_failure(self):
        for malformed in ([], None, {'nodes': [], 'deselected': [], 'errors': [None]}):
            with self.subTest(shape=malformed):
                def mutate(data, folder):
                    relative = invocation_path(data['cases'][0]['case_id'], 1)
                    status = json.loads((folder / relative).read_text())
                    path = folder / status['collection']['evidence_path']
                    write_json(path, malformed)
                    status['collection']['evidence_sha256'] = sha256(path.read_bytes())
                    write_json(folder / relative, status)
                self.run_control(mutate)

    def test_complete_freeze_must_include_explicit_coupled_companion_pins(self):
        from collect_results import frozen_environment
        with tempfile.TemporaryDirectory() as temporary:
            plan, artifacts, output, folder, data = evidence(Path(temporary))
            selected = json.loads((plan / 'compat-plan.json').read_text())['cases'][0]
            selected['coupled_companions'] = [{'name': 'langgraph-prebuilt', 'version': '1.0.5'}]
            with self.assertRaisesRegex(ValueError, 'coupled companion'):
                frozen_environment(plan, folder, selected, data['cases'][0])
