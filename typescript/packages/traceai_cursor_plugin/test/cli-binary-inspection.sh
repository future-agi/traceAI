#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work/evidence /work/downloads/cursor-installer /work/isolated-home
export HOME=/work/isolated-home
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"
export XDG_CACHE_HOME="$HOME/.cache"
unset CURSOR_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY FI_API_KEY FI_SECRET_KEY
curl --fail --silent --show-error --location https://cursor.com/install --output /work/downloads/cursor-installer/install.sh
sha256sum /work/downloads/cursor-installer/install.sh > /work/evidence/installer-sha256.txt
bash /work/downloads/cursor-installer/install.sh > /work/evidence/install.txt 2>&1
export PATH="$HOME/.local/bin:$PATH"
command -v agent > /work/evidence/path.txt
readlink -f "$(command -v agent)" >> /work/evidence/path.txt
# No model generation/authentication command is invoked. No credentials enter the sandbox.
timeout 30 agent --version | tee /work/evidence/version.txt
timeout 30 agent --help > /work/evidence/help.txt
printf 'Installation/version/help only; no authenticated prompt, no model calls, no OTLP spans.\n' > /work/evidence/scope.txt
cat /work/evidence/path.txt /work/evidence/version.txt /work/evidence/scope.txt
