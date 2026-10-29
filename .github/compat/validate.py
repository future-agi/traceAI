#!/usr/bin/env python3
"""Validate declarations and evidence; judge exact quarantines, never package waivers.

Requires Python >=3.11, jsonschema==4.23.0 and packaging==24.2. Importing this
module never reads files or starts a subprocess. Artifact commands are data only.
"""
import argparse
import ast
from datetime import date, datetime, timedelta
import math
import os
import subprocess
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
from packaging.version import Version

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


def xml_metadata(path):
    raw = Path(path).read_bytes()
    require(b'<!DOCTYPE' not in raw and b'<!ENTITY' not in raw, 'unsafe XML metadata')
    tree = ET.fromstring(raw)
    for element in tree.iter():
        element.tag = element.tag.split('}')[-1]
    return tree


def native_distribution(manifest, language):
    """Read package identity without Maven/MSBuild execution or dependency loading."""
    tree = xml_metadata(manifest)
    if language == 'java':
        group = tree.findtext('groupId') or tree.findtext('parent/groupId')
        artifact = tree.findtext('artifactId')
        properties = {e.tag: e.text for e in tree.findall('properties/*')}
        properties.update({'project.groupId': group, 'pom.groupId': group,
                           'project.artifactId': artifact, 'pom.artifactId': artifact,
                           'project.parent.groupId': tree.findtext('parent/groupId')})
        def expand(value, seen=()):
            require(value is not None, 'missing Maven identity')
            def replacement(match):
                key = match[1]
                require(key not in seen and key in properties, 'unresolved Maven identity property')
                return expand(properties[key], (*seen, key))
            return re.sub(r'\$\{([^}]+)\}', replacement, value)
        return expand(group) + ':' + expand(artifact)
    # NuGet defaults PackageId to AssemblyName, then the project basename.
    for key in ('PackageId', 'AssemblyName'):
        nodes = tree.findall('.//PropertyGroup/' + key)
        if nodes:
            require(len(nodes) == 1 and not nodes[0].get('Condition') and
                    all(not group.get('Condition') for group in tree.findall('PropertyGroup') if nodes[0] in list(group)),
                    'ambiguous conditional NuGet identity')
            value = nodes[0].text
            require(value and '$(' not in value, 'unresolved NuGet identity')
            return value
    return Path(manifest).stem


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
    # Signatures include the node ID. Sharing one signature across several nodes
    # is incoherent; independent parameter cases require independent exceptions.
    require(len(ex['test_ids']) == 1, 'exception requires exactly one test node/signature')
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
            elif target['language'] in ('java', 'csharp'):
                require(native_distribution(manifest, target['language']) == target['distribution'],
                        f'{target["id"]}: distribution drift')
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


def suite_files(root, contract):
    """Same complete consumer snapshot as PR2, including helpers and configuration."""
    root = Path(root).resolve()
    files = set()
    for relative in contract['paths']:
        path = safe_path(root, relative)
        require(path.is_file(), f'missing trusted suite: {relative}')
        for candidate in path.parent.rglob('*'):
            if candidate.is_file() and '__pycache__' not in candidate.parts and not candidate.name.endswith('.pyc'):
                rel = candidate.relative_to(root).as_posix()
                safe_path(root, rel)
                files.add(rel)
        for parent in [path.parent, *path.parent.parents]:
            if not parent.is_relative_to(root):
                break
            for name in ('conftest.py', 'pytest.ini', 'pyproject.toml', 'setup.cfg', '__init__.py'):
                candidate = parent / name
                if candidate.is_file():
                    files.add(candidate.relative_to(root).as_posix())
    return sorted(files)


def suite_digest(root, contract):
    digest = hashlib.sha256()
    for relative in suite_files(root, contract):
        digest.update(relative.encode() + b'\0' + safe_path(root, relative).read_bytes() + b'\0')
    return digest.hexdigest()


def expected_test_nodes(root, contract):
    """Fail closed for a partial suite: every trusted test function must execute.

    Parameter suffixes are reconciled to their owning function; exact per-parameter
    identities and outcomes are additionally reconciled against original JUnit.
    Dynamic/inherited test suites need an explicit collector adapter before promotion.
    """
    expected = set()
    def visit(nodes, prefix):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                expected.add(prefix + '::' + node.name)
            elif isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
                visit(node.body, prefix + '::' + node.name)
    for relative in contract['paths']:
        visit(ast.parse(safe_path(root, relative).read_text()).body, relative)
    require(expected, 'trusted suite has no statically registered test nodes')
    return expected


