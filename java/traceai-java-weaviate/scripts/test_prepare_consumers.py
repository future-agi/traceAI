"""Keep the standalone consumer faithful to the customer install instructions."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


class ReadmeConsumerTest(unittest.TestCase):
    def test_literal_readme_dependencies_match_local_install(self):
        module = Path(__file__).resolve().parents[1]
        snippet = re.search(r"```xml\n(.*?)\n```", (module / "README.md").read_text(), re.S).group(1)
        expected = ET.fromstring("<dependencies>" + snippet + "</dependencies>")
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        local_version = ET.parse(module / "pom.xml").findtext("m:parent/m:version", namespaces=ns)
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, "-I", str(module / "scripts/prepare_consumers.py"),
                            "--output", tmp], check=True, capture_output=True)
            actual = ET.parse(Path(tmp) / "v6/pom.xml").find("m:dependencies", ns)
            for dep in expected:
                artifact = dep.findtext("artifactId")
                generated = next(d for d in actual if d.findtext("m:artifactId", namespaces=ns) == artifact)
                for element in dep:
                    self.assertEqual(generated.findtext("m:" + element.tag, namespaces=ns), element.text,
                                     f"Consumer must use the literal README {artifact} {element.tag}")
                if artifact == "traceai-java-weaviate":
                    self.assertEqual(dep.findtext("version"), local_version,
                                     "README must select the locally installed checkout, not an older Central JAR")


if __name__ == "__main__":
    unittest.main()
