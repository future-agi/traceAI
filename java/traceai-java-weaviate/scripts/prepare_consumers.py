#!/usr/bin/env python3
"""Create standalone consumers of the installed JAR, not reactor class directories."""
import argparse
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--client-version", default="6.3.2")
    args = parser.parse_args()
    module = Path(__file__).resolve().parents[1]
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    version = ET.parse(module / "pom.xml").findtext("m:parent/m:version", namespaces=ns)
    for name, sdk, sdk_version in [("v6", "client6", args.client_version), ("legacy", "client", "5.5.0")]:
        dest = args.output.resolve() / name
        dest.mkdir(parents=True, exist_ok=False)
        test = dest / "src/test/java/ai/traceai/weaviate"
        test.mkdir(parents=True)
        if name == "v6":
            main_src = dest / "src/main/java"
            main_src.mkdir(parents=True)
            snippets = re.findall(r"```java\n(.*?)\n```", (module / "README.md").read_text(), re.S)
            example = next(s for s in snippets if "public class WeaviateExample" in s)
            (main_src / "WeaviateExample.java").write_text(example + "\n")
            shutil.copy(module / "src/test/java/ai/traceai/weaviate/WeaviateV6Fixture.java", test)
            shutil.copy(module / "src/consumer-test/WeaviateReadmeConsumerTest.java", test)
        else:
            shutil.copy(module / "src/consumer-test/WeaviateLegacyConsumerTest.java", test)
        dependencies = [
            ("com.futureagi", "traceai-java-weaviate", version, "compile"),
            ("io.weaviate", sdk, sdk_version, "compile"),
            ("org.junit.jupiter", "junit-jupiter", "5.10.1", "test"),
            ("org.assertj", "assertj-core", "3.24.2", "test"),
            ("io.opentelemetry", "opentelemetry-sdk-testing", "1.35.0", "test"),
        ]
        if name == "v6":
            dependencies.append(("io.opentelemetry.proto", "opentelemetry-proto", "1.3.2-alpha", "test"))
        project = ET.Element("project", {"xmlns": ns["m"]})
        for tag, value in [("modelVersion", "4.0.0"), ("groupId", "local.test"), ("artifactId", "weaviate-" + name), ("version", "1")]:
            ET.SubElement(project, tag).text = value
        props = ET.SubElement(project, "properties")
        ET.SubElement(props, "maven.compiler.release").text = "17"
        deps = ET.SubElement(project, "dependencies")
        for group, artifact, ver, scope in dependencies:
            dep = ET.SubElement(deps, "dependency")
            for tag, value in [("groupId", group), ("artifactId", artifact), ("version", ver), ("scope", scope)]:
                ET.SubElement(dep, tag).text = value
        plugins = ET.SubElement(ET.SubElement(project, "build"), "plugins")
        for artifact, ver in [("maven-compiler-plugin", "3.12.1"), ("maven-surefire-plugin", "3.2.3")]:
            plugin = ET.SubElement(plugins, "plugin")
            for tag, value in [("groupId", "org.apache.maven.plugins"), ("artifactId", artifact), ("version", ver)]:
                ET.SubElement(plugin, tag).text = value
        ET.indent(project)
        ET.ElementTree(project).write(dest / "pom.xml", encoding="unicode", xml_declaration=True)
        print(dest / "pom.xml")


if __name__ == "__main__":
    main()
