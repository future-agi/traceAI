#!/usr/bin/env bash
# Run after building fi-core, semantic conventions and this package.
# Every model request is handled by real_binary.mjs's loopback provider.
set -euo pipefail
cd "$(dirname "$0")/.."
pinned=$(node -p 'require("./package.json").devDependencies["@openai/codex-sdk"]')
floor=$(node -p 'require("./package.json").peerDependencies["@openai/codex-sdk"].match(/^>=([0-9.]+) <1$/)[1]')
sdk_link=node_modules/@openai/codex-sdk
# The workspace uses pnpm. Refuse to replace a real directory or mutate its lock.
test -L "$sdk_link"
original=$(readlink "$sdk_link")
tmp=$(mktemp -d)
cleanup() {
  rm -f "$sdk_link"
  ln -s "$original" "$sdk_link"
  rm -rf "$tmp"
}
trap cleanup EXIT

for version in "$pinned" "$floor"; do
  if [ "$version" != "$pinned" ]; then
    npm install --prefix "$tmp" --ignore-scripts --no-audit --no-fund "@openai/codex-sdk@$version"
    rm "$sdk_link"
    ln -s "$tmp/node_modules/@openai/codex-sdk" "$sdk_link"
  fi
  echo "=== Codex SDK $version ==="
  CODEX_SDK_VERSION="$version" pnpm test --runInBand
  node contract/run_real_sdk.mjs
  node contract/otlp_wire.mjs
  node contract/real_binary.mjs
  node --expose-gc contract/weak_lifecycle.mjs
done
