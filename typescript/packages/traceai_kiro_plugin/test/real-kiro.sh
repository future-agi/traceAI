#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/vendor /work/evidence
curl -fsSL https://prod.download.cli.kiro.dev/stable/3.0.0/kirocli-x86_64-linux.zip -o /work/vendor/kiro.zip
sha256sum /work/vendor/kiro.zip > /work/evidence/kiro-sha256.txt
unzip -q /work/vendor/kiro.zip -d /work/vendor/unpacked
bin=$(find /work/vendor/unpacked -type f -name kiro-cli | head -1)
chmod +x "$bin"
export PATH="$(dirname "$bin"):$PATH"
export KIRO_TEST_BIN="$bin"
node /work/typescript/packages/traceai_kiro_plugin/test/real-kiro.cjs