def validate_contract(case, target, root):
    contract, registered = case['contract'], target['contract_test']
    require(registered is not None, 'pass without an execution contract')
    require(case['language'] == 'python', 'native execution-evidence adapter not yet qualified')
    require(contract['suite_sha256'] == suite_digest(root, registered), 'trusted suite digest mismatch')
    # Scenarios are the owner's declarations about the complete, immutable suite,
    # not an artifact's arbitrary labels. Executing a subset cannot claim them.
    require(set(contract['scenarios']) == set(registered['scenarios']) and
            len(contract['scenarios']) == len(set(contract['scenarios'])), 'registered scenarios mismatch')
    expected = expected_test_nodes(root, registered)
    actual = {t['test_id'].split('[', 1)[0] for t in contract['tests'] if t['outcome'] != 'skipped'}
    require(actual == expected, f'registered contract test nodes mismatch: missing={sorted(expected-actual)}, unrelated={sorted(actual-expected)}')
    # Producer appends runner isolation/JUnit options, but must retain the trusted
    # argv in order (with an optional -I immediately after the Python executable).
    command = list(contract['command'])
    if command[:2] == ['python', '-I']:
        command.pop(1)
    require(command[:len(registered['command'])] == registered['command'], 'registered contract command mismatch')


def requested_version(req, language):
    value = req['requirement']
    version = req['selected_version']
    if language == 'python':
        if value.startswith(('=', '!', '<', '>', '~')):
            parsed = Requirement(req['name'] + value)
        else:
            parsed = Requirement(value)
        require(canonicalize_name(parsed.name) == canonicalize_name(req['name']) and
                parsed.url is None and parsed.marker is None and bool(str(parsed.specifier)) and
                parsed.specifier.contains(version, prereleases=True), 'requested requirement excludes selected version')
    else:
        # Native floating adapters are not enabled yet. Only native exact pins are
        # accepted; never reinterpret npm ranges, Go MVS or Maven as PEP 440.
        if language in ('typescript', 'java', 'csharp'):
            require(value == version or value == '=' + version, 'native requirement needs a verified exact pin')
        elif language == 'go':
            require(value in (version, req['name'] + '@' + version) and version.startswith('v'),
                    'Go requirement must be an exact module version, not a minimum')


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'timestamp requires timezone')
    return parsed


def installed_distributions(raw, format):
    """Parse only qualified lock adapters, never artifact commands or URLs."""
    require(len(raw) <= 16 * 1024 * 1024, 'oversized dependency lock')
    identities = {}
    def add(name, version):
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name), 'malformed lock distribution')
        require(isinstance(version, str) and version and str(Version(version)), 'malformed lock version')
        name = canonicalize_name(name)
        require(name not in identities, 'duplicate lock distribution')
        identities[name] = version
    if format == 'pip-freeze':
        for line in raw.decode('utf-8').splitlines():
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)', line)
            require(match is not None, 'malformed/noncanonical pip-freeze entry')
            add(match[1], match[2])
    elif format == 'pip-report':
        def unique_object(pairs):
            obj = {}
            for key, value in pairs:
                require(key not in obj, 'duplicate pip-report JSON field')
                obj[key] = value
            return obj
        report = json.loads(raw, object_pairs_hook=unique_object)
        require(report.get('version') == '1' and isinstance(report.get('install'), list), 'malformed pip-report')
        for item in report['install']:
            require(isinstance(item, dict) and isinstance(item.get('metadata'), dict), 'malformed pip-report metadata')
            add(item['metadata'].get('name'), item['metadata'].get('version'))
    else:
        raise ValueError('unqualified dependency lock adapter: ' + format)
    require(identities, 'empty dependency lock')
    return identities


def preserved_resolution(case, resolution, artifact_root, *, observed_mismatch=False):
    """Original report/requirements bytes are bound by the immutable plan."""
    contents = {}
    for key in ('report', 'requirements'):
        relative = resolution[key]
        path = safe_path(artifact_root, relative)
        if not path.is_file():
            # The collector retains complete per-attempt case uploads here.
            relative = f'evidence/{case["case_id"]}/attempt-{case["attempt"]}/locks/' + PurePosixPath(relative).name
            path = safe_path(artifact_root, relative)
        require(path.is_file(), 'missing frozen ' + key)
        contents[key] = path.read_bytes()
        require(hashlib.sha256(contents[key]).hexdigest() == resolution[key + '_sha256'], 'frozen ' + key + ' digest mismatch')
    expected = installed_distributions(contents['report'], 'pip-report')
    report = json.loads(contents['report'])
    requirements, digests = [], {}
    for item in report['install']:
        name = canonicalize_name(item['metadata']['name'])
        digest = item['download_info']['archive_info']['hashes']['sha256']
        require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'malformed frozen artifact digest')
        digests[name] = digest
        requirements.append(f'{name}=={expected[name]} --hash=sha256:{digest}')
    require(contents['requirements'].decode() == '\n'.join(sorted(requirements)) + '\n', 'frozen requirements/report disagree')
    for row in case['resolved']:
        name = canonicalize_name(row['name'])
        require(observed_mismatch or (expected.get(name) == row['version'] and
                digests.get(name) == row.get('artifact_sha256')), 'resolved artifact not in frozen report')
    return expected


