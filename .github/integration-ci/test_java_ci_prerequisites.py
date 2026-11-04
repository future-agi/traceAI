"""Keep both native Java integration prerequisites when merging concurrent CI changes."""
import re
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

    def test_arconia_and_anthropic_wrapper_keep_independent_jobs(self):
        workflow = (WORKFLOWS / 'java-ci.yml').read_text()
        # Scope each assertion to its job: a run step or report accidentally
        # assigned to the adjacent job must not satisfy the contract.
        contracts = {
            'arconia-privacy': (
                "arconia: ['0.30.0', '0.32.0']",
                "java-version: '21'",
                'mvn -B -ntp -f java/pom.xml -pl traceai-java-core -am install',
                'mvn -B -ntp -f java/examples/arconia-example/pom.xml',
                '-Darconia.version=${{ matrix.arconia }} test',
                'name: arconia-${{ matrix.arconia }}-test-reports',
                'path: java/examples/arconia-example/target/surefire-reports/',
            ),
            'anthropic-wrapper': (
                "anthropic: ['0.1.0', '3.4.0']",
                "java-version: '17'",
                'mvn -B -ntp -f java/pom.xml -pl traceai-java-anthropic -am '
                '-Danthropic-java.version=${{ matrix.anthropic }} verify',
                'name: anthropic-wrapper-${{ matrix.anthropic }}',
                'path: java/traceai-java-anthropic/target/surefire-reports/',
            ),
        }
        for name, required in contracts.items():
            with self.subTest(job=name):
                blocks = re.findall(
                    rf'^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:|\Z)',
                    workflow, re.MULTILINE | re.DOTALL,
                )
                self.assertEqual(len(blocks), 1, f'{name} must appear exactly once')
                for snippet in ('uses: actions/checkout@', 'uses: actions/setup-java@',
                                'uses: actions/upload-artifact@', 'if: always()', *required):
                    self.assertIn(snippet, blocks[0])


if __name__ == '__main__':
    unittest.main()
