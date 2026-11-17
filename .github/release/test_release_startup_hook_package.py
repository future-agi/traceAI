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
HELPER_PATH = '.github/release/check_startup_hook_package.sh'
SMOKE_APP = 'csharp/tests/TraceAI.AutoInstrumentation.PackageSmoke/Program.cs'
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

    def test_build_workflow_runs_the_shared_installed_package_check_on_the_exact_nupkg(self):
        step = BUILD.split('- name: Check the installed startup hook package', 1)[1].split('      - ', 1)[0]
        self.assertIn("if: matrix.name == 'TraceAI.AutoInstrumentation'", step)
        self.assertIn(f'bash {HELPER_PATH} "$RUNNER_TEMP/dist" "$VERSION"', step)
        self.assertLess(BUILD.index('dotnet pack'), BUILD.index(HELPER_PATH))
        self.assertLess(BUILD.index(HELPER_PATH), BUILD.index('actions/upload-artifact@'))
        # The helper starts net8.0 and net10.0 applications, so both runtimes are installed.
        self.assertIn("dotnet-version: '8.0.x'", BUILD)
        self.assertNotIn('unzip -q', BUILD)

    def test_pull_request_ci_runs_the_same_check_on_a_freshly_packed_hook(self):
        ci = (ROOT / '.github/workflows/csharp-ci.yml').read_text()
        job = ci.split('\n  startup-hook-linux:\n', 1)[1]
        self.assertIn('8.0.x', job)
        self.assertIn('10.0.x', job)
        pack = job.index('dotnet pack csharp/src/TraceAI.AutoInstrumentation/TraceAI.AutoInstrumentation.csproj')
        self.assertLess(pack, job.index(f'bash {HELPER_PATH}'))
        self.assertIn('--configuration Release --output "$RUNNER_TEMP/hook-feed"', job)
        self.assertIn(f'bash {HELPER_PATH} "$RUNNER_TEMP/hook-feed"', job)
        for event in ci.split('\njobs:\n', 1)[0].split('  push:\n'):
            for path in (HELPER_PATH, '.github/workflows/release-nuget.yml'):
                self.assertIn(f"- '{path}'", event)


class HookPackageSmokeTests(unittest.TestCase):
    def setUp(self):
        self.helper = (ROOT / HELPER_PATH).read_text()
        self.app = (ROOT / SMOKE_APP).read_text()

    def test_helper_installs_the_exact_nupkg_into_clean_net8_and_net10_apps(self):
        self.assertTrue(self.helper.startswith('#!/usr/bin/env bash\n'))
        self.assertIn('set -euo pipefail', self.helper)
        for tfm in ('net8.0', 'net10.0'):
            self.assertIn(tfm, self.helper)
        for needle in ('NUGET_PACKAGES=', 'dotnet add', 'package TraceAI.AutoInstrumentation', '--source "$FEED"',
                       'traceai.autoinstrumentation/$VERSION/tools/net8.0/any/TraceAI.AutoInstrumentation.dll'):
            self.assertIn(needle, self.helper)
        # The version comes from the argument or the packed file, never a fixed number.
        self.assertNotRegex(self.helper, r'\b0\.1\.0\b')
        self.assertIn('TraceAI.AutoInstrumentation.*.nupkg', self.helper)

    def test_helper_checks_the_payload_and_that_the_package_is_not_a_library(self):
        for name in ('TraceAI.AutoInstrumentation.dll', 'TraceAI.AutoInstrumentation.Runtime.dll', 'FIInstrumentation.dll',
                     'OpenTelemetry.dll', 'OpenTelemetry.Api.dll', 'OpenTelemetry.Exporter.OpenTelemetryProtocol.dll'):
            self.assertIn(PAYLOAD + name, self.helper)
        self.assertIn("'^lib/'", self.helper)
        self.assertIn('<dependency ', self.helper)

    def test_a_fail_open_warning_and_a_clean_exit_do_not_pass(self):
        # The hook catches every start error and lets the application exit 0. Only real AI span delivery passes.
        self.assertIn('Agent could not load', self.helper)
        self.assertIn('INSTALLED_HOOK_EXPORT_PASS', self.helper)
        self.assertIn('NEGATIVE_CONTROL', self.helper)
        self.assertIn('-eq 2', self.helper)
        self.assertIn('env -u DOTNET_STARTUP_HOOKS', self.helper)
        self.assertIn('FI_BASE_URL=http://127.0.0.1:', self.helper)

    def test_smoke_credentials_are_synthetic_and_no_external_collector_is_used(self):
        self.assertIn('sk-test-PLACEHOLDER', self.helper)
        self.assertNotIn('futureagi.com', self.helper + self.app)
        self.assertIn('http://127.0.0.1:', self.app)
        self.assertIn('/tracer/v1/traces', self.app)

    def test_app_proves_span_delivery_private_runtime_and_shared_diagnostic_source(self):
        for needle in ('package-proof-chat', 'HOOK_NOT_LISTENING', 'return 2;', 'INSTALLED_HOOK_EXPORT_PASS',
                       'AssemblyLoadContext.Default', 'traceai-auto', 'TraceAI.AutoInstrumentation.Runtime',
                       'System.Diagnostics.DiagnosticSource'):
            self.assertIn(needle, self.app)


class HookVersionCouplingTests(unittest.TestCase):
    def test_hook_and_sdk_share_one_aligned_baseline(self):
        # The hook compiles the SDK sources into its payload, so the two are versioned together.
        sdk = ET.fromstring((ROOT / 'csharp/src/FIInstrumentation/FIInstrumentation.csproj').read_text()).findtext('.//Version')
        manifest = json.loads((ROOT / '.release-please-manifest.json').read_text())
        self.assertEqual(hook_property('Version'), sdk)
        self.assertEqual(manifest['csharp/src/TraceAI.AutoInstrumentation'], sdk)
        self.assertEqual(manifest['csharp/src/FIInstrumentation'], sdk)

    def test_release_please_links_the_hook_and_sdk_with_only_supported_fields(self):
        text = (ROOT / 'release-please-config.json').read_text()
        config = json.loads(text)
        self.assertNotIn('additional-paths', text)
        linked = [p for p in config['plugins'] if (p['type'] if isinstance(p, dict) else p) == 'linked-versions']
        self.assertEqual(len(linked), 1)
        self.assertEqual(set(linked[0]), {'type', 'groupName', 'components'})
        self.assertEqual(set(linked[0]['components']), {'nuget-fi-instrumentation-otel', 'nuget-TraceAI.AutoInstrumentation'})

    def test_every_csharp_release_component_matches_the_publisher_tag_prefix(self):
        config = json.loads((ROOT / 'release-please-config.json').read_text())['packages']
        for p in release_plan.other_packages(ROOT):
            if p['ecosystem'] != 'nuget':
                continue
            component = config[p['dir']]['component']
            self.assertEqual(f"{component}-v{p['version']}", p['tag'])

    def test_hook_changelog_and_readme_say_it_is_versioned_with_the_sdk(self):
        changelog = (HOOK_DIR / 'CHANGELOG.md').read_text()
        readme = (HOOK_DIR / 'README.md').read_text()
        for text in (changelog, readme):
            self.assertIn('versioned together with FIInstrumentation', text)
        self.assertIn('first NuGet release', readme)
        self.assertRegex(readme, r'(?s)in-process.{0,400}project reference')
        self.assertIn('not available from the NuGet package', readme)


if __name__ == '__main__':
    unittest.main()
