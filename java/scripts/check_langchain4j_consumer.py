#!/usr/bin/env python3
"""Ensure the external LangChain4j test consumes this build, not a stale SDK jar."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

java = Path(__file__).resolve().parents[1]
ns = {"m": "http://maven.apache.org/POM/4.0.0"}
parent = ET.parse(java / "pom.xml").getroot()
consumer = ET.parse(java / "compat-tests/langchain4j/pom.xml").getroot()
version = parent.findtext("m:version", namespaces=ns)
consumer_version = consumer.findtext("m:parent/m:version", namespaces=ns)
if consumer_version != version:
    print(f"LangChain4j consumer targets SDK {consumer_version}, but reactor builds {version}", file=sys.stderr)
    sys.exit(1)
print(f"LangChain4j consumer targets the current SDK version {version}")
