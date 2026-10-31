#!/usr/bin/env python3
"""Plan the Codex/Pi, Go and Java integration checks, without changing Python CI.

Uses the existing Python planner's NUL-safe, rename-aware git diff reader. The
same files can land independently with each integration: absent target directories
are ignored, but an existing target may not silently lose its manifest or tests.
CI changes run every present target; TS shared dependencies run both TS targets.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
from python_test_plan import PlanError, changed_paths, commit_count, load_registry

MANIFESTS = {'typescript': 'package.json', 'go': 'go.mod', 'java': 'pom.xml'}


def legacy_targets(registry):
    """Preserve existing executable checks; the inventory lives only in registry.json."""
    return tuple((t['distribution'].removeprefix('@traceai/') if t['language'] == 'typescript'
                  else Path(t['package_dir']).name.removesuffix('-example'),
                  t['language'], t['package_dir'], MANIFESTS[t['language']])
                 for t in registry['integrations']
                 if t['language'] in MANIFESTS and t.get('regression_ci', False))


# Backward-compatible fixture interface, generated from the same inventory, not a second list.
TARGETS = legacy_targets(json.loads((ROOT / '.github/compat/registry.json').read_text()))
WORKFLOW = '.github/workflows/integration-tests.yml'
CI_PREFIX = '.github/integration-ci/'
SHARED_DIFF_READER = '.github/scripts/python_test_plan.py'
TS_SHARED_FILES = {
    'typescript/package.json', 'typescript/pnpm-lock.yaml',
    'typescript/pnpm-workspace.yaml', 'typescript/.npmrc',
}
TS_SHARED_PREFIXES = ('typescript/packages/fi-core/', 'typescript/packages/fi-semantic-conventions/')
TEST_GLOBS = {'typescript': 'src/__tests__/**/*.test.ts', 'go': '**/*_test.go', 'java': 'src/test/java/**/*Test.java'}


def plan(changed, root=ROOT, all_targets=False):
    changed = set(changed)
    registry = load_registry(root)
    by_dir = {t['package_dir']: t for t in registry['integrations']}
    run_all = all_targets or WORKFLOW in changed or SHARED_DIFF_READER in changed or any(p.startswith((CI_PREFIX, '.github/compat/')) for p in changed)
    ts_shared = any(p in TS_SHARED_FILES or p.startswith(TS_SHARED_PREFIXES) or
                    (p.startswith('typescript/tsconfig') and p.endswith('.json')) for p in changed)
    result = {'typescript': [], 'go': [], 'java': []}
    for name, language, directory, manifest in legacy_targets(registry):
        path = root / directory
        if not path.is_dir():
            # A not-yet-landed or entirely deleted integration is not a target.
            continue
        if not (path / manifest).is_file():
            raise PlanError(f'{directory} exists but is missing {manifest}')
        if not any(path.glob(TEST_GLOBS[language])):
            raise PlanError(f'{directory} has no tests; refusing an empty test gate')
        if run_all or (language == 'typescript' and ts_shared) or any(p.startswith(directory + '/') for p in changed):
            target = by_dir[directory]
            result[language].append({'name': name, 'dir': directory, 'integration_id': target['id'],
                                     'qualification': target['qualification']['status'],
                                     'exception_ids': [ex['id'] for ex in target['exceptions']]})
    result['coverage_gaps'] = [
        {'integration_id': t['id'], 'dir': t['package_dir'], 'qualification': t['qualification']['status'],
         'execution': 'not_tested'}
        for t in registry['integrations'] if t['language'] != 'python' and not t.get('regression_ci', False)
        and (run_all or (ts_shared and t['language'] == 'typescript') or
             any(p.startswith(t['package_dir'] + '/') for p in changed))
    ]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = parser.add_mutually_exclusive_group()
    args.add_argument('--diff')
    args.add_argument('--all', action='store_true')
    options = parser.parse_args(argv)
    try:
        if options.diff:
            paths = changed_paths(options.diff, ROOT)
            if not paths and commit_count(options.diff, ROOT) == 0:
                raise PlanError('no commits in diff; check base/head wiring')
        elif options.all:
            paths = changed_paths(None, ROOT)
            if not paths:
                raise PlanError('no tracked files; check checkout')
        else:
            paths = [line.strip() for line in sys.stdin if line.strip()]
        print(json.dumps(plan(paths, all_targets=options.all), sort_keys=True))
    except PlanError as exc:
        print(f'integration_plan: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
