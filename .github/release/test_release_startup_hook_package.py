"""The .NET startup hook ships as a NuGet package: its definition, its payload layout and its release plan entry."""
import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import release_plan  # noqa: E402

ROOT = release_plan.REPO_ROOT
HOOK_DIR = ROOT / 'csharp/src/TraceAI.AutoInstrumentation'
HOOK_PROJECT = HOOK_DIR / 'TraceAI.AutoInstrumentation.csproj'
PAYLOAD = 'tools/net8.0/any/'
BUILD = (ROOT / '.github/workflows/release-nuget.yml').read_text()


def hook_property(name):
    return ET.fromstring(HOOK_PROJECT.read_text()).findtext(f'.//{name}')


class HookPackageDefinitionTests(unittest.TestCase):
    def test_package_has_id_version_readme_and_license(self):
        self.assertEqual(hook_property('PackageId'), 'TraceAI.AutoInstrumentation')
        self.assertRegex(hook_property('Version'), r'^\d+\.\d+\.\d+$')
        self.assertEqual(hook_property('PackageReadmeFile'), 'README.md')
        self.assertEqual(hook_property('PackageLicenseExpression'), 'Apache-2.0')
        self.assertEqual(hook_property('Authors'), 'Future AGI')
        self.assertEqual(hook_property('RepositoryUrl'), 'https://github.com/future-agi/traceAI')
        self.assertTrue((HOOK_DIR / 'README.md').is_file())

    def test_package_carries_the_published_hook_directory_not_a_library(self):
        text = HOOK_PROJECT.read_text()
        # The hook runs from a directory named in DOTNET_STARTUP_HOOKS, not as a referenced library.
        self.assertEqual(hook_property('IncludeBuildOutput'), 'false')
        self.assertEqual(hook_property('SuppressDependenciesWhenPacking'), 'true')
        self.assertIn('TargetsForTfmSpecificContentInPackage', text)
        self.assertIn('Targets="Publish"', text)
        self.assertIn(PAYLOAD + '%(RecursiveDir)%(Filename)%(Extension)', text)

    def test_one_payload_directory_serves_net8_and_net10(self):
        # A startup hook built for a newer runtime cannot load in an older process.
        self.assertEqual(hook_property('TargetFramework'), 'net8.0')
        self.assertIn('net8.0', PAYLOAD)
        runtime = ET.fromstring((HOOK_DIR / 'AgentRuntime/TraceAI.AutoInstrumentation.Runtime.csproj').read_text())
        self.assertEqual(runtime.findtext('.//TargetFramework'), 'net8.0')

    def test_hook_loads_its_private_runtime_from_its_own_directory(self):
        # The package keeps these files together, so the hook directory is what DOTNET_STARTUP_HOOKS points into.
        source = (HOOK_DIR / 'StartupHook.cs').read_text()
        self.assertIn('"TraceAI.AutoInstrumentation.Runtime.dll"', source)
        self.assertIn('CopyLocalLockFileAssemblies', HOOK_PROJECT.read_text())

    def test_readme_names_the_packaged_hook_path(self):
        readme = (HOOK_DIR / 'README.md').read_text()
        self.assertIn('dotnet add package TraceAI.AutoInstrumentation', readme)
        self.assertIn('traceai.autoinstrumentation', readme)
        self.assertIn(PAYLOAD + 'TraceAI.AutoInstrumentation.dll', readme)
        self.assertNotIn('NuGet packages for the agent are not published', readme)


class HookReleasePlanTests(unittest.TestCase):
    def hook(self):
        found = [p for p in release_plan.other_packages(ROOT) if p['name'] == 'TraceAI.AutoInstrumentation']
        self.assertEqual(len(found), 1)
        return found[0]

    def test_release_plan_lists_the_hook(self):
        p = self.hook()
        version = hook_property('Version')
        self.assertEqual(p['ecosystem'], 'nuget')
        self.assertEqual(p['version'], version)
        self.assertEqual(p['dir'], 'csharp/src/TraceAI.AutoInstrumentation')
        self.assertEqual(p['project'], 'csharp/src/TraceAI.AutoInstrumentation/TraceAI.AutoInstrumentation.csproj')
        self.assertEqual(p['tag'], f'nuget-TraceAI.AutoInstrumentation-v{version}')

    def test_only_the_hook_project_is_listed_not_its_private_projects(self):
        names = {p['name'] for p in release_plan.other_packages(ROOT) if p['ecosystem'] == 'nuget'}
        self.assertEqual(names, {'fi-instrumentation-otel', 'TraceAI.AutoInstrumentation'})

    def test_release_please_covers_the_hook_and_bumps_its_project_version(self):
        self.assertEqual(release_plan.coverage_problems(ROOT, [self.hook()]), [])
        config = json.loads((ROOT / 'release-please-config.json').read_text())['packages']
        entry = config['csharp/src/TraceAI.AutoInstrumentation']
        self.assertEqual(entry['package-name'], 'TraceAI.AutoInstrumentation')
        paths = {e['path'] for e in entry['extra-files']}
        self.assertEqual(paths, {'TraceAI.AutoInstrumentation.csproj'})
        manifest = json.loads((ROOT / '.release-please-manifest.json').read_text())
        self.assertEqual(manifest['csharp/src/TraceAI.AutoInstrumentation'], hook_property('Version'))

    def test_hook_is_not_skipped_by_the_release_config(self):
        self.assertNotIn('TraceAI.AutoInstrumentation', release_plan.load_config()['skip'])

    def test_build_workflow_checks_the_payload_of_the_exact_nupkg(self):
        self.assertIn(PAYLOAD + 'TraceAI.AutoInstrumentation.dll', BUILD)
        self.assertIn(PAYLOAD + 'TraceAI.AutoInstrumentation.Runtime.dll', BUILD)
        self.assertIn("matrix.name == 'TraceAI.AutoInstrumentation'", BUILD)
        self.assertLess(BUILD.index('dotnet pack'), BUILD.index(PAYLOAD + 'TraceAI.AutoInstrumentation.dll'))
        self.assertLess(BUILD.index(PAYLOAD + 'TraceAI.AutoInstrumentation.dll'), BUILD.index('actions/upload-artifact@'))


if __name__ == '__main__':
    unittest.main()
