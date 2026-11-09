"""Keep the legacy TypeScript integration discoverable without recommending it."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "typescript/packages/traceai_llamaindex"


class LlamaIndexTypeScriptDeprecationTests(unittest.TestCase):
    def test_notice_precedes_legacy_setup_and_distinguishes_python(self):
        readme = (PACKAGE / "README.md").read_text()
        notice = readme.split("## Installation", 1)[0]
        self.assertIn("Deprecated", notice)
        self.assertIn("https://github.com/run-llama/LlamaIndexTS#deprecation-notice", notice)
        self.assertIn("no longer maintained upstream", notice)
        self.assertIn("Do not use this integration for new applications", notice)
        self.assertIn("Python integration is not deprecated", notice)
        self.assertIn("https://docs.futureagi.com/docs/integrations/traceai/llamaindex", notice)
        self.assertIn("not a drop-in replacement", notice)

    def test_catalogs_mark_only_the_typescript_integration(self):
        root_readme = (ROOT / "README.md").read_text()
        row = next(line for line in root_readme.splitlines() if '"@traceai/llamaindex"' in line)
        self.assertIn('[SDK](python/frameworks/llama_index/ "traceAI-llamaindex")', row)
        self.assertIn('[Deprecated](typescript/packages/traceai_llamaindex/ "@traceai/llamaindex")', row)
        ts_readme = (ROOT / "typescript/README.md").read_text()
        self.assertIn("`@traceai/llamaindex` (deprecated)", ts_readme)

    def test_package_and_release_entries_remain_available(self):
        package = json.loads((PACKAGE / "package.json").read_text())
        self.assertEqual(package["name"], "@traceai/llamaindex")
        self.assertIn(".", package["exports"])
        self.assertTrue((PACKAGE / "src/index.ts").is_file())
        path = "typescript/packages/traceai_llamaindex"
        manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
        config = json.loads((ROOT / "release-please-config.json").read_text())
        self.assertIn(path, manifest)
        self.assertIn(path, config["packages"])


if __name__ == "__main__":
    unittest.main()
