#!/usr/bin/env python3
"""Plan selected TypeScript, Go and Java integration checks, without changing Python CI.

Uses the existing Python planner's NUL-safe, rename-aware git diff reader. The
same files can land independently with each integration: absent target directories
are ignored, but an existing target may not silently lose its manifest or tests.
CI changes run every present target; TS shared dependencies run all TS targets.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.github/scripts'))
from python_test_plan import PlanError, changed_paths, commit_count

TARGETS = (
    ('codex-sdk', 'typescript', 'typescript/packages/traceai_codex_sdk', 'package.json'),
    ('pi-coding-agent', 'typescript', 'typescript/packages/traceai_pi_coding_agent', 'package.json'),
    ('instructor', 'typescript', 'typescript/packages/traceai_instructor', 'package.json'),
    ('openai', 'go', 'go/openai', 'go.mod'),
    ('a2a', 'go', 'go/a2a', 'go.mod'),
    ('eino', 'go', 'go/eino', 'go.mod'),
    ('anthropic', 'go', 'go/anthropic', 'go.mod'),
    ('genai', 'go', 'go/genai', 'go.mod'),
    ('arconia', 'java', 'java/examples/arconia-example', 'pom.xml'),
    ('quarkus-langchain4j', 'java', 'java/examples/quarkus-langchain4j-example', 'pom.xml'),
)
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
    run_all = all_targets or WORKFLOW in changed or SHARED_DIFF_READER in changed or any(p.startswith(CI_PREFIX) for p in changed)
    ts_shared = any(p in TS_SHARED_FILES or p.startswith(TS_SHARED_PREFIXES) or
                    (p.startswith('typescript/tsconfig') and p.endswith('.json')) for p in changed)
    result = {'typescript': [], 'go': [], 'java': []}
    for name, language, directory, manifest in TARGETS:
        path = root / directory
        if not path.is_dir():
            # A not-yet-landed or entirely deleted integration is not a target.
            continue
        if not (path / manifest).is_file():
            raise PlanError(f'{directory} exists but is missing {manifest}')
        if not any(path.glob(TEST_GLOBS[language])):
            raise PlanError(f'{directory} has no tests; refusing an empty test gate')
        if run_all or (language == 'typescript' and ts_shared) or any(p.startswith(directory + '/') for p in changed):
            result[language].append({'name': name, 'dir': directory})
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
