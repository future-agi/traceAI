#!/usr/bin/env python3
"""Fail if a planned matrix was skipped, failed or cancelled (no empty green gate)."""
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def check_execution(language, path):
    """A successful process/file on disk is not proof that a test actually ran."""
    path = Path(path)
    if language == 'typescript':
        data = json.loads(path.read_text())
        tests = [test for suite in data.get('testResults', []) for test in suite.get('assertionResults', [])]
        if not tests or any(t.get('status') != 'passed' for t in tests):
            raise ValueError('TypeScript: empty, skipped, pending or failed tests')
        if data.get('numTotalTests') != len(tests) or not data.get('success'):
            raise ValueError('TypeScript: inconsistent execution counts')
    elif language == 'go':
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        tests = [e for e in events if e.get('Test') and e.get('Action') in ['pass', 'skip', 'fail']]
        if not tests or any(e['Action'] != 'pass' for e in tests):
            raise ValueError('Go: empty, skipped or failed tests')
    elif language == 'java':
        tests = [t for report in sorted(path.glob('*-reports/TEST-*.xml')) for t in ET.parse(report).iter('testcase')]
        if not tests or any(t.find('skipped') is not None or t.find('failure') is not None or t.find('error') is not None for t in tests):
            raise ValueError('Java: empty, skipped or failed tests')
    else:
        raise ValueError(f'unknown execution language: {language}')
    return len(tests)


def check(needs):
    if needs['plan']['result'] != 'success':
        raise ValueError('integration planning did not succeed')
    for language in ['typescript', 'go', 'java']:
        matrix = json.loads(needs['plan']['outputs'][language])
        if not isinstance(matrix, list):
            raise ValueError(f'{language}: missing/invalid matrix')
        expected = 'success' if matrix else 'skipped'
        actual = needs[language]['result']
        if actual != expected:
            raise ValueError(f'{language}: planned {len(matrix)} packages, got {actual}, expected {expected}')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'execution':
        print(f'Executed {check_execution(sys.argv[2], sys.argv[3])} tests (not portfolio qualification).')
    else:
        check(json.loads(os.environ['NEEDS_JSON']))
        print('Every planned integration built and passed its tests; unplanned targets remain unqualified.')
