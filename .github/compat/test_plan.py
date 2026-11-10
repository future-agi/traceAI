import copy
import json
import tempfile
import unittest
from pathlib import Path

from discover import discover, pypi, timestamp
from plan import generate as generate_plan, safe_path, verify_metadata

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
NOW = timestamp("2026-10-28T00:00:00Z")
SHA = "1" * 40
RUNTIMES = {v: dict(version=v + ".17", implementation="cpython", os="linux", arch="x86_64",
    tags=["py3-none-any", "cp" + v.replace(".", "") + "-cp" + v.replace(".", "") + "-manylinux_2_17_x86_64"])
    for v in ("3.10", "3.11", "3.13")}


def generate(*args, **kwargs):
    return generate_plan(*args, runtime_identities=RUNTIMES, **kwargs)


def inputs():
    registry = json.loads((ROOT / ".github/compat/registry.json").read_text())
    return registry, discover(registry, NOW, FIXTURES)


class PlanTests(unittest.TestCase):
    def test_registry_fixture_release_changes_plan_without_code_change(self):
        registry, discovery = inputs()
        before = generate(registry, discovery, SHA, SHA, NOW)
        package = next(p for p in discovery["packages"] if p["name"] == "openai")
        raw = {"releases": {"3.25.0": [{"filename": "openai-3.25.0-py3-none-any.whl", "upload_time_iso_8601": "2026-10-24T00:00:00Z",
                                       "requires_python": ">=3.10", "yanked": False}]}}
        package["releases"].extend(pypi("openai", raw, "2026-10-28T00:00:00Z")["releases"])
        after = generate(registry, discovery, SHA, SHA, NOW)
        a = next(c for c in before["cases"] if c["integration_id"] == "python-openai")
        b = next(c for c in after["cases"] if c["integration_id"] == "python-openai")
        self.assertEqual(a["requested"][0]["selected_version"], "3.24.0")
        self.assertEqual(b["requested"][0]["selected_version"], "3.25.0")
        self.assertNotEqual(a, b)

    def test_deterministic_pilot_only_and_exact_floor(self):
        registry, discovery = inputs()
        plan = generate(registry, discovery, SHA, SHA, NOW)
        self.assertEqual(plan, generate(registry, discovery, SHA, SHA, NOW))
        self.assertEqual(len(plan["cases"]), 3)
        self.assertEqual({c["integration_id"] for c in plan["cases"]}, set(registry["defaults"]["pilot_ids"]))
        weekly = generate(registry, discovery, SHA, SHA, NOW, "weekly")
        self.assertLess(len(weekly["cases"]), 48)
        self.assertEqual(len([c for c in weekly["cases"] if c["mode"] == "published"]), 3)
        floors = [c for c in weekly["cases"] if c["integration_id"] == "python-langchain" and c["selector"] == "floor"]
        self.assertEqual([r["selected_version"] for r in floors[0]["requested"][:2]], ["0.2.43", "0.2.0"])
        self.assertEqual({c["runtime"] for c in weekly["cases"]}, {"3.10", "3.11", "3.13"})

    def test_outside_cap_and_release_cooldown_visible_not_latest_pass(self):
        registry, discovery = inputs()
        target = next(t for t in registry["integrations"] if t["id"] == "python-openai")
        target["upstream"][0]["declared_range"] = ">=1.69.0,<3"
        plan = generate(registry, discovery, SHA, SHA, NOW)
        case = next(c for c in plan["cases"] if c["integration_id"] == "python-openai")
        self.assertEqual(case["discovery"][0]["absolute_latest"], "3.24.0")
        self.assertEqual(case["discovery"][0]["latest_eligible"], "1.69.0")
        self.assertEqual(case["requested"][0]["selected_version"], "1.69.0")

    def test_exploration_is_allowlisted_complete_tuple_and_nonblocking(self):
        registry, discovery = inputs()
        exploratory = generate(registry, discovery, SHA, SHA, NOW, "weekly", allowlist={"python-openai": {"openai": ">=3"}})
        cases = [c for c in exploratory["cases"] if c["selector"] == "historical"]
        self.assertEqual(len(cases), 3)
        self.assertTrue(all(c["non_blocking"] for c in cases))
        with self.assertRaises(ValueError):
            generate(registry, discovery, SHA, SHA, NOW, "weekly", allowlist={"python-langchain": {"langchain-core": ">=2"}})

    def test_source_metadata_verified(self):
        registry, _ = inputs()
        verify_metadata(registry, ROOT)
        next(t for t in registry["integrations"] if t["id"] == "python-openai")["upstream"][0]["declared_range"] = ">=9"
        with self.assertRaisesRegex(ValueError, "drift"):
            verify_metadata(registry, ROOT)

    def test_json_decoding_bounds_containers_not_escaped_string_contents(self):
        from unittest.mock import patch
        from plan import bounded_json
        data = {'escaped': '\\"' + '[' * 2000 + '\\"', 'valid': [{'x': True}]}
        self.assertEqual(bounded_json(json.dumps(data).encode()), data)
        for raw in ('[' * 65 + '0' + ']' * 65, '{"key":' * 65 + '0' + '}' * 65):
            with self.assertRaisesRegex(ValueError, 'nesting limit'):
                bounded_json(raw)
        with self.assertRaisesRegex(ValueError, 'Oversized JSON'):
            bounded_json('"abcd"', max_bytes=5)
        with patch('plan.json.loads', side_effect=RecursionError('decoder control')):
            with self.assertRaisesRegex(ValueError, 'decoder recursion limit'):
                bounded_json('{}')

    def test_safe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("../escape", "/tmp/file", "bad\\file"):
                with self.assertRaises(ValueError):
                    safe_path(root, value)
            (root / "link").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                safe_path(root, "link/file")

    def test_unusable_release_cannot_displace_usable_canary_and_patch_is_not_zero(self):
        registry, discovery = inputs()
        package = next(p for p in discovery['packages'] if p['name'] == 'openai')
        def release(version, filename):
            return pypi('openai', {'releases': {version: [dict(filename=filename, upload_time_iso_8601='2026-10-01T00:00:00Z',
                yanked=False, requires_python='>=3.11.1')]}}, '2026-10-28T00:00:00Z')['releases'][0]
        package['releases'].extend([release('3.25.0', 'openai-3.25.0.tar.gz'),
            release('3.26.0', 'openai-3.26.0-cp311-cp311-win_amd64.whl')])
        plan = generate(registry, discovery, SHA, SHA, NOW)
        self.assertEqual(next(c for c in plan['cases'] if c['integration_id'] == 'python-openai')['requested'][0]['selected_version'], '3.24.0')
        package['releases'].append(release('3.27.0', 'openai-3.27.0-py3-none-any.whl'))
        plan = generate(registry, discovery, SHA, SHA, NOW)
        case = next(c for c in plan['cases'] if c['integration_id'] == 'python-openai')
        self.assertEqual(case['runtime_identity']['version'], '3.11.17')
        self.assertEqual(case['requested'][0]['selected_version'], '3.27.0')

    def test_build_failures_only_block_source_cases_requiring_that_distribution(self):
        import sys
        import zipfile
        from unittest.mock import patch
        import plan as planner
        registry, discovery = inputs()
        for broken in ('python-langchain', 'python-fi-instrumentation'):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / 'out'
                fixture = root / 'discovery.json'
                fixture.write_text(json.dumps(discovery))
                attempted, resolutions = [], []
                def wheel(command, cwd, log):
                    target = next(t for t in registry['integrations'] if str(ROOT / t['package_dir']) == command[-1])
                    attempted.append(target['id'])
                    if target['id'] == broken:
                        raise ValueError('Synthetic isolated build failure: ' + broken)
                    wheels = Path(command[command.index('--wheel-dir') + 1])
                    wheels.mkdir(parents=True, exist_ok=True)
                    name = target['distribution'].replace('-', '_')
                    with zipfile.ZipFile(wheels / (name + '-1.0.0-py3-none-any.whl'), 'w') as archive:
                        archive.writestr(name + '-1.0.0.dist-info/METADATA', 'Name: ' + target['distribution'] + '\nVersion: 1.0.0\n')
                def resolve(case, source, out, built, python):
                    if not case['problems']:
                        resolutions.append(case['case_id'])
                        if case['mode'] == 'source':
                            self.assertTrue(all(r['name'].lower().replace('_', '-') in built for r in case['requested'] if r['role'] in ('core', 'instrumentation')))
                argv = ['plan', '--root', str(ROOT), '--out', str(output), '--discovery', str(fixture),
                        '--mode', 'weekly', '--resolve']
                for runtime in RUNTIMES:
                    argv.extend(['--python', runtime + '=' + runtime])
                with patch.object(sys, 'argv', argv), patch('plan.run', side_effect=wheel), \
                     patch('plan.resolve', side_effect=resolve), patch('plan.runtime_identity', side_effect=lambda p: RUNTIMES[p]), \
                     patch('plan.subprocess.check_output', return_value=SHA):
                    planner.main()
                planned = json.loads((output / 'compat-plan.json').read_text())['cases']
                self.assertEqual(set(attempted), {'python-fi-instrumentation', 'python-openai', 'python-anthropic', 'python-langchain'})
                affected = [c for c in planned if c['mode'] == 'source' and (broken == 'python-fi-instrumentation' or c['integration_id'] == broken)]
                healthy = [c for c in planned if c not in affected]
                self.assertEqual(len(affected), 18 if broken == 'python-fi-instrumentation' else 6)
                self.assertTrue(all(c['problems'] and c['resolution'] is None for c in affected))
                self.assertTrue(all(not c['problems'] and c['case_id'] in resolutions for c in healthy))
                self.assertEqual(sum(c['mode'] == 'published' and c['case_id'] in resolutions for c in planned), 3)

    def test_langgraph_coupled_companions_do_not_change_primary_or_floor(self):
        from plan import coupled_companions
        registry, discovery = inputs()
        cases = generate(registry, discovery, SHA, SHA, NOW, 'weekly')['cases']
        graph = next(c for c in cases if c['integration_id'] == 'python-langchain' and c['selector'] == 'latest_eligible')
        self.assertEqual([r['selected_version'] for r in graph['requested'][:2]], ['1.2.7', '1.0.5'])
        self.assertEqual([(r['name'], r['version']) for r in graph['coupled_companions']],
                         [('langgraph-prebuilt', '1.0.5'), ('langgraph-checkpoint', '3.0.1')])
        floor = next(c for c in cases if c['integration_id'] == 'python-langchain' and c['selector'] == 'floor')
        self.assertEqual([r['selected_version'] for r in floor['requested'][:2]], ['0.2.43', '0.2.0'])
        self.assertNotIn('coupled_companions', floor)
        graph['requested'][1]['selected_version'] = '1.2.17'
        self.assertEqual(coupled_companions(graph), [])
