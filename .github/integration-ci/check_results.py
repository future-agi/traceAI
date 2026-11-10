#!/usr/bin/env python3
"""Fail if a planned matrix was skipped, failed or cancelled (no empty green gate)."""
import json
import os


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
    check(json.loads(os.environ['NEEDS_JSON']))
    print('Every planned integration built and passed its tests.')
