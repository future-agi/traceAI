#!/usr/bin/env python3
"""Prepare installed, flattened POMs for JitPack's artifact harvesting only.

JitPack rewrites direct dependencies but not dependencyManagement. Explicitly
remap all internal dependency references, including the BOM, to this build's
commit/tag. Top-level coordinates and filenames stay unchanged for harvesting.
This never changes source POMs, JARs, or Central publishing configuration.
"""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS_URI = "http://maven.apache.org/POM/4.0.0"
NS = {"m": NS_URI}
ET.register_namespace("", NS_URI)
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")


def held_artifact_ids():
    """artifactIds that the release builds but does not publish (hold_artifacts); JitPack leaves them out as well."""
    try:
        hold = json.loads((ROOT.parent / ".github/release/config.json").read_text()).get("hold_artifacts", {})
    except FileNotFoundError:
        return set()
    return {name.split(":", 1)[1] for name in hold if name.startswith("com.futureagi:")}


def prepare(local_repo, group, version, held=None):
    if not group or not version or "${" in group + version:
        raise ValueError("JitPack group and version must be nonempty resolved values")
    held = held_artifact_ids() if held is None else set(held)
    source = ET.parse(ROOT / "pom.xml").getroot()
    source_version = source.findtext("m:version", namespaces=NS)
    artifacts = {source.findtext("m:artifactId", namespaces=NS)}
    for module in source.findall("m:modules/m:module", NS):
        pom = ET.parse(ROOT / module.text.strip() / "pom.xml").getroot()
        artifacts.add(pom.findtext("m:artifactId", namespaces=NS))
    # A held module is not installed on this route (its jitpack profile skips the install), so there is
    # nothing of it to harvest, and no harvested POM may depend on it.
    harvested = artifacts - held
    # Validate the complete set before writing anything. In particular, refuse
    # normal/Central POMs: this command requires the isolated jitpack profile.
    prepared = []
    for artifact in sorted(harvested):
        path = local_repo / "com/futureagi" / artifact / source_version / "{}-{}.pom".format(artifact, source_version)
        tree = ET.parse(path)
        pom = tree.getroot()
        if pom.find("m:parent", NS) is not None or "${" in path.read_text():
            raise ValueError("not a self-contained JitPack POM: {}".format(path))
        for dep in pom.findall(".//m:dependency", NS):
            dep_group = dep.find("m:groupId", NS)
            if dep_group is not None and dep_group.text in {"com.futureagi", group}:
                dep_version = dep.find("m:version", NS)
                if (dep.findtext("m:artifactId", namespaces=NS) not in harvested or
                        dep_version is None or dep_version.text not in {source_version, version}):
                    raise ValueError("unaligned internal dependency in {}".format(path))
                dep_group.text = group
                dep_version.text = version
        prepared.append((path, tree))
    for path, tree in prepared:
        tree.write(path, encoding="UTF-8", xml_declaration=True)
    print("Prepared {} self-contained POMs for {}:*:{}".format(len(prepared), group, version))
    return len(prepared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="JitPack's VERSION, not the Maven release version")
    parser.add_argument("--group", default="com.github.future-agi.traceAI", help="JitPack's GROUP.ARTIFACT")
    parser.add_argument("--local-repo", type=Path, default=Path.home() / ".m2/repository")
    args = parser.parse_args()
    prepare(args.local_repo.resolve(), args.group, args.version)


if __name__ == "__main__":
    main()