def reconcile_invocations(case, artifact_root, plan_sha256):
    path = safe_path(artifact_root, f'invocations/{case["case_id"]}-attempt-{case["attempt"]}.json')
    successful = case['status'] in ('passed', 'quarantined', 'flaky')
    require(path.is_file(), 'missing invocation manifest')
    require(path.stat().st_size <= 1024 * 1024, 'oversized invocation manifest')
    manifest = json.loads(path.read_text())
    require(manifest.get('schema_version') == 1 and manifest.get('case_id') == case['case_id'] and
            manifest.get('attempt') == case['attempt'] and manifest.get('source_sha') == case['source_sha'] and
            manifest.get('plan_sha256') == plan_sha256, 'invocation identity mismatch')
    retained = case['contract']
    execution_observed = bool(retained['junit_paths'] or retained['tests'] or retained['scenarios'])
    # Status is a conclusion, not permission to discard provenance. A retained
    # execution (including a failed one) requires both authenticated stages.
    execution_started = execution_observed or manifest.get('contract') is not None
    collected = None
    for name in ('collection', 'contract'):
        record = manifest.get(name)
        required = successful or execution_observed or (name == 'collection' and execution_started)
        if record is None:
            require(not required, 'missing invocation record for retained execution: ' + name)
            continue
        require(isinstance(record, dict), 'invalid invocation record')
        code = record.get('exit_code')
        require((code is None or type(code) is int) and type(record.get('timed_out')) is bool,
                'invalid invocation status')
        require(timestamp(record['completed_at']) >= timestamp(record['started_at']), 'invalid invocation chronology')
        if successful or (name == 'collection' and execution_started):
            require(not record['timed_out'] and type(code) is int and code in ((0,) if name == 'collection' else (0, 1)),
                    'runner/collection invocation failure cannot attest execution')
        relative, digest = record.get('evidence_path'), record.get('evidence_sha256')
        require((relative is None) == (digest is None), 'incomplete invocation evidence identity')
        if relative is not None:
            evidence = safe_path(artifact_root, relative)
            require(evidence.is_file() and hashlib.sha256(evidence.read_bytes()).hexdigest() == digest,
                    'invocation ' + ('JUnit' if name == 'contract' else 'collection') + ' evidence digest mismatch')
            if name == 'contract':
                require(retained['junit_paths'] == [relative], 'invocation/JUnit path mismatch')
                status_path = safe_path(artifact_root, relative + '.exit-code')
                shell_code = 124 if record['timed_out'] else (128 - code if code is not None and code < 0 else code)
                require(shell_code is not None and status_path.is_file() and
                        status_path.read_text().strip() == str(shell_code), 'invocation exit-code disagreement')
            else:
                collected = json.loads(evidence.read_text())
                require(isinstance(collected, dict), 'invalid invocation collection evidence')
        else:
            require(not (successful or execution_observed or (name == 'collection' and execution_started)),
                    'missing invocation evidence for retained execution: ' + name)
    if execution_started:
        collection, execution = manifest['collection'], manifest['contract']
        nodes = collected.get('nodes') if collected is not None else None
        require(isinstance(nodes, list) and nodes and all(isinstance(node, str) for node in nodes) and
                len(nodes) == len(set(nodes)) and collected.get('deselected') == [] and not collected.get('errors'),
                'invocation contract started without successful full collection')
        require(timestamp(execution['started_at']) >= timestamp(collection['completed_at']),
                'invocation contract started before collection completed')
        observed = sorted(t['test_id'] for t in retained['tests'])
        # A genuinely crashed contract may execute only part of the collected
        # suite, but cannot execute nodes absent from its authenticated collection.
        require(set(observed) <= set(nodes) and (not successful or observed == sorted(nodes)),
                'invocation collection/JUnit completeness mismatch')
    return manifest


