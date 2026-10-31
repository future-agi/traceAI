"""Foundation package inventory, assets and dependency release order."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import release_plan
import npm_release


class AgentPackages(unittest.TestCase):
    def test_dependency_closure_and_ownership(self):
        inventory = release_plan.typescript_packages()
        target = next(p for p in inventory if p['name'] == '@traceai/agents')
        names = [p['name'] for p in npm_release.dependency_closure([target], inventory)]
        self.assertEqual(names, ['@traceai/fi-semantic-conventions', '@traceai/agent-core', '@traceai/agents'])
        for name in names[-2:]:
            package = next(p for p in inventory if p['name'] == name)
            self.assertTrue(package['dir'].startswith('typescript/packages/'))

    def test_assets_and_no_adapter_activation(self):
        root = release_plan.REPO_ROOT
        runtime = root / 'typescript/packages/traceai_agents'
        package = json.loads((runtime / 'package.json').read_text())
        self.assertEqual(package['dependencies']['better-sqlite3'], '12.4.1')
        self.assertEqual(package['bin'], {'fi-agent': 'bin/fi-agent.cjs'})
        self.assertIn('bin', package['files'])
        self.assertNotIn('contract', package['files'])
        capabilities = json.loads((runtime / 'profiles/capabilities.json').read_text())
        self.assertEqual(capabilities['enabled_by_default'], [])
        cursor = [a for a in capabilities['agents'] if a['agent'] == 'cursor']
        self.assertEqual([a['surface'] for a in cursor], ['ide', 'cli'])
        self.assertTrue(all(a['qualification'] == 'documented-payload-fixtures-only' for a in cursor))
        self.assertTrue((runtime / 'bin/fi-cursor-hook.cjs').stat().st_mode & 0o111)
        self.assertEqual(package['dependencies']['jsonc-parser'], '3.3.1')
        lock = (root / 'typescript/pnpm-lock.yaml').read_text()
        self.assertEqual(lock.count('  jsonc-parser@3.3.1:'), 2, 'package and snapshot records are both required')
        for schema in (root / 'integrations/coding-agents/schema').glob('*.schema.json'):
            self.assertEqual(json.loads(schema.read_text()), json.loads((runtime / 'schema' / schema.name).read_text()))
            self.assertEqual(json.loads(schema.read_text()), json.loads((root / 'typescript/packages/traceai_agent_core/schema' / schema.name).read_text()))


if __name__ == '__main__':
    unittest.main()
