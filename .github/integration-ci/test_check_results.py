import copy
import unittest
from check_results import check, check_execution
import json
from pathlib import Path
import tempfile


class ExecutionTests(unittest.TestCase):
    def test_native_execution_requires_nonzero_pass_and_no_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'results.json'
            for status in ['passed', 'pending', 'failed', 'todo']:
                path.write_text(json.dumps({'success': status == 'passed', 'numTotalTests': 1,
                                          'testResults': [{'assertionResults': [{'status': status}]}]}))
                if status == 'passed': self.assertEqual(check_execution('typescript', path), 1)
                else:
                    with self.assertRaises(ValueError): check_execution('typescript', path)
            path.write_text(json.dumps({'success': True, 'numTotalTests': 0, 'testResults': []}))
            with self.assertRaises(ValueError): check_execution('typescript', path)
            for action in ['pass', 'skip', 'fail']:
                path.write_text(json.dumps({'Action': action, 'Test': 'TestSDK', 'Package': 'example'}) + '\n')
                if action == 'pass': self.assertEqual(check_execution('go', path), 1)
                else:
                    with self.assertRaises(ValueError): check_execution('go', path)
            path.write_text(json.dumps({'Action': 'pass', 'Package': 'no-tests'}) + '\n')
            with self.assertRaises(ValueError): check_execution('go', path)
            reports = root / 'surefire-reports'; reports.mkdir()
            report = reports / 'TEST-sdk.xml'
            report.write_text('<testsuite><testcase name="executes"/></testsuite>')
            self.assertEqual(check_execution('java', root), 1)
            for xml in ['<testsuite/>', '<testsuite><testcase name="skip"><skipped/></testcase></testsuite>']:
                report.write_text(xml)
                with self.assertRaises(ValueError): check_execution('java', root)


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.needs = {'plan': {'result': 'success', 'outputs': {
            'typescript': '[]', 'go': '[{"name":"openai"}]', 'java': '[]'}},
            'typescript': {'result': 'skipped'}, 'go': {'result': 'success'},
            'java': {'result': 'skipped'}}

    def test_success(self):
        check(self.needs)

    def test_failed_or_skipped_planner_fails(self):
        for result in ['failure', 'cancelled', 'skipped']:
            needs = copy.deepcopy(self.needs)
            needs['plan']['result'] = result
            with self.assertRaises(ValueError):
                check(needs)

    def test_planned_matrix_must_succeed(self):
        for result in ['failure', 'cancelled', 'skipped']:
            needs = copy.deepcopy(self.needs)
            needs['go']['result'] = result
            with self.assertRaises(ValueError):
                check(needs)

    def test_unplanned_matrix_must_not_claim_success(self):
        self.needs['java']['result'] = 'success'
        with self.assertRaises(ValueError):
            check(self.needs)

    def test_missing_or_invalid_matrix_fails(self):
        for value in ['', 'null', '{}', 'false']:
            self.needs['plan']['outputs']['go'] = value
            with self.assertRaises(ValueError):
                check(self.needs)
        del self.needs['plan']['outputs']['go']
        with self.assertRaises(KeyError):
            check(self.needs)