def reconcile_failure(case, manifest, observed_tests, artifact_root):
    """A failure label is not evidence that a successful attempt actually failed.

    Keep real pre-execution failures UNKNOWN. Once execution has been observed,
    reconcile the claimed failure stage and exact failing IDs with the original
    invocation/JUnit rather than letting it justify invented flaky recovery.
    """
    if case['status'] not in ('failed', 'blocked'):
        return
    failure = case['failure']
    require(failure is not None, 'failure evidence missing for failed/blocked case')
    claimed = failure['test_ids']
    require(len(claimed) == len(set(claimed)), 'failure evidence has duplicate test IDs')
    collection, execution = manifest.get('collection'), manifest.get('contract')
    bad = {t['test_id'] for t in observed_tests if t['outcome'] in ('failure', 'error')}
    if observed_tests and failure['kind'] != 'collection':
        require(set(claimed) == bad, 'failure evidence test IDs disagree with original JUnit')
    if bad and execution is not None:
        require(execution['timed_out'] or execution['exit_code'] != 0,
                'failure evidence contradicts successful invocation')
    if failure['kind'] in ('install', 'resolution'):
        require(not observed_tests and collection is None and execution is None,
                'failure evidence contradicts claimed pre-execution stage')
    elif failure['kind'] == 'contract':
        require(execution is not None, 'contract failure evidence requires an actual invocation')
        require(set(claimed) == bad, 'contract failure evidence test IDs disagree with original JUnit')
        invocation_failed = execution['timed_out'] or execution['exit_code'] != 0
        skipped = any(t['outcome'] == 'skipped' for t in observed_tests)
        partial = False
        if collection and collection.get('evidence_path'):
            collected = json.loads(safe_path(artifact_root, collection['evidence_path']).read_text())
            partial = set(collected.get('nodes', [])) != {t['test_id'] for t in observed_tests}
        require(bad or skipped or invocation_failed or partial,
                'contract failure evidence contradicts preserved successful execution')
    elif failure['kind'] in ('automation', 'infrastructure'):
        # Collector failures synthesize UNKNOWN observations; they must not turn
        # a fully preserved successful execution into an invented earlier flake.
        complete_pass = (observed_tests and all(t['outcome'] == 'passed' for t in observed_tests) and
            execution is not None and not execution['timed_out'] and execution['exit_code'] == 0 and
            collection is not None and not collection['timed_out'] and collection['exit_code'] == 0 and
            collection.get('evidence_path') is not None)
        if complete_pass:
            collected = json.loads(safe_path(artifact_root, collection['evidence_path']).read_text())
            require(collected.get('errors') or collected.get('deselected') or
                    set(collected.get('nodes', [])) != {t['test_id'] for t in observed_tests},
                    'automation/infrastructure failure evidence contradicts preserved successful execution')
    elif failure['kind'] == 'collection':
        # A missing report may be genuinely unobserved. A preserved successful
        # full collection followed by passing execution cannot be called failure.
        if collection is not None:
            failed = collection['timed_out'] or collection['exit_code'] != 0
            if collection.get('evidence_path'):
                collected = json.loads(safe_path(artifact_root, collection['evidence_path']).read_text())
                nodes = collected.get('nodes', [])
                failed = failed or bool(collected.get('errors') or collected.get('deselected')) or not nodes or len(nodes) != len(set(nodes))
            else:
                failed = True  # malformed/missing collection output
            require(failed or any(t['outcome'] == 'error' for t in observed_tests),
                    'collection failure evidence contradicts preserved successful collection')


def reconcile_artifacts(case, artifact_root, resolution=None, plan_sha256=None):
    require(artifact_root is not None, 'execution evidence requires an artifact root')
    lock = case['dependency_lock']
    mismatch = (case['status'] in ('failed', 'blocked') and case['failure'] is not None and
                case['failure']['kind'] == 'requested_resolved_mismatch')
    # Absence of an observation is UNKNOWN, not evidence of an incompatible
    # installation. Authenticate the claimed mismatch even when no lock exists.
    if mismatch:
        require(lock is not None, 'requested/resolved failure evidence requires an observed installed lock')
    if lock:
        path = safe_path(artifact_root, lock['path'])
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == lock['sha256'], 'lock digest mismatch')
        require(case['language'] == 'python', 'native lock adapter not yet qualified')
        installed = installed_distributions(path.read_bytes(), lock['format'])
        for row in case['resolved']:
            require(installed.get(canonicalize_name(row['name'])) == row['version'], 'installed lock/resolved mismatch')
        frozen = None
        if resolution is not None:
            frozen = preserved_resolution(case, resolution, artifact_root, observed_mismatch=mismatch)
            require(installed == frozen or mismatch, 'complete installed lock/frozen resolution mismatch')
        if mismatch:
            require((frozen is not None and installed != frozen) or
                    any(installed.get(canonicalize_name(r['name'])) != r['selected_version']
                        for r in case['requested']), 'requested/resolved failure evidence contradicts frozen installation')
            # A truthful failed install-mismatch row retains the actual complete
            # inventory, not a fictional copy of the intended frozen environment.
            # It can never attest passed/quarantined/flaky compatibility.
    manifest = None
    if plan_sha256 is not None:
        manifest = reconcile_invocations(case, artifact_root, plan_sha256)
    paths = case['contract']['junit_paths']
    require(len(paths) == len(set(paths)), 'duplicate JUnit report')
    exits = []
    for relative in paths:
        sidecar = safe_path(artifact_root, relative + '.exit-code')
        require(sidecar.is_file() and sidecar.stat().st_size <= 16, 'missing/malformed invocation exit-code sidecar')
        text = sidecar.read_text()
        require(re.fullmatch(r'(?:0|[1-9][0-9]{0,2})\n?', text) is not None, 'malformed invocation exit-code')
        exits.append(int(text))
    successful = case['status'] in ('passed', 'quarantined', 'flaky')
    if paths and successful:
        observed_tests = invocation_tests([safe_path(artifact_root, p) for p in paths], exits)
    else:
        observed_tests = [t for path in paths for t in junit_tests(safe_path(artifact_root, path))]
        if any(exits):
            require(case['failure'] is not None, 'failed invocation requires failure evidence')
    claimed = case['contract']['tests']
    require(len(observed_tests) == len(claimed) and len({t['test_id'] for t in observed_tests}) == len(observed_tests),
            'JUnit test count/identity mismatch')
    actual = {t['test_id']: t for t in observed_tests}
    for test in claimed:
        require(test['test_id'] in actual, 'JUnit test identity mismatch')
        row = actual[test['test_id']]
        require(all(test[k] == row[k] for k in ('outcome', 'failure_signature')) and
                abs(test['duration_seconds'] - row['duration_seconds']) <= 0.000001,
                f'JUnit outcome/signature/duration mismatch: {test["test_id"]}')
    if manifest is not None:
        reconcile_failure(case, manifest, observed_tests, artifact_root)


