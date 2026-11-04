# Real-SDK contract checks

Not part of `pnpm test` (they need the Pi SDK, a model endpoint and a collector).

`sdk_types.ts` fails to compile if `@earendil-works/pi-coding-agent`'s
`AgentSession` / `createAgentSession` stop fitting the structural types this
package accepts. `real_sdk_session.mjs` runs one real session through the
instrumentation and exports it with `@traceai/fi-core`.

```bash
mkdir /tmp/pi-contract && cd /tmp/pi-contract && npm init -y && npm pkg set type=module
npm install @earendil-works/pi-coding-agent@1.2.0 typescript@5 \
  <path>/fi-core.tgz <path>/fi-semantic-conventions.tgz <path>/traceai-pi-coding-agent.tgz   # pnpm pack each
cp <this dir>/sdk_types.ts . && npx tsc --noEmit --strict --skipLibCheck \
  --module NodeNext --moduleResolution NodeNext --target ES2022 sdk_types.ts

# models.json in $PI_CODING_AGENT_DIR defines <provider>/<model>
# FI_BASE_URL only for a self-hosted Future AGI (default: https://api.futureagi.com)
FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=http://localhost:4318 FI_PROJECT_NAME=pi-sdk-contract \
PI_CODING_AGENT_DIR=/tmp/pi-agent PI_E2E_CWD=/tmp/pi-project \
  node <this dir>/real_sdk_session.mjs <provider> <model> "List the files here and read README.md"
```
