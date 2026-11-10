#!/usr/bin/env python3
"""Compile external Maven consumers; never execute provider calls or publish.

After `mvn install`, run `python3 -I scripts/test_consumers.py`.
After `mvn -P jitpack -DskipTests install`, also pass --check-jitpack-poms.
To test a pushed commit using only public JitPack artifacts, pass
--jitpack-version <sha>; by default this uses a new, empty Maven repository.
Use --core-only to compile just the README ownership snippet (BOM and direct).
Use --central to resolve the advertised README version from a fresh repository,
without source-installed artifacts. This intentionally fails while the README's
ownership API is unreleased; it is a publication check, not a source-build check.
The generated projects live outside the reactor and do not inherit its POM.
"""
import argparse
import json
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
TEST_LIBRARIES = [
    ("io.opentelemetry", "opentelemetry-sdk-testing"),
    ("org.junit.jupiter", "junit-jupiter"),
    ("org.assertj", "assertj-core"),
    ("org.mockito", "mockito-core"),
    ("org.mockito", "mockito-junit-jupiter"),
]


def check_jitpack_poms(version):
    parent = ET.parse(ROOT / "pom.xml").getroot()
    directories = [ROOT] + [ROOT / m.text.strip() for m in parent.findall("m:modules/m:module", NS)]
    for directory in directories:
        path = directory / "target/jitpack-pom.xml"
        pom = ET.parse(path).getroot()
        assert pom.find("m:parent", NS) is None, f"unresolvable JitPack parent: {path}"
        assert "${" not in path.read_text(), f"unresolved expression: {path}"
        assert pom.findtext("m:groupId", namespaces=NS) == "com.futureagi", path
        assert pom.findtext("m:version", namespaces=NS) == version, path
        for dep in pom.findall(".//m:dependency", NS):
            if dep.findtext("m:groupId", namespaces=NS) == "com.futureagi":
                assert dep.findtext("m:version", namespaces=NS) == version, path
        if directory.name == "traceai-bom":
            managed = pom.findall("m:dependencyManagement/m:dependencies/m:dependency", NS)
            artifacts = {d.findtext("m:artifactId", namespaces=NS) for d in managed
                         if d.findtext("m:groupId", namespaces=NS) == "com.futureagi"}
            expected = {d.name for d in directories[1:] if d.name != "traceai-bom"}
            assert artifacts == expected, "flattened BOM lost SDK version alignment"
            for dep in managed:
                assert dep.findtext("m:scope", "", NS) != "test", "BOM leaks test scope"
    # JitPack does not remap dependencyManagement itself. Exercise our final
    # harvesting step on copies and verify both the BOM and direct dependencies.
    prepare = runpy.run_path(str(ROOT / "scripts/prepare_jitpack.py"))["prepare"]
    with tempfile.TemporaryDirectory(prefix="traceai-jitpack-poms-") as temp:
        repo = Path(temp)
        staged = []
        for directory in directories:
            artifact = ET.parse(directory / "pom.xml").getroot().findtext("m:artifactId", namespaces=NS)
            path = repo / "com/futureagi" / artifact / version / f"{artifact}-{version}.pom"
            path.parent.mkdir(parents=True)
            path.write_bytes((directory / "target/jitpack-pom.xml").read_bytes())
            staged.append(path)
        group, commit = "com.github.future-agi.traceAI", "consumer-regression-sha"
        assert prepare(repo, group, commit) == len(directories)
        for path in staged:
            pom = ET.parse(path).getroot()
            assert pom.findtext("m:groupId", namespaces=NS) == "com.futureagi", path
            assert pom.findtext("m:version", namespaces=NS) == version, path
            for dep in pom.findall(".//m:dependency", NS):
                dep_group = dep.findtext("m:groupId", namespaces=NS)
                assert dep_group != "com.futureagi", f"unmapped internal dependency: {path}"
                if dep_group == group:
                    assert dep.findtext("m:version", namespaces=NS) == commit, path
        # Refuse non-flattened Central POMs without partially changing any file.
        child = repo / "com/futureagi/traceai-bom" / version / f"traceai-bom-{version}.pom"
        child.write_bytes((ROOT / "traceai-bom/pom.xml").read_bytes())
        before = {path: path.read_bytes() for path in staged}
        try:
            prepare(repo, group, commit)
        except ValueError:
            pass
        else:
            raise AssertionError("JitPack preparation accepted an original Central POM")
        assert all(path.read_bytes() == content for path, content in before.items())
    return len(directories)


def dependency(group, artifact, version=None):
    version_xml = f"<version>{escape(version)}</version>" if version else ""
    return (f"<dependency><groupId>{escape(group)}</groupId>"
            f"<artifactId>{escape(artifact)}</artifactId>{version_xml}</dependency>")


def readme_version(readme, reactor_version):
    """Use advertised coordinates, never silently substitute the reactor version."""
    patterns = {
        "Maven BOM": r"<artifactId>traceai-bom</artifactId>\s*<version>([^<]+)</version>",
        "Gradle BOM": r"com\.futureagi:traceai-bom:([^']+)'",
        "direct core": r"<artifactId>traceai-java-core</artifactId>\s*<version>([^<]+)</version>",
        "without BOM": r"Without the BOM, specify `<version>([^<]+)</version>`",
        "install version": r"The install examples track Java SDK `([^`]+)`",
    }
    versions = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, readme)
        if not match:
            raise ValueError(f"README is missing {name} coordinates")
        versions[name] = match.group(1)
    if set(versions.values()) != {reactor_version}:
        raise ValueError(f"README coordinates {versions} do not match reactor {reactor_version}")
    return versions["Maven BOM"]


