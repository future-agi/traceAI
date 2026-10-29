#!/usr/bin/env python3
"""Validate every reactor effective POM and (optionally) release attachments.

First run from java/:
  mvn -B -ntp -P release help:effective-pom -Doutput=target/central-effective-pom.xml
  python3 -I scripts/check_central.py --artifacts

No network requests, signing, or deployment are performed by this checker.
"""
import argparse
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

NS = {"m": "http://maven.apache.org/POM/4.0.0"}
ROOT = Path(__file__).resolve().parents[1]


def text(element, path):
    return element.findtext("/".join("m:" + part for part in path.split("/")), "", NS).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effective-pom", type=Path, default=ROOT / "target/central-effective-pom.xml")
    parser.add_argument("--artifacts", action="store_true", help="also check binary, source, and Javadoc JARs")
    args = parser.parse_args()
    parent = ET.parse(ROOT / "pom.xml").getroot()
    version = text(parent, "version")
    expected = {text(parent, "artifactId"): ROOT}
    errors = []
    for module in parent.findall("m:modules/m:module", NS):
        directory = ROOT / module.text.strip()
        pom = ET.parse(directory / "pom.xml").getroot()
        artifact = text(pom, "artifactId")
        expected[artifact] = directory
        if text(pom, "version") or text(pom, "groupId"):
            errors.append(f"{artifact}: must inherit its version and groupId")
        if text(pom, "parent/groupId") != "com.futureagi" or text(pom, "parent/version") != version:
            errors.append(f"{artifact}: parent coordinates differ from java/pom.xml")

    document = ET.parse(args.effective_pom).getroot()
    projects = [document] if document.tag.endswith("}project") else document.findall("m:project", NS)
    found = set()
    for project in projects:
        artifact = text(project, "artifactId")
        found.add(artifact)
        if artifact not in expected:
            errors.append(f"unexpected effective POM: {artifact}")
            continue
        for field in ("groupId", "artifactId", "version", "name", "description", "url",
                      "licenses/license/name", "licenses/license/url", "developers/developer/name",
                      "scm/connection", "scm/developerConnection", "scm/url"):
            value = text(project, field)
            if not value or "${" in value:
                errors.append(f"{artifact}: missing or unresolved {field}")
        if text(project, "groupId") != "com.futureagi" or text(project, "version") != version:
            errors.append(f"{artifact}: version/groupId differ from parent")
        if text(project, "licenses/license/name") != "Apache License, Version 2.0":
            errors.append(f"{artifact}: expected Apache-2.0 license")
        for dep in project.findall(".//m:dependency", NS):
            if text(dep, "groupId") == "com.futureagi" and text(dep, "version") != version:
                errors.append(f"{artifact}: internal dependency {text(dep, 'artifactId')} is not aligned")
        plugins = {text(p, "artifactId"): p for p in project.findall("m:build/m:plugins/m:plugin", NS)}
        for name in ("maven-source-plugin", "maven-javadoc-plugin", "maven-gpg-plugin",
                     "central-publishing-maven-plugin"):
            if name not in plugins:
                errors.append(f"{artifact}: release plugin missing: {name}")
        central = plugins.get("central-publishing-maven-plugin")
        if central is not None:
            for key, value in (("publishingServerId", "central"), ("autoPublish", "true"), ("waitUntil", "published")):
                if text(central, "configuration/" + key) != value:
                    errors.append(f"{artifact}: incorrect Central configuration: {key}")
        if args.artifacts and text(project, "packaging") != "pom":
            for suffix in ("", "-sources", "-javadoc"):
                jar = expected[artifact] / "target" / f"{artifact}-{version}{suffix}.jar"
                if not jar.is_file():
                    errors.append(f"missing release attachment: {jar.relative_to(ROOT)}")
                    continue
                with zipfile.ZipFile(jar) as archive:
                    if not archive.namelist() or archive.testzip() is not None:
                        errors.append(f"invalid release attachment: {jar.relative_to(ROOT)}")
        print(f"Checked com.futureagi:{artifact}:{version}")
    if found != set(expected):
        errors.append(f"reactor mismatch: missing={sorted(set(expected) - found)}, extra={sorted(found - set(expected))}")
    # The BOM must align every instrumentation module, not just a hand-picked subset.
    bom = ET.parse(ROOT / "traceai-bom/pom.xml").getroot()
    bom_artifacts = {text(d, "artifactId") for d in bom.findall("m:dependencyManagement/m:dependencies/m:dependency", NS)}
    if bom_artifacts != set(expected) - {"traceai-java-parent", "traceai-bom"}:
        errors.append("BOM does not cover exactly the SDK modules")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"PASS: {len(found)} effective POMs meet Central metadata and version checks" +
          ("; all release JARs exist" if args.artifacts else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
