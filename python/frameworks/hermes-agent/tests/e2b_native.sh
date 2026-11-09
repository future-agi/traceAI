#!/usr/bin/env bash
# Run only in the disposable E2B toolchain sandbox. All inputs are under /work.
set -euo pipefail
[ "$(uname -s)" = Linux ] && [ -d /work/typescript ]
command -v iptables >/dev/null || { apt-get update -qq; apt-get install -y -qq iptables; }
uv venv /work/testenv
uv pip install --python /work/testenv/bin/python /work/python/frameworks/hermes-agent pytest pexpect
export HERMES_RESOLVER_PYTHON=/work/testenv/bin/python
cd /work/typescript
pnpm install --frozen-lockfile --ignore-scripts >/work/pnpm.log 2>&1
pnpm --filter @traceai/fi-semantic-conventions --filter @traceai/fi-core --filter @traceai/agent-core --filter @traceai/hermes-agent --filter @traceai/agents -r run build
pnpm --filter @traceai/agent-core --filter @traceai/hermes-agent --filter @traceai/agents -r run test
mkdir -p /work/tarballs /work/consumer
for package in fi-semantic-conventions fi-core traceai_agent_core traceai_hermes_agent traceai_agents; do
  (cd "/work/typescript/packages/$package" && pnpm pack --pack-destination /work/tarballs)
done
(cd /work/consumer && npm install --ignore-scripts --cache /work/empty-npm-cache /work/tarballs/*.tgz)
cd /work
/work/testenv/bin/python -m pytest python/frameworks/hermes-agent/tests -q -s
python -I .github/release/release_plan.py --check
python -m unittest discover -s .github/release -p test_npm_release.py
uv build --wheel --out-dir /work/wheels /work/python/frameworks/hermes-agent
mkdir /work/native-origin
cp -R /work/python /work/native-origin/
git -C /work/native-origin init -q
git -C /work/native-origin add .
git -C /work/native-origin -c user.name=Test -c user.email=test@example.invalid commit -qm fixture
git init -q /work/host
git -C /work/host remote add origin https://github.com/NousResearch/hermes-agent.git
git -C /work/host fetch --quiet --depth 1 origin 90d6b98fd86d20981ee06ddd5b7d93e4320f26a4
git -C /work/host checkout --quiet --detach FETCH_HEAD
uv python install 3.14
(cd /work/host && uv sync --python 3.14 --no-dev >/work/host-install.log 2>&1)
/work/testenv/bin/python -I /work/python/frameworks/hermes-agent/tests/e2b_native.py
