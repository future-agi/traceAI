#!/usr/bin/env bash
# Exact Maven toolchain, verified before extracting; no third-party setup action.
set -euo pipefail
: "${RUNNER_TEMP:?}"
: "${GITHUB_PATH:?}"
archive="$RUNNER_TEMP/apache-maven-3.9.9-bin.tar.gz"
curl --fail --show-error --location --retry 3 --max-time 120 \
  https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.tar.gz \
  --output "$archive"
printf '%s  %s\n' \
  a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b \
  "$archive" | sha512sum --check --strict
tar -xzf "$archive" -C "$RUNNER_TEMP"
printf '%s\n' "$RUNNER_TEMP/apache-maven-3.9.9/bin" >> "$GITHUB_PATH"
