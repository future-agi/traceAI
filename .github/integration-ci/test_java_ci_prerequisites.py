"""Keep both native Java integration prerequisites when merging concurrent CI changes."""
import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / 'workflows'


class JavaPrerequisiteTests(unittest.TestCase):
    def test_arconia_and_quarkus_each_install_their_matching_local_artifact(self):
        workflow = (WORKFLOWS / 'integration-tests.yml').read_text()
        self.assertIn("if: matrix.package.name == 'arconia'\n"
                      "        run: mvn -B -ntp -f java/pom.xml -pl traceai-java-core -am install", workflow)
        self.assertIn("if: matrix.package.name == 'quarkus-langchain4j'\n"
                      "        run: mvn -B -ntp -f java/pom.xml -pl traceai-java-quarkus-langchain4j -am -DskipTests install", workflow)

    def test_native_arconia_floor_and_latest_stay_in_java_ci(self):
        workflow = (WORKFLOWS / 'java-ci.yml').read_text()
        self.assertIn("arconia: ['0.30.0', '0.32.0']", workflow)
        self.assertIn('mvn -B -ntp -f java/examples/arconia-example/pom.xml', workflow)


if __name__ == '__main__':
    unittest.main()