def reconcile_plan(data, plan, raw):
    require(json.loads(raw) == plan, 'trusted plan object/bytes mismatch')
    run = data['run']
    require(hashlib.sha256(raw).hexdigest() == run['plan_sha256'], 'plan digest mismatch')
    for field in ('source_sha', 'workflow_sha', 'cooldown_hours', 'cooldown_cutoff', 'self_test'):
        require(run[field] == plan[field], f'trusted plan {field} mismatch')
    planned = {c['case_id']: c for c in plan['cases']}
    require(len(planned) == len(plan['cases']) and set(planned) == set(run['planned_case_ids']), 'trusted planned cases mismatch')
    for case in data['cases']:
        require(case['case_id'] in planned, 'unplanned result')
        trusted = planned[case['case_id']]
        for field in ('integration_id', 'mode', 'selector', 'requested'):
            require(case[field] == trusted[field], f'trusted case {field} mismatch')
        require(case['runtime']['version'].split('.')[:2] == trusted['runtime'].split('.')[:2], 'trusted runtime mismatch')
        if case['status'] in ('passed', 'quarantined', 'flaky'):
            require(trusted['resolution'] is not None and not trusted['problems'], 'pass without trusted resolution')
            require(case['resolved'] == trusted['resolution']['resolved'], 'trusted resolved tuple mismatch')
        expected = [{**d, 'resolved_version': next((r['version'] for r in case['resolved'] if
                     canonicalize_name(r['name']) == canonicalize_name(d['name'])), None)} for d in trusted['discovery']]
        require(case['discovery'] == expected, 'trusted discovery mismatch')