def readme_consumer(readme, heading):
    section = readme.split(heading, 1)[1]
    snippet = section.split("```java\n", 1)[1].split("```", 1)[0]
    imports, statements = [], []
    for line in snippet.splitlines():
        (imports if line.startswith("import ") else statements).append(line)
    return ("\n".join(imports)
            + "\npublic class Consumer { public static void main(String[] args) {\n"
            + "\n".join(statements) + "\n}}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jitpack-version", help="resolve this pushed SHA from public JitPack")
    parser.add_argument("--local-repo", type=Path, help="Maven repository; fresh by default for JitPack")
    parser.add_argument("--work-dir", type=Path, help="parent directory for retained consumer projects/logs")
    parser.add_argument("--check-jitpack-poms", action="store_true")
    parser.add_argument("--core-only", action="store_true", help="only compile the ownership example")
    parser.add_argument("--central", action="store_true", help="resolve README coordinates from Central, not a source install")
    args = parser.parse_args()
    if args.central and (args.jitpack_version or args.local_repo or args.check_jitpack_poms):
        parser.error("--central requires a fresh repository and cannot use JitPack or --local-repo")
    version = ET.parse(ROOT / "pom.xml").getroot().findtext("m:version", namespaces=NS)
    readme = (ROOT / "README.md").read_text()
    advertised_version = readme_version(readme, version)
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="traceai-consumers-", dir=args.work_dir))
    local_repo = args.local_repo.resolve() if args.local_repo else (
        work / "m2" if args.jitpack_version or args.central else None)
    group = "com.github.future-agi.traceAI" if args.jitpack_version else "com.futureagi"
    resolved_version = args.jitpack_version or advertised_version
    if args.central:
        # Ignore user/global mirrors and repositories as well as source-installed JARs.
        settings = work / "central-settings.xml"
        settings.write_text('<settings><mirrors><mirror><id>central-only</id><mirrorOf>*</mirrorOf>'
                            '<url>https://repo.maven.apache.org/maven2</url></mirror></mirrors></settings>')
    repositories = ("<repositories><repository><id>jitpack.io</id>"
                    "<url>https://jitpack.io</url></repository></repositories>" if args.jitpack_version else "")
    # Compile the actual README examples, not independently maintained copies.
    quickstart = readme_consumer(readme, "## Send your first trace")
    ownership = readme_consumer(readme, "### Provider ownership and shutdown")
    openai = dependency(group, "traceai-java-openai") + dependency("com.openai", "openai-java", "0.8.0")
    cases = [
        ("readme-openai", openai, quickstart),
        ("readme-spring-openai", openai + dependency(group, "traceai-spring-boot-starter"), quickstart),
        ("bom-main-scope", "".join(dependency(g, a) for g, a in TEST_LIBRARIES), textwrap.dedent("""\
            import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
            import org.junit.jupiter.api.Assertions;
            import org.mockito.junit.jupiter.MockitoExtension;
            public class Consumer {
                public static void main(String[] args) {
                    InMemorySpanExporter exporter = InMemorySpanExporter.create();
                    Assertions.assertNotNull(exporter);
                    org.assertj.core.api.Assertions.assertThat(exporter).isNotNull();
                    Runnable mock = org.mockito.Mockito.mock(Runnable.class);
                    Class<?> extension = MockitoExtension.class;
                    exporter.close();
                }
            }
            """)),
    ]
    core_cases = [
        ("readme-ownership-bom", dependency(group, "traceai-java-core"), ownership),
        ("readme-ownership-direct", dependency(group, "traceai-java-core", resolved_version), ownership),
    ]
    cases = core_cases if args.core_only else cases + core_cases
    results = []
    for name, dependencies, source in cases:
        project = work / name
        java = project / "src/main/java/Consumer.java"
        java.parent.mkdir(parents=True)
        java.write_text(source)
        management = "" if name == "readme-ownership-direct" else f"""
          <dependencyManagement><dependencies>
            <dependency><groupId>{escape(group)}</groupId><artifactId>traceai-bom</artifactId>
              <version>{escape(resolved_version)}</version><type>pom</type><scope>import</scope>
            </dependency>
          </dependencies></dependencyManagement>"""
        pom = f"""<project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion><groupId>example</groupId>
          <artifactId>{name}</artifactId><version>1</version>
          {repositories}
          {management}
          <dependencies>{dependencies}</dependencies>
          <build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
            <artifactId>maven-compiler-plugin</artifactId><version>3.12.1</version>
            <configuration><release>17</release></configuration>
          </plugin></plugins></build>
        </project>"""
        (project / "pom.xml").write_text(pom)
        command = ["mvn", "-B", "-ntp", "-f", str(project / "pom.xml")]
        if local_repo:
            command.append(f"-Dmaven.repo.local={local_repo}")
        if args.central:
            command.extend(["--settings", str(settings), "--global-settings", str(settings)])
        command.append("compile")
        log = project / "build.log"
        with log.open("w") as output:
            result = subprocess.run(command, cwd=project, stdout=output, stderr=subprocess.STDOUT)
        results.append({"case": name, "exit_code": result.returncode, "log": str(log)})
        print(f"{name}: {'PASS' if result.returncode == 0 else 'FAIL'} ({log})", flush=True)
    flattened = check_jitpack_poms(version) if args.check_jitpack_poms else None
    report = {"group": group, "version": resolved_version, "local_repo": str(local_repo) if local_repo else "default",
              "flattened_poms_checked": flattened, "consumers": results}
    (work / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return int(any(r["exit_code"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
