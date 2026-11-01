import copy
import unittest
from check_results import check


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
