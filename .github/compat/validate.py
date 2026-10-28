#!/usr/bin/env python3
"""Validate declarations and evidence; judge exact quarantines, never package waivers.

Requires Python >=3.11, jsonschema==4.23.0 and packaging==24.2. Importing this
module never reads files or starts a subprocess. Artifact commands are data only.
"""
import argparse
import ast
from datetime import date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
import xml.etree.ElementTree as ET

from jsonschema import Draft202012Validator, FormatChecker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(root, path):
    p = PurePosixPath(path)
    require(not p.is_absolute() and '..' not in p.parts and '\\' not in path,
            f'unsafe path: {path}')
    base = Path(root).resolve()
    result = base / path
    require(result.resolve().is_relative_to(base), f'escaping path: {path}')
    require(not any((base.joinpath(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)), f'symlink: {path}')
    return result


def shape(data, name):
    schema = json.loads((HERE / f'{name}.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data),
                    key=lambda e: str(list(e.path)))
    if errors:
        raise ValueError(f'{name}: {list(errors[0].path)}: {errors[0].message}')


def dotted(data, key):
    # Dependency names may themselves contain dots; prefer an exact remaining key.
    if key in data:
        return data[key]
    head, sep, tail = key.partition('.')
    return dotted(data[head], tail) if sep and head in data else None


def runtime_requirements(path, variable):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
            return [Requirement(s) for s in ast.literal_eval(node.value)]
    raise ValueError(f'{path}: missing literal {variable}')


def observed(source, root, name=None):
    """Read declarations as data, without importing instrumentation or executing tools."""
    path = safe_path(root, source['path'])
    require(path.is_file(), f'missing declaration: {source["path"]}')
    key = source['key']
    if source['kind'] == 'inferred':
        return None  # A documented gap, not an unrestricted requirement.
    if path.suffix == '.py':
        requirements = runtime_requirements(path, key)
        matches = [r for r in requirements if canonicalize_name(r.name) == canonicalize_name(name)]
        require(len(matches) == 1, f'{path}: missing/ambiguous runtime requirement {name}')
        return str(matches[0].specifier) or None
    if path.suffix == '.toml':
        value = dotted(tomllib.loads(path.read_text()), key)
        if isinstance(value, dict):
            return value.get('version')
        if isinstance(value, list) and name:
            reqs = [Requirement(v) for v in value]
            found = [r for r in reqs if canonicalize_name(r.name) == canonicalize_name(name)]
            require(len(found) == 1, f'{path}:{key}: missing/ambiguous {name}')
            return str(found[0].specifier) or None
        return value
    if path.suffix == '.json':
        return dotted(json.loads(path.read_text()), key)
    if path.name == 'go.mod':
        text = path.read_text()
        if key == 'go':
            match = re.search(r'^go\s+(\S+)', text, re.M)
            return '>=' + match[1] if match else None
        module = key.removeprefix('require ')
        match = re.search(r'^\s*(?:require\s+)?' + re.escape(module) + r'\s+(v\S+)', text, re.M)
        return match[1] if match else None
    tree = ET.fromstring(path.read_text())
    for element in tree.iter():
        element.tag = element.tag.split('}')[-1]
    if key.startswith('dependencies/'):
        _, dependency, _ = key.split('/')
        for dep in tree.findall('./dependencies/dependency'):
            if ':'.join([dep.findtext('groupId', ''), dep.findtext('artifactId', '')]) == dependency:
                return dep.findtext('version')
        return None
    if key.startswith('PackageReference/'):
        for dep in tree.iter('PackageReference'):
            if dep.get('Include') == key.split('/', 1)[1]:
                return dep.get('Version')
        return None
    value = tree.findtext('.//' + key)
    return '>=' + value if key == 'properties/maven.compiler.target' and value else value


def equal_range(left, right):
    if left == right:
        return True
    if left is None or right is None:
        return False
    # Formatting-only normalization, not npm/Poetry/Go reinterpretation.
    return sorted(re.sub(r'\s+', '', left).split(',')) == sorted(re.sub(r'\s+', '', right).split(','))


def inventory(root):
    root = Path(root)
    paths = {p.parent.relative_to(root).as_posix() for pattern in [
        'python/frameworks/*/pyproject.toml', 'python/traceai/pyproject.toml',
        'python/pyproject.toml', 'typescript/packages/*/package.json',
        'go/*/go.mod', 'java/traceai-*/pom.xml',
        'java/examples/arconia-example/pom.xml', 'java/examples/quarkus-langchain4j-example/pom.xml',
        'csharp/src/*/*.csproj',
    ] for p in root.glob(pattern)}
    return paths


def validate_exception(ex, target, today=None):
    today = today or date.today()
    added, expires = date.fromisoformat(ex['added']), date.fromisoformat(ex['expires'])
    require(added <= today < expires and 0 < (expires - added).days <= 14,
            f'{ex["id"]}: expired, future or over-14-day exception')
    upstream = {canonicalize_name(u['name']) for u in target['upstream']}
    for constraint in ex['upstream_constraints']:
        require(canonicalize_name(constraint['name']) in upstream, f'{ex["id"]}: unknown upstream')
        require(bool(list(SpecifierSet(constraint['specifier']))), 'unbounded exception')
    require(bool(list(SpecifierSet(ex['runtime_constraints']['specifier']))), 'unbounded runtime')
    require(ex['runtime_constraints']['implementation'] == target['runtimes']['implementation'],
            'exception runtime implementation mismatch')
    for node in ex['test_ids']:
        file, sep, test = node.partition('::')
        require(sep and test and not any(c in node for c in '*?') and file.endswith('.py'),
                f'not an exact test ID: {node}')
        require(not PurePosixPath(file).is_absolute() and '..' not in PurePosixPath(file).parts,
                f'unsafe test ID: {node}')
        require(file.startswith(target['package_dir'] + '/') or
                file.startswith('python/tests/test_framework_') or
                file.startswith('.github/compat/contracts/'), f'exception outside target: {node}')


def validate_registry(data, root=None):
    shape(data, 'registry')
    ids, dirs, exceptions = set(), set(), set()
    for target in data['integrations']:
        require(target['id'] not in ids, f'duplicate integration ID: {target["id"]}')
        require(target['package_dir'] not in dirs, f'duplicate package directory: {target["package_dir"]}')
        ids.add(target['id']); dirs.add(target['package_dir'])
        require(target['id'].startswith(target['language'] + '-'), 'ID/language mismatch')
        require(len({u['name'] for u in target['upstream']}) == len(target['upstream']), 'duplicate upstream')
        if target.get('regression_ci', False):
            require(target['contract_test'] is not None, 'regression CI target needs a contract')
        if target['qualification']['status'] == 'qualified':
            require(target['contract_test'] and target['qualification']['evidence'],
                    f'{target["id"]}: qualification requires execution evidence and a contract')
            require(not target['exceptions'], 'quarantined target cannot be qualified')
        for ex in target['exceptions']:
            require(ex['id'] not in exceptions, f'duplicate exception ID: {ex["id"]}')
            exceptions.add(ex['id'])
            validate_exception(ex, target)
        if root is not None:
            # Verify identity, declared dependencies, runtime guards and interpreter range.
            manifest = safe_path(root, target['declared_range_source']['path'])
            if target['language'] == 'python':
                meta = tomllib.loads(manifest.read_text())
                distribution = meta.get('project', {}).get('name') or meta.get('tool', {}).get('poetry', {}).get('name')
                require(distribution == target['distribution'], f'{target["id"]}: distribution drift')
            elif target['language'] == 'typescript':
                require(json.loads(manifest.read_text())['name'] == target['distribution'], 'distribution drift')
            elif target['language'] == 'go':
                require(re.search(r'^module\s+' + re.escape(target['distribution']) + r'\s*$', manifest.read_text(), re.M),
                        f'{target["id"]}: module drift')
            runtime = target['runtimes']
            require(equal_range(observed(runtime['source'], root), runtime['declared']),
                    f'{target["id"]}: runtime metadata drift')
            if target['language'] == 'python' and runtime['declared']:
                for version in runtime['representative'] + runtime['boundaries']:
                    require(SpecifierSet(runtime['declared']).contains(version),
                            f'{target["id"]}: runtime {version} outside declaration')
            for upstream in target['upstream']:
                for source_key, value_key in [('range_source', 'declared_range'), ('runtime_range_source', 'runtime_range')]:
                    if upstream[source_key]:
                        value = observed(upstream[source_key], root, upstream['name'])
                        require(equal_range(value, upstream[value_key]),
                                f'{target["id"]}/{upstream["name"]}: {value_key} drift: {value!r} != {upstream[value_key]!r}')
                if target['qualification']['status'] == 'qualified' and upstream['runtime_range_source']:
                    require(equal_range(upstream['declared_range'], upstream['runtime_range']),
                            f'{target["id"]}: qualification cannot conceal declaration disagreement')
    require(set(data['defaults']['pilot_ids']) <= ids, 'missing pilot entry')
    if root is not None:
        actual = inventory(root)
        require(actual == dirs, f'inventory drift: missing={sorted(actual - dirs)}, absent={sorted(dirs - actual)}')
    return data


def load_registry(root):
    return validate_registry(json.loads((Path(root) / '.github/compat/registry.json').read_text()), root)


def applicable(ex, runtime, resolved):
    versions = {canonicalize_name(r['name']): r['version'] for r in resolved}
    rc = ex['runtime_constraints']
    return (runtime['implementation'] == rc['implementation'] and
            SpecifierSet(rc['specifier']).contains(runtime['version']) and
            all(canonicalize_name(c['name']) in versions and
                SpecifierSet(c['specifier']).contains(versions[canonicalize_name(c['name'])])
                for c in ex['upstream_constraints']))


def judge_tests(tests, target, runtime, resolved):
    """Return passed/quarantined, or raise. Never waive runner/collection failures."""
    require(tests, 'empty collection: no executed tests')
    require(len({t['test_id'] for t in tests}) == len(tests), 'duplicate test IDs')
    matches = [ex for ex in target['exceptions'] if applicable(ex, runtime, resolved)]
    for ex in target['exceptions']:
        validate_exception(ex, target)
    seen = set()
    degraded = False
    for test in tests:
        require(test['outcome'] != 'skipped', f'unexpected skip: {test["test_id"]}')
        scoped = [ex for ex in matches if test['test_id'] in ex['test_ids']]
        require(len(scoped) <= 1, f'overlapping exceptions: {test["test_id"]}')
        if scoped:
            ex = scoped[0]
            require(test['outcome'] == ex['expected_outcome'], f'unexpected pass/outcome: {test["test_id"]}; remove {ex["id"]}')
            require(test['failure_signature'] == ex['failure_signature'], f'unmatched signature: {test["test_id"]}')
            seen.add(test['test_id'])
            degraded = True
        else:
            require(test['outcome'] == 'passed', f'unmatched failure: {test["test_id"]}')
        if test.get('exception_id') is not None:
            require(scoped and test['exception_id'] == scoped[0]['id'], 'false exception attribution')
    require(all(node in seen for ex in matches for node in ex['test_ids']), 'excepted test did not execute')
    return 'quarantined' if degraded else 'passed'


def validate_results(data, registry=None, expected_source_sha=None, expected_case_ids=None):
    shape(data, 'results')
    run = data['run']
    require(expected_source_sha is None or run['source_sha'] == expected_source_sha, 'source identity mismatch')
    planned = set(run['planned_case_ids'])
    require(expected_case_ids is None or planned == set(expected_case_ids), 'plan identity mismatch')
    targets = {t['id']: t for t in registry['integrations']} if registry else {}
    seen = set()
    groups = {}
    for case in data['cases']:
        key = case['case_id'], case['attempt']
        require(key not in seen and case['case_id'] in planned, 'duplicate/unplanned result')
        seen.add(key)
        groups.setdefault(case['case_id'], []).append(case)
        require(case['source_sha'] == run['source_sha'], 'case source mismatch')
        contract = case['contract']; tests = contract['tests']
        require(len({t['test_id'] for t in tests}) == len(tests), 'duplicate test IDs')
        require(contract['collected'] == len(tests), 'collected count mismatch')
        require(contract['executed'] == sum(t['outcome'] != 'skipped' for t in tests), 'executed count mismatch')
        require(contract['unexpected_skips'] == sum(t['outcome'] == 'skipped' for t in tests), 'skip count mismatch')
        for test in tests:
            require((test['failure_signature'] is not None) == (test['outcome'] in ['failure', 'error']), 'failure signature/outcome mismatch')
        for path in contract['junit_paths'] + ([case['dependency_lock']['path']] if case['dependency_lock'] else []):
            safe_path(Path('/__compat_artifact__'), path)  # lexically safe; no artifact execution
        target = targets.get(case['integration_id'])
        if registry:
            require(target is not None, 'unregistered integration')
            require(case['language'] == target['language'] and case['qualification'] == target['qualification']['status'],
                    'qualification/language mismatch')
        successful = case['status'] in ['passed', 'quarantined']
        resolved = {(canonicalize_name(r['name']) if case['language'] == 'python' else r['name'], r['role']): r['version'] for r in case['resolved']}
        require(len(resolved) == len(case['resolved']), 'duplicate resolved distribution')
        if successful:
            require(case['dependency_lock'] is not None, 'pass without dependency lock')
            require(contract['junit_paths'] and contract['executed'] >= (target['contract_test']['minimum_executed'] if target and target['contract_test'] else 1),
                    'pass without nonzero execution evidence')
            require(contract['unexpected_skips'] == 0, 'pass with unexpected skips')
            require({'instrumentation', 'core'} <= {r['role'] for r in case['resolved']}, 'missing instrumentation/core identity')
            requested_keys = [(canonicalize_name(r['name']) if case['language'] == 'python' else r['name'], r['role']) for r in case['requested']]
            require(len(set(requested_keys)) == len(requested_keys), 'duplicate requested distribution')
            require({'instrumentation', 'core'} <= {r['role'] for r in case['requested']}, 'missing requested instrumentation/core identity')
            if target and case['language'] == 'python':
                expected_names = {canonicalize_name(u['name']) for u in target['upstream']}
                expected_names.add(canonicalize_name(target['distribution']))
                expected_names.update(canonicalize_name(t['distribution']) for t in registry['integrations'] if t['language'] == 'python' and t['role'] == 'core')
                require(expected_names <= {name for name, _ in requested_keys}, 'missing requested upstream/core tuple')
            for req in case['requested']:
                name = canonicalize_name(req['name']) if case['language'] == 'python' else req['name']
                require(req['selected_version'] is not None and resolved.get((name, req['role'])) == req['selected_version'],
                        'requested/resolved mismatch')
            if target:
                require(target['contract_test'] is not None, 'pass without an execution contract')
                verdict = judge_tests(tests, target, case['runtime'], case['resolved'])
                require(verdict == case['status'], 'false pass/quarantine status')
            else:
                require(case['status'] != 'passed' or all(t['outcome'] == 'passed' for t in tests), 'pass with failures')
            require(case['status'] != 'passed' or case['failure'] is None, 'pass with a failure')
        for discovery in case['discovery']:
            found = [r for r in case['resolved'] if (canonicalize_name(r['name']) if case['language'] == 'python' else r['name']) ==
                     (canonicalize_name(discovery['name']) if case['language'] == 'python' else discovery['name'])]
            if successful:
                require(len(found) == 1 and discovery['resolved_version'] == found[0]['version'] == discovery['requested_version'], 'discovery/resolution mismatch')
                require(not discovery['yanked'], 'pass with yanked release')
                if case['selector'] == 'latest_eligible':
                    require(discovery['requested_version'] == discovery['latest_eligible'], 'false latest-eligible claim')
    require(set(groups) == planned, 'missing planned evidence')
    for attempts in groups.values():
        attempts.sort(key=lambda c: c['attempt'])
        require(attempts[0]['attempt'] == 1, 'missing first attempt')
        if len(attempts) > 1:
            first, second = attempts
            for field in ['source_sha', 'integration_id', 'mode', 'selector', 'runtime', 'requested', 'resolved', 'dependency_lock']:
                require(first[field] == second[field], f'retry changed immutable {field}')
            require(first['contract']['suite_sha256'] == second['contract']['suite_sha256'], 'retry changed suite')
            require(not (first['status'] in ['failed', 'blocked'] and second['status'] == 'passed'), 'fail-then-pass must be flaky')
    return data


def failure_signature(kind, test_ids, text):
    # First exception/assertion line; runner paths/addresses/timestamps are unstable.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    message = next((line for line in lines if re.search(r'(?:Error|Exception|assert\b|Assertion)', line)), lines[0] if lines else kind)
    message = re.sub(r'0x[0-9a-fA-F]+', '<address>', message)
    message = re.sub(r'(?:/[^\s:]+)+/', '<path>/', message)
    message = re.sub(r'\d{4}-\d\d-\d\d[T ][\d:.+Z-]+', '<time>', message)
    message = re.sub(r'(?i)(?:sk-[\w-]+|bearer\s+\S+|(?:api[_-]?key|token|password)\s*[=:]\s*\S+)', '<redacted>', message)
    return hashlib.sha256(json.dumps([kind, sorted(test_ids), message], separators=(',', ':')).encode()).hexdigest()


def junit_tests(path):
    require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024, f'missing/oversized JUnit: {path}')
    tree = ET.parse(path)
    tests = []
    for node in tree.iter('testcase'):
        file = node.get('file', '')
        require(file.endswith('.py') and not PurePosixPath(file).is_absolute() and '..' not in PurePosixPath(file).parts,
                'JUnit requires repository-relative file attributes (use --rootdir and junit_family=xunit1)')
        classname = node.get('classname', '')
        marker = PurePosixPath(file).stem
        suffix = classname.rsplit(marker, 1)[-1].strip('.') if marker in classname else ''
        test_id = file + '::' + (suffix.replace('.', '::') + '::' if suffix else '') + node.get('name', '')
        outcome, signature = 'passed', None
        for tag in ['failure', 'error', 'skipped']:
            child = node.find(tag)
            if child is not None:
                outcome = tag
                if tag != 'skipped':
                    signature = failure_signature(tag, [test_id], child.text or child.get('message', tag))
                break
        tests.append(dict(test_id=test_id, outcome=outcome, duration_seconds=float(node.get('time', '0')),
                          failure_signature=signature, exception_id=None))
    return tests


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['registry', 'results', 'verdict', 'fixture-gate']:
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, default=ROOT)
        if name == 'results':
            command.add_argument('path', type=Path)
        if name == 'verdict':
            command.add_argument('--integration-id', required=True)
            command.add_argument('--context', type=Path, required=True)
            command.add_argument('--junit', action='append', type=Path, default=[])
            command.add_argument('--exit-code', type=int, required=True)
        if name == 'fixture-gate':
            command.add_argument('kind', choices=['scoped', 'execution'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'fixture-gate':
            fixture = json.loads((args.root / '.github/compat/fixtures/validation.json').read_text())
            target = fixture['target']
            from datetime import timedelta
            for ex in target['exceptions']:
                ex['added'] = date.today().isoformat()
                ex['expires'] = (date.today() + timedelta(days=7)).isoformat()
            print(judge_tests(fixture[args.kind], target, fixture['runtime'], fixture['resolved']))
            return 0
        registry = load_registry(args.root)
        if args.command == 'results':
            data = json.loads(args.path.read_text())
            validate_results(data, registry)
            for case in data['cases']:
                if case['dependency_lock']:
                    lock = case['dependency_lock']
                    path = safe_path(args.path.parent, lock['path'])
                    require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == lock['sha256'], 'lock digest mismatch')
                for path in case['contract']['junit_paths']:
                    require(safe_path(args.path.parent, path).is_file(), 'missing JUnit evidence')
        elif args.command == 'verdict':
            require(args.exit_code in [0, 1], 'runner/collection error cannot be quarantined')
            target = next(t for t in registry['integrations'] if t['id'] == args.integration_id)
            context = json.loads(args.context.read_text())
            require(args.junit, 'no JUnit suites planned: integration not tested')
            tests = []
            for path in args.junit:
                suite = junit_tests(path)
                require(suite, f'empty collection: {path}')
                tests.extend(suite)
            verdict = judge_tests(tests, target, context['runtime'], context['resolved'])
            require((args.exit_code == 0) == all(t['outcome'] == 'passed' for t in tests), 'runner/JUnit outcome mismatch')
            print(f'{target["id"]}: {verdict}; executed={len(tests)}; qualification={target["qualification"]["status"]}')
        else:
            print(f'Validated {len(registry["integrations"])} inventory records (not a compatibility claim).')
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        print(f'compatibility: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