def validate_results(data, registry=None, expected_source_sha=None, expected_case_ids=None,
                     *, artifact_root=None, source_root=ROOT, expected_plan=None, plan_bytes=None):
    shape(data, 'results')
    if registry is None:
        registry = load_registry(source_root)
    if expected_plan is not None:
        require(plan_bytes is not None, 'trusted plan bytes required')
        reconcile_plan(data, expected_plan, plan_bytes)
    run = data['run']
    require(timestamp(run['started_at']) - timestamp(run['cooldown_cutoff']) == timedelta(hours=72),
            'cutoff must be 72 hours before immutable plan start')
    require(timestamp(run['completed_at']) >= timestamp(run['started_at']), 'run completed before start')
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
        successful = case['status'] in ['passed', 'quarantined', 'flaky']
        normalize = canonicalize_name if case['language'] == 'python' else lambda name: name
        for field in ('requested', 'resolved'):
            names = [normalize(r['name']) for r in case[field]]
            require(len(names) == len(set(names)), 'duplicate distribution identity across roles')
            if target:
                roles = {normalize(target['distribution']): 'instrumentation'}
                roles.update({normalize(t['distribution']): 'core' for t in registry['integrations']
                              if t['language'] == target['language'] and t['role'] == 'core'})
                roles.update({normalize(u['name']): 'upstream' if u['role'] == 'primary' else 'companion'
                              for u in target['upstream']})
                require(all(roles.get(normalize(r['name'])) == r['role'] for r in case[field]),
                        'registered distribution role mismatch')
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
                requested_version(req, case['language'])
            if target:
                validate_contract(case, target, source_root)
                if case['language'] == 'python':
                    require(case['runtime']['implementation'] == target['runtimes']['implementation'] and
                            SpecifierSet(target['runtimes']['declared']).contains(case['runtime']['version']),
                            'execution runtime outside registered constraints')
                    for upstream in target['upstream']:
                        matches = [r for r in case['requested'] if canonicalize_name(r['name']) == canonicalize_name(upstream['name'])
                                   and r['role'] == ('upstream' if upstream['role'] == 'primary' else 'companion')]
                        require(len(matches) == 1, 'requested upstream role mismatch')
                        version = matches[0]['selected_version']
                        require(upstream['declared_range'] is not None and
                                SpecifierSet(upstream['declared_range']).contains(version, prereleases=True),
                                'selected upstream outside registered support')
                        if case['selector'] == 'floor':
                            require(version == upstream['exact_floor'], 'selected version is not the exact registered floor')
                verdict = judge_tests(tests, target, case['runtime'], case['resolved'])
                require(verdict == ('passed' if case['status'] == 'flaky' else case['status']), 'false pass/quarantine/flaky status')
            else:
                require(case['status'] != 'passed' or all(t['outcome'] == 'passed' for t in tests), 'pass with failures')
            require(case['status'] not in ('passed', 'flaky') or case['failure'] is None, 'pass with a failure')
        if successful or tests or contract['scenarios'] or contract['junit_paths'] or case['dependency_lock'] or (case['failure'] is not None and case['failure']['kind'] in ('contract', 'requested_resolved_mismatch')):
            resolution = None
            if expected_plan is not None:
                resolution = next(c['resolution'] for c in expected_plan['cases'] if c['case_id'] == case['case_id'])
            reconcile_artifacts(case, artifact_root, resolution, run['plan_sha256'])
        discovery_names = [canonicalize_name(d['name']) if case['language'] == 'python' else d['name'] for d in case['discovery']]
        require(len(set(discovery_names)) == len(discovery_names), 'duplicate discovery package')
        if successful and case['selector'] == 'latest_eligible':
            expected_names = {canonicalize_name(u['name']) if case['language'] == 'python' else u['name']
                              for u in target['upstream']} if target else {
                              name for (name, role) in resolved if role in ('upstream', 'companion')}
            require(expected_names and set(discovery_names) == expected_names, 'incomplete latest-eligible discovery')
        for discovery in case['discovery']:
            found = [r for r in case['resolved'] if (canonicalize_name(r['name']) if case['language'] == 'python' else r['name']) ==
                     (canonicalize_name(discovery['name']) if case['language'] == 'python' else discovery['name'])]
            if successful:
                require(len(found) == 1 and discovery['resolved_version'] == found[0]['version'] == discovery['requested_version'], 'discovery/resolution mismatch')
                require(not discovery['yanked'], 'pass with yanked release')
                age = discovery['published_at'] or discovery['first_seen_at']
                require(age is not None and timestamp(age) <= timestamp(run['cooldown_cutoff']), 'release inside cooldown or missing eligibility time')
                if case['selector'] == 'latest_eligible':
                    require(discovery['absolute_latest'] is not None and discovery['latest_eligible'] is not None and
                            discovery['requested_version'] == discovery['latest_eligible'], 'false latest-eligible claim')
                    if case['language'] == 'python':
                        version = Version(discovery['latest_eligible'])
                        require(not version.is_prerelease and not version.is_devrelease, 'stable latest-eligible cannot select prerelease/dev')
        if successful and case['selector'] == 'prerelease':
            require(target and 'prerelease' in target['canary']['selectors'], 'exploratory prerelease cell not registered')
    require(set(groups) == planned, 'missing planned evidence')
    for attempts in groups.values():
        attempts.sort(key=lambda c: c['attempt'])
        require(attempts[0]['attempt'] == 1, 'missing first attempt')
        require(attempts[0]['status'] != 'flaky', 'flaky requires a prior retained failure')
        if len(attempts) > 1:
            first, second = attempts
            for field in ['source_sha', 'integration_id', 'mode', 'selector', 'runtime', 'requested']:
                require(first[field] == second[field], f'retry changed immutable {field}')
            # A missing/malformed retry upload is not a new observation of an
            # empty installed environment. Preserve attempt 1 and admit only a
            # wholly unknown, explicitly blocked automation retry; do not copy
            # old observations/JUnit into it or permit partial/changed evidence.
            later_contract = second['contract']
            unknown_retry = (second['status'] == 'blocked' and second['failure'] is not None and
                second['failure']['kind'] == 'automation' and not second['resolved'] and
                second['dependency_lock'] is None and not later_contract['tests'] and
                not later_contract['junit_paths'] and not later_contract['scenarios'] and
                later_contract['collected'] == later_contract['executed'] == later_contract['unexpected_skips'] == 0 and
                later_contract['suite_sha256'] == hashlib.sha256(b'').hexdigest())
            # Missing first observations may populate after install/runner failure.
            # Every observation actually present in both attempts stays immutable.
            later = {(canonicalize_name(r['name']), r['role']): r for r in second['resolved']}
            require(unknown_retry or all(later.get((canonicalize_name(r['name']), r['role'])) == r for r in first['resolved']),
                    'retry changed immutable resolved observation')
            if first['dependency_lock'] is not None and not unknown_retry:
                require(first['dependency_lock'] == second['dependency_lock'], 'retry changed immutable dependency_lock')
            unknown_suite = (not first['contract']['tests'] and not first['contract']['junit_paths'] and
                             first['contract']['suite_sha256'] == hashlib.sha256(b'').hexdigest())
            require(unknown_retry or unknown_suite or first['contract']['suite_sha256'] == second['contract']['suite_sha256'], 'retry changed suite')
            if second['status'] == 'flaky':
                require(first['status'] in ('failed', 'blocked') and first['failure'] is not None,
                        'flaky requires a retained genuine failure')
            require(not (first['status'] in ['failed', 'blocked'] and second['status'] in ('passed', 'quarantined')),
                    'fail-then-pass must be flaky')
    return data


