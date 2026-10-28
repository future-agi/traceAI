# Version compatibility

Declared support, configured tests and observed execution are different facts. A dependency range is not proof that every admitted version passed.

> **SYNTHETIC FIXTURE PREVIEW — not customer compatibility evidence.**

Generated: 2026-10-28T12:00:00Z. Exact environments and original results are retained in the `compatibility-results` and `compatibility-report` Actions artifacts.

## Exact observed combinations

Only **published / passed** rows identify tested installable releases. Source rows are unreleased builds. Pilot-pending or unqualified targets remain so even after an individual pass. Stale, blocked, flaky and quarantined are not green. No live-provider behavior is certified.

### python-openai — published / passed

- Qualification: **pilot_pending**; last checked: **2026-10-28T05:00:00Z**; [evidence](https://github.com/future-agi/traceAI/actions/runs/1007)
- Runtime/OS: `{"arch": "x86_64", "implementation": "cpython", "os": "linux", "version": "3.11.9"}`; selector: `latest_eligible`.
- Source SHA: `64e3a57ad273559849bbab0b8125bc57c7e7592e`; contract: `21d1570285a666fb43bd561849296f26a21e61d01b3d5a93235f7cd3f0884b95`.
- Requested selection: `openai==1.93.1` (upstream), `traceAI-openai==0.1.10` (instrumentation), `fi-instrumentation-otel==1.1.0` (core)
- Exact resolved packages: `fi-instrumentation-otel==1.1.0` (core), `traceAI-openai==0.1.10` (instrumentation), `openai==1.93.1` (upstream)
- Features exercised: real-sdk, sync, async, streaming, error-propagation, span-lifecycle, privacy.
- Complete environment lock: `{"format": "pip-freeze", "path": "locks/fixture.txt", "sha256": "85e4a7035dcef823773c5d8fa660e32ce82ed0df2ded129676120d0af9cfa154"}`.
- Discovery (absolute latest / eligible after 72-hour cooldown / actually resolved): openai: 1.94.0 / 1.93.1 / 1.93.1

Exact tested install (retain the full lock from the evidence for transitive reproducibility):

```sh
python -m pip install 'fi-instrumentation-otel==1.1.0' 'traceAI-openai==0.1.10' 'openai==1.93.1'
```

### python-openai — published / passed

- Qualification: **pilot_pending**; last checked: **2026-10-26T04:00:00Z**; [evidence](https://github.com/future-agi/traceAI/actions/runs/1001)
- Runtime/OS: `{"arch": "x86_64", "implementation": "cpython", "os": "linux", "version": "3.11.9"}`; selector: `latest_eligible`.
- Source SHA: `64e3a57ad273559849bbab0b8125bc57c7e7592e`; contract: `21d1570285a666fb43bd561849296f26a21e61d01b3d5a93235f7cd3f0884b95`.
- Requested selection: `openai==1.92.0` (upstream), `traceAI-openai==0.1.10` (instrumentation), `fi-instrumentation-otel==1.1.0` (core)
- Exact resolved packages: `fi-instrumentation-otel==1.1.0` (core), `traceAI-openai==0.1.10` (instrumentation), `openai==1.92.0` (upstream)
- Features exercised: real-sdk, sync, async, streaming, error-propagation, span-lifecycle, privacy.
- Complete environment lock: `{"format": "pip-freeze", "path": "locks/fixture.txt", "sha256": "85e4a7035dcef823773c5d8fa660e32ce82ed0df2ded129676120d0af9cfa154"}`.
- Discovery (absolute latest / eligible after 72-hour cooldown / actually resolved): openai: 1.94.0 / 1.92.0 / 1.92.0

Exact tested install (retain the full lock from the evidence for transitive reproducibility):

```sh
python -m pip install 'fi-instrumentation-otel==1.1.0' 'traceAI-openai==0.1.10' 'openai==1.92.0'
```

### python-openai — source / passed

- Qualification: **pilot_pending**; last checked: **2026-10-27T07:00:00Z**; [evidence](https://github.com/future-agi/traceAI/actions/runs/1005)
- Runtime/OS: `{"arch": "x86_64", "implementation": "cpython", "os": "linux", "version": "3.11.9"}`; selector: `latest_eligible`.
- Source SHA: `64e3a57ad273559849bbab0b8125bc57c7e7592e`; contract: `21d1570285a666fb43bd561849296f26a21e61d01b3d5a93235f7cd3f0884b95`.
- Requested selection: `openai==1.93.1` (upstream), `traceAI-openai==0.1.10` (instrumentation), `fi-instrumentation-otel==1.1.0` (core)
- Exact resolved packages: `fi-instrumentation-otel==1.1.0` (core), `traceAI-openai==0.1.10` (instrumentation), `openai==1.93.1` (upstream)
- Features exercised: real-sdk, sync, async, streaming, error-propagation, span-lifecycle, privacy.
- Complete environment lock: `{"format": "pip-freeze", "path": "locks/fixture.txt", "sha256": "85e4a7035dcef823773c5d8fa660e32ce82ed0df2ded129676120d0af9cfa154"}`.
- Discovery (absolute latest / eligible after 72-hour cooldown / actually resolved): openai: 1.94.0 / 1.93.1 / 1.93.1

## Qualification inventory

This includes known-broken and unqualified gaps, shared/core packages and separately labelled example checks. A Go/Maven pin is a baseline, not an inferred minimum. Java/.NET floating qualification is deferred.

| Target | Role | Qualification | Declared upstream ranges (not all verified) | Canary |
|---|---|---|---|---|
| csharp-fiinstrumentation | core | unqualified | OpenTelemetry: 1.11.2; OpenTelemetry.Exporter.OpenTelemetryProtocol: 1.11.2; OpenTelemetry.Exporter.Console: 1.11.2 | not enabled |
| go-a2a | instrumentation | unqualified | github.com/a2aproject/a2a-go/v2: v2.5.0 | not enabled |
| go-anthropic | instrumentation | unqualified | github.com/anthropics/anthropic-sdk-go: v1.82.0 | not enabled |
| go-eino | instrumentation | unqualified | github.com/cloudwego/eino: v0.9.19; github.com/cloudwego/eino-ext/components/model/openai: v0.1.13 | not enabled |
| go-genai | instrumentation | unqualified | google.golang.org/genai: v1.71.0 | not enabled |
| go-openai | instrumentation | unqualified | github.com/openai/openai-go/v3: v3.71.1 | not enabled |
| java-arconia-example | example | unqualified | org.springframework.boot:spring-boot-starter-webmvc: undeclared; io.arconia:arconia-opentelemetry-spring-boot-starter: undeclared | not enabled |
| java-quarkus-langchain4j-example | example | unqualified | io.quarkiverse.langchain4j:quarkus-langchain4j-openai: undeclared | not enabled |
| java-traceai-java-anthropic | instrumentation | unqualified | com.anthropic:anthropic-java: 0.1.0 | not enabled |
| java-traceai-java-azure-openai | instrumentation | unqualified | com.azure:azure-ai-openai: 1.0.0-beta.16 | not enabled |
| java-traceai-java-azure-search | instrumentation | unqualified | com.azure:azure-search-documents: 11.6.2 | not enabled |
| java-traceai-java-bedrock | instrumentation | unqualified | software.amazon.awssdk:bedrockruntime: 2.25.67 | not enabled |
| java-traceai-java-chromadb | instrumentation | unqualified | io.github.amikos-tech:chromadb-java-client: 0.1.7 | not enabled |
| java-traceai-java-cohere | instrumentation | unqualified | com.cohere:cohere-java: 1.9.3 | not enabled |
| java-traceai-java-core | core | unqualified | none | not enabled |
| java-traceai-java-elasticsearch | instrumentation | unqualified | co.elastic.clients:elasticsearch-java: 8.12.0 | not enabled |
| java-traceai-java-google-genai | instrumentation | unqualified | com.google.genai:google-genai: 1.1.0 | not enabled |
| java-traceai-java-milvus | instrumentation | unqualified | io.milvus:milvus-sdk-java: 2.6.12 | not enabled |
| java-traceai-java-mongodb | instrumentation | unqualified | org.mongodb:mongodb-driver-sync: 5.2.0 | not enabled |
| java-traceai-java-ollama | instrumentation | unqualified | io.github.ollama4j:ollama4j: 1.0.79 | not enabled |
| java-traceai-java-openai | instrumentation | unqualified | com.openai:openai-java: 0.8.0 | not enabled |
| java-traceai-java-pgvector | instrumentation | unqualified | com.pgvector:pgvector: 0.1.6; org.postgresql:postgresql: 42.7.1 | not enabled |
| java-traceai-java-pinecone | instrumentation | unqualified | io.pinecone:pinecone-client: 5.1.0 | not enabled |
| java-traceai-java-qdrant | instrumentation | unqualified | io.qdrant:client: 1.16.2 | not enabled |
| java-traceai-java-redis | instrumentation | unqualified | redis.clients:jedis: 5.1.0 | not enabled |
| java-traceai-java-semantic-kernel | instrumentation | unqualified | com.microsoft.semantic-kernel:semantickernel-api: 1.3.0; com.microsoft.semantic-kernel:semantickernel-aiservices-openai: 1.3.0 | not enabled |
| java-traceai-java-vertexai | instrumentation | unqualified | com.google.cloud:google-cloud-vertexai: 1.7.0 | not enabled |
| java-traceai-java-watsonx | instrumentation | unqualified | com.ibm.watsonx:watsonx-ai: 0.12.0 | not enabled |
| java-traceai-java-weaviate | instrumentation | unqualified | io.weaviate:client: 5.5.0 | not enabled |
| java-traceai-langchain4j | instrumentation | unqualified | dev.langchain4j:langchain4j-core: 0.35.0 | not enabled |
| java-traceai-spring-ai | instrumentation | unqualified | org.springframework.ai:spring-ai-core: 1.0.0-M4; org.springframework:spring-context: 6.1.3 | not enabled |
| java-traceai-spring-boot-starter | instrumentation | unqualified | org.springframework.boot:spring-boot-autoconfigure: 3.2.1; org.springframework.boot:spring-boot-configuration-processor: 3.2.1; org.springframework.ai:spring-ai-core: 1.0.0-M4 | not enabled |
| python-a2a | instrumentation | unqualified | a2a-sdk: &gt;= 0.2.0 | not enabled |
| python-ag2 | instrumentation | unqualified | ag2: &gt;=1.0.0 | not enabled |
| python-ag2-classic | instrumentation | unqualified | autogen: &gt;=0.14.0,&lt;0.15 | not enabled |
| python-agent-framework | instrumentation | unqualified | agent-framework: &gt;=1.0.0 | not enabled |
| python-agno | instrumentation | known_broken | agno: &gt;=2.0.0 | not enabled |
| python-anthropic | instrumentation | pilot_pending | anthropic: &gt;=0.41.0 | pilot |
| python-autogen | instrumentation | known_broken | autogen-agentchat: &gt;=0.4.0 | not enabled |
| python-bedrock | instrumentation | known_broken | botocore: *; boto3: &gt;=1.38.17 | not enabled |
| python-beeai | instrumentation | unqualified | beeai-framework: &gt;=0.1.0 | not enabled |
| python-cerebras | instrumentation | known_broken | cerebras-cloud-sdk: &gt;=1.0.0 | not enabled |
| python-chromadb | instrumentation | unqualified | chromadb: &gt;=0.4.0 | not enabled |
| python-claude-agent-sdk | instrumentation | unqualified | claude-agent-sdk: &gt;= 0.1.0 | not enabled |
| python-cohere | instrumentation | known_broken | cohere: &gt;=5.0.0 | not enabled |
| python-crewai | instrumentation | unqualified | crewai: &gt;0.19.0 | not enabled |
| python-deepseek | instrumentation | known_broken | openai: &gt;=1.0.0 | not enabled |
| python-discoveryengine | instrumentation | unqualified | google-cloud-discoveryengine: &gt;=0.20.5,&lt;1 | not enabled |
| python-dspy | instrumentation | known_broken | dspy: &gt;=2.5.0 | not enabled |
| python-elevenlabs-agents | instrumentation | unqualified | elevenlabs: 2.73.0 | not enabled |
| python-exa | instrumentation | unqualified | exa-py: &gt;=2.25.0,&lt;3 | not enabled |
| python-fi-instrumentation | core | unqualified | futureagi: &gt;=0.6.0; ai-evaluation: &gt;=0.1.0 | not enabled |
| python-firecrawl | instrumentation | unqualified | firecrawl-py: &gt;=4.46.2,&lt;5 | not enabled |
| python-fireworks | instrumentation | known_broken | openai: &gt;=1.0.0 | not enabled |
| python-google-adk | instrumentation | unqualified | google-adk: &gt;= 1.2.1; google-genai: ^1.0.0 | not enabled |
| python-google-genai | instrumentation | unqualified | google-genai: &gt;=1.8.0 | not enabled |
| python-google-genai-live | instrumentation | unqualified | google-genai: &gt;=2.28,&lt;3 | not enabled |
| python-groq | instrumentation | known_broken | groq: &gt;=0.9.0 | not enabled |
| python-guardrails | instrumentation | known_broken | guardrails-ai: &gt;=0.5.0 | not enabled |
| python-haystack | instrumentation | known_broken | haystack-ai: &gt;=2.10.0 | not enabled |
| python-hermes-agent | instrumentation | unqualified | hermes-agent: undeclared | not enabled |
| python-huggingface | instrumentation | known_broken | huggingface-hub: &gt;=0.20.0 | not enabled |
| python-instructor | instrumentation | known_broken | instructor: &gt;=1.0.0 | not enabled |
| python-lancedb | instrumentation | unqualified | lancedb: &gt;=0.4.0 | not enabled |
| python-langchain | instrumentation | pilot_pending | langchain-core: &gt;=0.2.43; langgraph: &gt;=0.2.0 | pilot |
| python-litellm | instrumentation | known_broken | litellm: &gt;=1.43.0 | not enabled |
| python-livekit | instrumentation | unqualified | livekit-agents: ~1.8; livekit-plugins-openai: ~1.8; livekit-plugins-silero: ~1.8 | not enabled |
| python-llama_index | instrumentation | known_broken | llama-index-core: &gt;=0.10.43 | not enabled |
| python-mcp | instrumentation | known_broken | mcp: &gt;=1.8.1 | not enabled |
| python-mcp-use | instrumentation | unqualified | mcp-use: &gt;=1.7.1,&lt;2; langchain-core: &gt;=1.0.0,&lt;2 | not enabled |
| python-milvus | instrumentation | unqualified | pymilvus: &gt;=2.3.0 | not enabled |
| python-minimax | instrumentation | unqualified | openai: &gt;=1.0.0 | not enabled |
| python-mistralai | instrumentation | known_broken | mistralai: &gt;=1.0.2 | not enabled |
| python-mongodb-vector | instrumentation | unqualified | pymongo: &gt;=4.6.0 | not enabled |
| python-ollama | instrumentation | known_broken | ollama: &gt;=0.3.0 | not enabled |
| python-openai | instrumentation | pilot_pending | openai: &gt;=1.69.0 | pilot |
| python-openai-agents | instrumentation | known_broken | openai-agents: &gt;= 0.2.6 | not enabled |
| python-openai-realtime | instrumentation | unqualified | openai: &gt;=2.0.0,&lt;4 | not enabled |
| python-parallel | instrumentation | unqualified | parallel-web: &gt;=1.0.1,&lt;2 | not enabled |
| python-pgvector | instrumentation | unqualified | pgvector: &gt;=0.2.0; psycopg2-binary: &gt;=2.9.0 | not enabled |
| python-pinecone | instrumentation | known_broken | pinecone-client: &gt;=3.0.0 | not enabled |
| python-pipecat | instrumentation | unqualified | pipecat-ai: undeclared | not enabled |
| python-portkey | instrumentation | known_broken | portkey-ai: &gt;0.1.0 | not enabled |
| python-pydantic-ai | instrumentation | known_broken | pydantic-ai: &gt;=0.1.0 | not enabled |
| python-qdrant | instrumentation | known_broken | qdrant-client: &gt;=1.6.0 | not enabled |
| python-redis-vector | instrumentation | unqualified | redis: &gt;=4.5.0; redisvl: &gt;=0.1.0 | not enabled |
| python-replicate | instrumentation | unqualified | replicate: &gt;=1.0.0,&lt;2 | not enabled |
| python-semantic-kernel | instrumentation | unqualified | semantic-kernel: &gt;=1.38.0,&lt;2 | not enabled |
| python-smolagents | instrumentation | known_broken | smolagents: &gt;=1.12.0 | not enabled |
| python-strands | instrumentation | unqualified | strands-agents: &gt;=1.0.0 | not enabled |
| python-tavily | instrumentation | unqualified | tavily-python: &gt;=0.8.4,&lt;1 | not enabled |
| python-together | instrumentation | known_broken | together: &gt;=1.0.0 | not enabled |
| python-traceai | umbrella | known_broken | none | not enabled |
| python-typesafe | instrumentation | unqualified | typesafe-sdk: &gt;=0.7.0,&lt;1 | not enabled |
| python-vertexai | instrumentation | known_broken | google-cloud-aiplatform: &gt;= 1.49.0 | not enabled |
| python-vllm | instrumentation | known_broken | openai: &gt;=1.0.0 | not enabled |
| python-voyage | instrumentation | unqualified | voyageai: &gt;=0.3.7,&lt;1 | not enabled |
| python-weaviate | instrumentation | unqualified | weaviate-client: &gt;=4.0.0 | not enabled |
| python-xai | instrumentation | known_broken | openai: &gt;=1.0.0 | not enabled |
| typescript-anthropic | instrumentation | unqualified | ＠anthropic-ai/sdk: ^0.27.3 | not enabled |
| typescript-bedrock | instrumentation | unqualified | ＠aws-sdk/client-bedrock-runtime: ^3.693.0 | not enabled |
| typescript-beeai | instrumentation | unqualified | beeai-framework: ^0.1.0 | not enabled |
| typescript-cerebras | instrumentation | unqualified | ＠cerebras/cerebras_cloud_sdk: &gt;=1.0.0 | not enabled |
| typescript-chromadb | instrumentation | unqualified | chromadb: ^1.0.0 | not enabled |
| typescript-claude-agent-sdk | instrumentation | unqualified | ＠anthropic-ai/claude-agent-sdk: ^0.3.277 | not enabled |
| typescript-codex-sdk | instrumentation | unqualified | ＠openai/codex-sdk: &gt;=0.44.0 &lt;1 | not enabled |
| typescript-cohere | instrumentation | unqualified | cohere-ai: &gt;=7.0.0 | not enabled |
| typescript-cursor-sdk | instrumentation | unqualified | ＠cursor/sdk: &gt;=1.0.0 &lt;2 | not enabled |
| typescript-deepseek | instrumentation | unqualified | openai: &gt;=4.0.0 | not enabled |
| typescript-fi-core | core | unqualified | none | not enabled |
| typescript-fi-semantic-conventions | core | unqualified | none | not enabled |
| typescript-fireworks | instrumentation | unqualified | openai: &gt;=4.0.0 | not enabled |
| typescript-genkit | instrumentation | unqualified | genkit: ^1.42.0 | not enabled |
| typescript-google-adk | instrumentation | unqualified | ＠google/adk: &gt;=0.1.0 | not enabled |
| typescript-google-genai | instrumentation | unqualified | ＠google/generative-ai: &gt;=0.1.0 | not enabled |
| typescript-groq | instrumentation | unqualified | groq-sdk: &gt;=0.3.0 | not enabled |
| typescript-guardrails | instrumentation | unqualified | ＠guardrails-ai/core: ^0.1.0 | not enabled |
| typescript-huggingface | instrumentation | unqualified | ＠huggingface/inference: &gt;=2.0.0 | not enabled |
| typescript-instructor | instrumentation | unqualified | ＠instructor-ai/instructor: ^1.0.0 | not enabled |
| typescript-lancedb | instrumentation | unqualified | ＠lancedb/lancedb: &gt;=0.1.0 | not enabled |
| typescript-langchain | instrumentation | unqualified | ＠langchain/core: ^0.2.0 \|\| ^0.3.0 | not enabled |
| typescript-livekit | instrumentation | unqualified | livekit-client: &gt;=1.0.0 \|\| &gt;=2.0.0; ＠livekit/rtc-node: &gt;=0.3.0 | not enabled |
| typescript-llamaindex | instrumentation | unqualified | llamaindex: &gt;=0.1.0; ＠llamaindex/openai: &gt;=0.3.0 | not enabled |
| typescript-mastra | instrumentation | unqualified | ＠mastra/core: &gt;=1.16.0; ＠mastra/observability: &gt;=1.0.0; ＠mastra/otel-exporter: &gt;=1.0.0 | not enabled |
| typescript-mcp | instrumentation | unqualified | ＠modelcontextprotocol/sdk: ^1.10.2 | not enabled |
| typescript-milvus | instrumentation | unqualified | ＠zilliz/milvus2-sdk-node: &gt;=2.0.0 | not enabled |
| typescript-mistral | instrumentation | unqualified | ＠mistralai/mistralai: &gt;=0.1.0 | not enabled |
| typescript-mongodb | instrumentation | unqualified | mongodb: &gt;=5.0.0 | not enabled |
| typescript-ollama | instrumentation | unqualified | ollama: &gt;=0.4.0 | not enabled |
| typescript-openai | instrumentation | unqualified | openai: ^4.0.0 \|\| ^5.0.0 | not enabled |
| typescript-openai-agents | instrumentation | unqualified | ＠openai/agents: &gt;=0.4.0 &lt;0.22.0 | not enabled |
| typescript-pgvector | instrumentation | unqualified | pg: &gt;=8.0.0; pgvector: &gt;=0.1.0 | not enabled |
| typescript-pi-coding-agent | instrumentation | unqualified | ＠mariozechner/pi-coding-agent: undeclared | not enabled |
| typescript-pinecone | instrumentation | unqualified | ＠pinecone-database/pinecone: ^2.0.0 \|\| ^3.0.0 \|\| ^4.0.0 | not enabled |
| typescript-pipecat | instrumentation | unqualified | ＠pipecat-ai/client-js: &gt;=0.2.0 | not enabled |
| typescript-portkey | instrumentation | unqualified | portkey-ai: ^1.0.0 | not enabled |
| typescript-qdrant | instrumentation | unqualified | ＠qdrant/js-client-rest: ^1.0.0 | not enabled |
| typescript-redis | instrumentation | unqualified | redis: &gt;=4.0.0 | not enabled |
| typescript-strands | instrumentation | unqualified | ＠strands-agents/sdk: ^0.1.0 | not enabled |
| typescript-together | instrumentation | unqualified | together-ai: &gt;=0.5.0 | not enabled |
| typescript-vercel | instrumentation | unqualified | ai: ^6.0.0 | not enabled |
| typescript-vertexai | instrumentation | unqualified | ＠google-cloud/vertexai: &gt;=1.0.0 | not enabled |
| typescript-vllm | instrumentation | unqualified | openai: &gt;=4.0.0 | not enabled |
| typescript-voltagent | instrumentation | unqualified | ＠voltagent/core: ^2.11.0 | not enabled |
| typescript-weaviate | instrumentation | unqualified | weaviate-client: &gt;=3.0.0 | not enabled |
| typescript-xai | instrumentation | unqualified | openai: &gt;=4.0.0 | not enabled |

## Automation health

- Stale/missing daily canary evidence for python-anthropic (last complete: none). Check the default-branch scheduler; dev-only cron is dormant. No passing status may be inferred.
- Stale/missing weekly canary evidence for python-anthropic (last complete: none). Check the default-branch scheduler; dev-only cron is dormant. No passing status may be inferred.
- Stale/missing daily canary evidence for python-langchain (last complete: none). Check the default-branch scheduler; dev-only cron is dormant. No passing status may be inferred.
- Stale/missing weekly canary evidence for python-langchain (last complete: none). Check the default-branch scheduler; dev-only cron is dormant. No passing status may be inferred.
- Stale/missing weekly canary evidence for python-openai (last complete: none). Check the default-branch scheduler; dev-only cron is dormant. No passing status may be inferred.

## Operating and release practice

- The pilot is Python OpenAI, Anthropic and LangChain. Expand only after a low-noise baseline and measured CI use.
- Main is the default branch. Cron definitions merged only into dev are dormant. An operator must separately promote trusted canary/reporter implementation to main and observe a real scheduled run. Branch self-tests and manual dry runs are not proof of active scheduling.
- Missing expected weekday/Sunday evidence is a heartbeat incident after the 36-hour daily / eight-day weekly grace; Saturday alone is not a missed sweep. Weekly completeness requires source floor/latest at the representative and boundary runtimes plus the published representative consumer; a lone cell cannot clear it. Missing other cells does not erase a fresh exact observation. Expired exceptions are automation failures.
- Only labelled, schema-valid GitHub Actions bot issues supply reporter state. Public marker copies are ignored; malformed or conflicting owned records are isolated without stopping unrelated incidents.
- Incidents deduplicate by integration + complete upstream tuple + failure signature. Two fresh, distinct comparable passing runs recover every failed scope. Development-only fixes remain release pending for published failures, including repeated failures of the unchanged released package. Pre-execution resolution/install failures may recover through a complete validated execution of the exact tuple/runtime; executed contract failures still require identical suite and failed-test coverage. A newer upstream patch does not erase an older bad tuple. Unseen old failures remain actionable even after a reporter outage.
- `release-notes.json` contains exact support/failure inputs, not an automatically approved support-policy change. Confirm published fix versions, support additions, exclusions and floor/cap/privacy changes before release.
- Agents receive a human-claimed handoff with a one-open-repair-PR marker guard. This reporter never starts an agent, invokes vendors, spends money, opens repair PRs, pushes code or merges. Slack/Linear delivery is disabled; GitHub issues are canonical.
- New integrations need a real upstream SDK contract, exact floor/family/runtime evidence, aligned metadata/guards, privacy assertions, clean built-consumer checks, no empty/skip success, and a successful scheduled result before qualification.
- Weekly review: new majors, stale/unqualified targets, expiring exceptions, release-pending fixes, maintenance time and runner use.