def failure_signature(kind, test_ids, text, diagnostic=None):
    # Pytest source excerpts may contain earlier assertions or exception names.
    # Only its actual diagnostic (message attribute / E-prefixed lines) is evidence.
    errors = [re.sub(r'^E\s+', '', line).strip() for line in text.splitlines() if re.match(r'^E\s+', line)]
    if diagnostic:
        message = diagnostic.strip()
        # Assertion messages omit their type in pytest's message attribute.
        if message.startswith('assert '):
            message = 'AssertionError: ' + message
    elif errors:
        message = '\n'.join(errors)
    else:
        diagnostics = [line.strip() for line in text.splitlines()
                       if re.match(r'^[\w.]+(?:Error|Exception|Failure):', line.strip())]
        require(diagnostics, 'missing actual exception diagnostic')
        message = diagnostics[-1]
    # Include the actual exception type even if a plugin emits a terse message.
    types = [re.match(r'^([\w.]+):', line).group(1) for line in errors if re.match(r'^([\w.]+):', line)]
    if types and not message.startswith(types[-1] + ':'):
        message = types[-1] + ': ' + message
    message = re.sub(r'0x[0-9a-fA-F]+', '<address>', message)
    message = re.sub(r'(?:/[^\s:]+)+/', '<path>/', message)
    message = re.sub(r'\d{4}-\d\d-\d\d[T ][\d:.+Z-]+', '<time>', message)
    message = re.sub(r'(?i)(?:sk-[\w-]+|bearer\s+\S+|(?:api[_-]?key|token|password)\s*[=:]\s*\S+)', '<redacted>', message)
    return hashlib.sha256(json.dumps([kind, sorted(test_ids), message], separators=(',', ':')).encode()).hexdigest()


def junit_tests(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024, f'missing/oversized JUnit: {path}')
    raw = path.read_bytes()
    require(b'<!DOCTYPE' not in raw and b'<!ENTITY' not in raw, 'unsafe JUnit declarations')
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f'malformed JUnit: {path}') from exc
    require(tree.tag in ('testsuites', 'testsuite'), 'invalid JUnit root')
    tests = []
    for node in tree.iter('testcase'):
        file = node.get('file', '')
        require(file.endswith('.py') and not PurePosixPath(file).is_absolute() and '..' not in PurePosixPath(file).parts and '\\' not in file,
                'JUnit requires repository-relative file attributes (use --rootdir and junit_family=xunit1)')
        require(node.get('name'), 'JUnit missing test name')
        classname = node.get('classname', '')
        marker = PurePosixPath(file).stem
        suffix = classname.rsplit(marker, 1)[-1].strip('.') if marker in classname else ''
        test_id = file + '::' + (suffix.replace('.', '::') + '::' if suffix else '') + node.get('name')
        children = [c for c in node if c.tag in ('failure', 'error', 'skipped')]
        require(len(children) <= 1, 'ambiguous JUnit outcomes')
        outcome, signature = 'passed', None
        if children:
            child = children[0]
            outcome = child.tag
            if outcome != 'skipped':
                signature = failure_signature(outcome, [test_id], child.text or '', child.get('message'))
        duration = float(node.get('time', '0'))
        require(math.isfinite(duration) and duration >= 0, 'invalid JUnit duration')
        tests.append(dict(test_id=test_id, outcome=outcome, duration_seconds=duration,
                          failure_signature=signature, exception_id=None))
    require(tests, f'empty JUnit collection: {path}')
    require(len({t['test_id'] for t in tests}) == len(tests), 'duplicate JUnit test IDs')
    for suite in tree.iter('testsuite'):
        nodes = list(suite.iter('testcase'))
        counts = dict(tests=len(nodes), failures=sum(n.find('failure') is not None for n in nodes),
                      errors=sum(n.find('error') is not None for n in nodes), skipped=sum(n.find('skipped') is not None for n in nodes))
        for key, value in counts.items():
            require(key not in suite.attrib or int(suite.get(key)) == value, f'JUnit {key} count mismatch')
    return tests


def invocation_tests(paths, exits):
    require(paths and len(paths) == len(exits), 'each invocation requires its own exit code and JUnit')
    tests = []
    for path, exit_code in zip(paths, exits):
        require(exit_code in (0, 1), 'runner/collection error cannot be quarantined')
        suite = junit_tests(path)
        require((exit_code == 0) == all(t['outcome'] == 'passed' for t in suite),
                f'runner/JUnit outcome mismatch for invocation: {path}')
        tests.extend(suite)
    return tests


# Trusted workflow-only runner. Results ingestion never calls this function and
# never interprets an artifact's command/reproduction arrays as executable argv.
_NETWORK_BOOTSTRAP = r'''
import json, os, pathlib, socket, subprocess, sys
assert sys.platform == "linux"
links = json.loads(subprocess.check_output(["ip", "-j", "link", "show"]))
assert [link["ifname"] for link in links] == ["lo"]
assert len(pathlib.Path("/proc/net/route").read_text().splitlines()) == 1
status = pathlib.Path("/proc/self/status").read_text()
assert "CapEff:\t0000000000000000" in status and "NoNewPrivs:\t1" in status
code = """import socket
for family, host in ((socket.AF_INET, '1.1.1.1'), (socket.AF_INET6, '2606:4700:4700::1111')):
    for method in ('connect', 'connect_ex'):
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            try:
                result = getattr(connection, method)((host, 443))
            except OSError:
                pass
            else:
                assert method == 'connect_ex' and result != 0, 'External connection succeeded'
"""
exec(code)
subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)
with socket.socket() as server:
    server.bind(("127.0.0.1", 0)); server.listen(1); server.settimeout(5)
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c",
        "import socket,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); s.sendall(b'loopback-ok'); s.close()",
        str(server.getsockname()[1])])
    connection, _ = server.accept()
    with connection:
        assert connection.recv(64) == b"loopback-ok"
    assert child.wait(timeout=5) == 0
pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(network_isolation="passed", loopback="passed",
    ipv4="denied", ipv6="denied", child_external="denied", child_loopback="passed", connect_ex="denied")))
os.chdir(sys.argv[2])
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
'''


def sandbox_run(command, cwd, output):
    require(sys.platform == 'linux', 'Linux network namespace required; no host fallback')
    require(command, 'missing trusted workflow command')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only root sets up the empty namespace. Test/dependency code runs as the
    # original unprivileged UID, no supplementary groups, capabilities or sudo.
    script = r'''
set -euo pipefail
ip link set dev lo up
uid=$1; gid=$2; python=$3; library=$4; bootstrap=$5; shift 5
exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 LD_LIBRARY_PATH="$library" \
  FI_BASE_URL=http://127.0.0.1:9 FI_GRPC_URL=http://127.0.0.1:9 \
  "$python" -I -S -c "$bootstrap" "$@"
'''
    return subprocess.run(['sudo', '--non-interactive', 'unshare', '--net', '--', '/bin/bash', '-c', script,
        'compat-sandbox', str(os.getuid()), str(os.getgid()), sys.executable, os.environ.get('LD_LIBRARY_PATH', ''),
        _NETWORK_BOOTSTRAP, str(output), str(Path(cwd).resolve()), *command], check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['registry', 'results', 'verdict', 'fixture-gate']:
        command = commands.add_parser(name)
        command.add_argument('--root', type=Path, default=ROOT)
        if name == 'results':
            command.add_argument('path', type=Path)
            command.add_argument('--plan', type=Path, help='Original immutable compat-plan.json (default: beside results)')
        if name == 'verdict':
            command.add_argument('--integration-id', required=True)
            command.add_argument('--context', type=Path, required=True)
            command.add_argument('--junit', action='append', type=Path, default=[])
            command.add_argument('--exit-code', type=int, action='append', required=True,
                                 help='One status per --junit, in the same order; never an aggregated maximum')
        if name == 'fixture-gate':
            command.add_argument('kind', choices=['scoped', 'execution'])
    sandbox = commands.add_parser('sandbox', help='Trusted workflow argv only, never artifact commands')
    sandbox.add_argument('--cwd', required=True, type=Path)
    sandbox.add_argument('--probe-output', required=True, type=Path)
    sandbox.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == 'sandbox':
            command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            return sandbox_run(command, args.cwd, args.probe_output)
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
            plan_path = args.plan or args.path.parent / 'compat-plan.json'
            raw = plan_path.read_bytes()
            source_sha = subprocess.check_output(['git', '-C', str(args.root), 'rev-parse', 'HEAD'], text=True).strip()
            validate_results(data, registry, expected_source_sha=source_sha,
                             artifact_root=args.path.parent, source_root=args.root,
                             expected_plan=json.loads(raw), plan_bytes=raw)
        elif args.command == 'verdict':
            target = next(t for t in registry['integrations'] if t['id'] == args.integration_id)
            context = json.loads(args.context.read_text())
            tests = invocation_tests(args.junit, args.exit_code)
            verdict = judge_tests(tests, target, context['runtime'], context['resolved'])
            print(f'{target["id"]}: {verdict}; executed={len(tests)}; qualification={target["qualification"]["status"]}')
        else:
            print(f'Validated {len(registry["integrations"])} inventory records (not a compatibility claim).')
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        print(f'compatibility: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
