# traceAI for Java

OpenTelemetry instrumentation for Java AI applications. Trace LLM calls, embeddings, retrieval, and framework operations in [Future AGI](https://futureagi.com).

## Install from Maven Central

Release coordinates use **`com.futureagi`**. Artifact names are unchanged, and **Java package names remain `ai.traceai.*`**; existing imports do not change.

The install examples track Java SDK `1.0.1`, matching [`pom.xml`](pom.xml); release-please updates them together. All SDK modules inherit that version, and the BOM aligns them. <!-- x-release-please-version -->

> **Unreleased API:** the [provider ownership and shutdown](#provider-ownership-and-shutdown) behavior below, including `TraceAI.init(TraceConfig, OpenTelemetry)`, is not in the immutable Central releases `1.0.0` or `1.0.1`. Until a new release containing it is published and passes the [external-consumer check](#validate-readme-coordinates), that section requires a verified source build of this checkout. Installing an older Central release or the historical JitPack commit below is not sufficient. The basic OpenAI quickstart remains available from Central.

### Maven

Import the BOM once, then add the instrumentation you need:

```xml
<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>com.futureagi</groupId>
            <artifactId>traceai-bom</artifactId>
            <version>1.0.1</version> <!-- x-release-please-version -->
            <type>pom</type>
            <scope>import</scope>
        </dependency>
    </dependencies>
</dependencyManagement>

<dependencies>
    <dependency>
        <groupId>com.futureagi</groupId>
        <artifactId>traceai-java-openai</artifactId>
    </dependency>
    <!-- Provider SDKs are provided-scope: add the SDK your application uses.
         This is the OpenAI version used by this reactor. -->
    <dependency>
        <groupId>com.openai</groupId>
        <artifactId>openai-java</artifactId>
        <version>0.8.0</version>
    </dependency>
</dependencies>
```

Without the BOM, specify `<version>1.0.1</version>` on each traceAI dependency. <!-- x-release-please-version -->

For Spring Boot auto-configuration, **add** `traceai-spring-boot-starter` alongside `traceai-java-openai` and the OpenAI SDK shown above:

```xml
<dependency>
    <groupId>com.futureagi</groupId>
    <artifactId>traceai-spring-boot-starter</artifactId>
</dependency>
```

The starter configures traceAI and Spring AI; it does not include the direct OpenAI SDK wrapper used in the quickstart. Keep both traceAI dependencies when calling `TracedOpenAIClient`.

### Gradle

```groovy
repositories {
    mavenCentral()
}

dependencies {
    implementation platform('com.futureagi:traceai-bom:1.0.1') // x-release-please-version
    implementation 'com.futureagi:traceai-java-openai'
    implementation 'com.openai:openai-java:0.8.0'
    // For Spring Boot auto-configuration, add this as well (do not replace OpenAI):
    // implementation 'com.futureagi:traceai-spring-boot-starter'
}
```

### JitPack for unreleased commits

Use JitPack to try a specific Git commit before a Central release. Its coordinates are **not** the Central coordinates. Pin a successful build, not an unverified release tag or a moving branch.

This example uses the known-successful commit `3ef6e5d844` (the existing 25-artifact build, before the BOM was added):

```xml
<repositories>
    <repository>
        <id>jitpack.io</id>
        <url>https://jitpack.io</url>
    </repository>
</repositories>

<dependencies>
    <dependency>
        <groupId>com.github.future-agi.traceAI</groupId>
        <artifactId>traceai-java-openai</artifactId>
        <version>3ef6e5d844</version>
    </dependency>
</dependencies>
```

```groovy
repositories {
    mavenCentral()
    maven { url 'https://jitpack.io' }
}

dependencies {
    implementation 'com.github.future-agi.traceAI:traceai-java-openai:3ef6e5d844'
}
```

Add your provider SDK as in the Central example. To choose another commit, check its [JitPack build status](https://jitpack.io/#future-agi/traceAI) first and replace the commit version. A successful build is necessary but not sufficient: verify that your external application resolves and compiles against that exact commit. For commits containing this BOM, maintainers can run `python3 -I java/scripts/test_consumers.py --jitpack-version <sha>` from the repository root; it uses a fresh Maven repository and checks both the README and BOM consumers. Do not mix Central and JitPack copies of the same instrumentation on one classpath.

JitPack uses the opt-in `jitpack` Maven profile to install self-contained POMs with resolved dependency management and no parent reference. This avoids JitPack's root-artifact remapping leaving child modules dependent on an unavailable `traceai-java-parent`. Its install hook then prepares internal dependencies **and BOM dependency management** for the exact JitPack group and commit/tag, since JitPack does not rewrite managed dependencies itself. Normal builds and the Central `release` profile keep the original `com.futureagi` POMs.

## Send your first trace

The SDK reads these environment variables (`TRACEAI_API_KEY` is not the SDK's environment variable):

```bash
export FI_BASE_URL=https://api.futureagi.com
export FI_API_KEY=your-futureagi-api-key
export FI_SECRET_KEY=your-futureagi-secret-key
export FI_PROJECT_NAME=java-openai-example
export OPENAI_API_KEY=your-openai-api-key
```

Initialize once at application startup, then wrap your existing provider client:

```java
import ai.traceai.TraceAI;
import ai.traceai.openai.TracedOpenAIClient;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;

TraceAI.initFromEnvironment();
OpenAIClient openai = OpenAIOkHttpClient.builder()
    .apiKey(System.getenv("OPENAI_API_KEY"))
    .build();
TracedOpenAIClient traced = new TracedOpenAIClient(openai);
// Use traced.createChatCompletion(params) / traced.createEmbedding(params).
// At application shutdown, flush pending spans:
TraceAI.shutdown();
```

See the complete [`OpenAIExample`](examples/openai-example/src/main/java/ai/traceai/examples/OpenAIExample.java) for request construction. After [installing the reactor locally](#build-and-test), run a single small chat request:

```bash
# From java/, with the environment variables above set:
mvn -B -ntp -f examples/openai-example/pom.xml compile exec:java -Dexec.args=--smoke
```

`OPENAI_BASE_URL` and `OPENAI_MODEL` optionally select an OpenAI-compatible endpoint and model. Without `--smoke`, the example also runs an embedding request and a multi-turn chat. Provider calls use your own key and may incur provider charges. The example flushes pending spans even when a provider call fails.

### Configuration

```java
import ai.traceai.TraceConfig;

TraceAI.init(TraceConfig.builder()
    .baseUrl(System.getenv("FI_BASE_URL"))
    .apiKey(System.getenv("FI_API_KEY"))
    .secretKey(System.getenv("FI_SECRET_KEY"))
    .projectName(System.getenv("FI_PROJECT_NAME"))
    .serviceName("my-java-service")
    .hideInputs(false)
    .hideOutputs(false)
    .hideInputMessages(false)
    .hideOutputMessages(false)
    .hideErrorDetails(false)
    .enableConsoleExporter(false)
    .batchSize(512)
    .exportIntervalMs(5000)
    .build());
```

Unset connection fields fall back to the `FI_*` environment variables. The SDK appends `/tracer/v1/traces` to `FI_BASE_URL` for OTLP/HTTP export. Console export can include request and response content; enable it only when that is appropriate.

### Provider ownership and shutdown

**Unreleased; source build required.** Central `1.0.0` and `1.0.1` do not provide this overload or the lifecycle behavior described in this section. First build and verify core and the BOM from this checkout (JDK 21 and Maven):

```bash
# From java/ in this checkout; runs core tests before installing source artifacts.
mvn -B -ntp -pl traceai-bom,traceai-java-core -am install
python3 -I scripts/test_consumers.py --core-only
```

The second command compiles the exact example below as independent applications, both with and without the BOM. Continue only if both pass. The locally installed source artifact takes precedence over the older Central artifact with the same version. Do not deploy it or overwrite any published version. Until a containing release is verified, keep using this source build for this API.

Add `com.futureagi:traceai-java-core` to your application using the BOM above, or use an explicit version after the source install:

```xml
<dependency>
    <groupId>com.futureagi</groupId>
    <artifactId>traceai-java-core</artifactId>
    <version>1.0.1</version> <!-- x-release-please-version -->
</dependency>
```

`TraceAI.init(config)` and `TraceAI.initFromEnvironment()` create a provider used by `TraceAI.getTracer()` and the TraceAI wrappers. They do **not** register, replace or read global OpenTelemetry. Global tracers do not send spans through this local provider. This lets a Java agent or application SDK coexist without registration errors, including when the application registers its SDK after TraceAI starts.

If your application already owns OpenTelemetry, share its pipeline instead:

```java
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.GlobalOpenTelemetry;

// Run after the application SDK or Java agent is initialized.
TraceAI.init(TraceConfig.builder()
    .hideInputs(true)
    .hideOutputs(true)
    .hideErrorDetails(true)
    .build(), GlobalOpenTelemetry.get());
```

You can pass a non-global `OpenTelemetry` instance in place of `GlobalOpenTelemetry.get()`. This overload uses only the `FITracer` privacy options in `TraceConfig`: it does not create a provider, exporter, processor or shutdown hook, and it does not apply the config's connection or resource settings. Configure the application-owned exporter with the Future AGI trace endpoint, authentication headers and project resource attributes; retain the application's existing propagation configuration. Do not enable another instrumentor for the same client operations you wrap with TraceAI.

The first initialization wins until `TraceAI.shutdown()`. Shutdown flushes and closes only a TraceAI-owned provider, with a ten-second wait each for flush and shutdown. In borrowed mode it only detaches TraceAI; the application remains responsible for flushing and closing its SDK. Reinitialization is supported in either mode. Obtain a new `FITracer` and rebuild wrappers after restarting a TraceAI-owned provider; existing wrappers retain their previous tracer.

**Migrating from global registration:** older versions implicitly installed their provider globally. If you relied on that behavior for spans created outside TraceAI, configure an application-owned SDK for all those spans and pass it to the overload above. TraceAI no longer claims ownership of the process-wide SDK.

### Privacy controls

To suppress input/output content and exception details recorded by `FITracer`, configure all three controls:

```java
TraceConfig config = TraceConfig.builder()
    .hideInputs(true)
    .hideOutputs(true)
    .hideErrorDetails(true)
    .build();
```

- `hideInputs` suppresses input values, raw input and input messages. In Cohere it also suppresses the preamble and rerank query.
- `hideOutputs` suppresses output values, raw output and output messages. In Cohere it also suppresses generated tool-call names and arguments.
- `hideInputMessages` and `hideOutputMessages` suppress only message attributes (including Cohere's preamble and tool calls respectively). They do not suppress values or raw payloads, which can contain the same content. Setting a message flag to `false` cannot override `hideInputs(true)` or `hideOutputs(true)`.
- `hideErrorDetails` suppresses `error.message`, exception messages, exception stack traces and ERROR status descriptions written by `FITracer.setError`, including errors from Cohere chat, embed and rerank. ERROR status, `error.type` and an exception event containing `exception.type` remain available.

Input, output and error-detail hiding each default to `false`. Both `TraceConfig.builder().build()` and `TraceConfig.fromEnvironment()` read `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS`. Each variable enables its flag only for `true` (case-insensitive); unset or other values mean `false`. Explicit builder values, including `false`, override the corresponding environment variable. For example, `.hideInputs(false)` intentionally disables input hiding even when `FI_HIDE_INPUTS=true`. Input/output hiding does not implicitly enable error-detail hiding. The message-only flags remain builder options.

For Bedrock, input hiding also suppresses tool definitions; output hiding suppresses streamed text and tool arguments, and prevents content accumulation by the stream adapter. Model IDs, finish reasons and usage/cache counters remain available. Error details use their separate flag.

These controls govern the tracing helpers and the Cohere content fields described above, not arbitrary attributes or events written directly to an OpenTelemetry span by your application or another instrumentor. Do not attach sensitive content directly to spans.

Spring Boot applications can configure the starter with:

```yaml
traceai:
  enabled: true
  base-url: ${FI_BASE_URL}
  api-key: ${FI_API_KEY}
  secret-key: ${FI_SECRET_KEY}
  project-name: ${FI_PROJECT_NAME}
  service-name: my-java-service
  hide-inputs: false
  hide-outputs: false
  hide-input-messages: false
  hide-output-messages: false
```

## Instrumentation modules

Each name below is an artifact in the Maven reactor. Provider/framework dependency versions are pinned in the module POMs and parent properties; this list is not a claim of compatibility with every upstream SDK version.

| Area | Integration | Artifact |
| --- | --- | --- |
| Core | Tracer, configuration, semantic attributes | `traceai-java-core` |
| LLM | OpenAI | `traceai-java-openai` |
| LLM | Azure OpenAI | `traceai-java-azure-openai` |
| LLM | Anthropic | `traceai-java-anthropic` |
| LLM | Cohere | `traceai-java-cohere` |
| LLM | Google GenAI | `traceai-java-google-genai` |
| LLM | Ollama | `traceai-java-ollama` |
| LLM | AWS Bedrock | `traceai-java-bedrock` |
| LLM | Google Vertex AI | `traceai-java-vertexai` |
| LLM | IBM watsonx.ai | `traceai-java-watsonx` |
| Retrieval | Pinecone | `traceai-java-pinecone` |
| Retrieval | Qdrant | `traceai-java-qdrant` |
| Retrieval | Milvus | `traceai-java-milvus` |
| Retrieval | Weaviate | `traceai-java-weaviate` |
| Retrieval | ChromaDB | `traceai-java-chromadb` |
| Retrieval | MongoDB Atlas | `traceai-java-mongodb` |
| Retrieval | Redis | `traceai-java-redis` |
| Retrieval | Azure AI Search (synchronous SDK 11.6.2–11.8.1) | `traceai-java-azure-search` |
| Retrieval | Azure AI Search (synchronous SDK 12.x; compilation floor 12.0.3, newest validated 12.1.1) | `traceai-java-azure-search-v12` |
| Retrieval | PostgreSQL pgvector | `traceai-java-pgvector` |
| Retrieval | Elasticsearch | `traceai-java-elasticsearch` |
| Framework | LangChain4j | `traceai-langchain4j` |
| Framework | Spring AI | `traceai-spring-ai` |
| Framework | Spring Boot auto-configuration | `traceai-spring-boot-starter` |
| Framework | Microsoft Semantic Kernel | `traceai-java-semantic-kernel` |

For Anthropic Java 3.2.0–3.4.0, prefer the [native OpenTelemetry example](examples/anthropic-native-example) rather than the blocking-call wrapper. Use only one request-span owner. Native tracing omits server-tool billing counts; read the [server-tool billing limitation](examples/anthropic-native-example#server-tool-billing) before relying on span-based cost views.

Spring AI currently targets the `1.0.0-M4` dependency in the parent POM. The separate [Spring AI 1.1 compatibility PR #204](https://github.com/future-agi/traceAI/pull/204) is not included in this publishing change.

Additional examples are in [`examples/`](examples), including [LangChain4j](examples/langchain4j-example), [Spring AI](examples/spring-ai-example), [Semantic Kernel](examples/semantic-kernel-example), and [RAG](examples/rag-example). [Arconia](examples/arconia-example) and [Quarkus LangChain4j](examples/quarkus-langchain4j-example) use their frameworks' own OpenTelemetry export rather than an additional traceAI artifact.

### AWS Bedrock

The provided-scope AWS SDK dependency has a minimum version of `2.25.67`; add `software.amazon.awssdk:bedrockruntime` to your application. Use `BedrockRuntimeInterceptor` for synchronous `Converse` and `InvokeModel`, or `TracedBedrockRuntimeClient` around an existing synchronous client. For asynchronous operations use `TracedBedrockRuntimeAsyncClient`; the execution interceptor alone intentionally ignores async clients because it cannot reliably observe future cancellation.

```java
import ai.traceai.TraceAI;
import ai.traceai.bedrock.TracedBedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.ConverseStreamResponseHandler;

// Configure AWS credentials, region and model access in the application as usual.
try (BedrockRuntimeAsyncClient sdk = BedrockRuntimeAsyncClient.create()) {
    var traced = new TracedBedrockRuntimeAsyncClient(sdk, TraceAI.getTracer());
    var handler = ConverseStreamResponseHandler.builder()
        .subscriber(event -> { /* handle the original SDK event */ })
        .build();
    traced.converseStream(request, handler).join(); // request is a ConverseStreamRequest
}
```

The async facade supports `converse`, `invokeModel` and `converseStream`. A stream span inherits the calling parent context, captures text/tool deltas and usage, and ends once on terminal completion, failure, or cancellation—not on receipt of response headers. Your handler callbacks run with the LLM span current. Original SDK response/event objects and subscriber demand are forwarded; the adapter makes no additional event requests. Cancelling the returned future forwards to the SDK; cancelling the subscription ends the span with a cancellation error and cancels the SDK future only if it has not already completed. AWS can complete its future successfully or report transport cancellation depending on which SDK cancellation listener wins (including cancellation by its Consumer subscriber after a callback throws); the adapter preserves the SDK outcome rather than choosing an application result. Caller-side future timeouts are forwarded too. Consume, cancel, or time out every stream you start. A second instrumentor around the same operations can produce duplicate spans; do not enable both.

Retryable SDK error callbacks do not end the logical stream span: its status and duration follow the final SDK future, except for application callback failures and explicit subscription cancellation. Each new event-stream attempt replaces accumulated output, usage/cache counters, finish reason and truncation state; these attributes describe the last streamed attempt, not a sum of retries. A terminal stream error retains that attempt's partial output.

**Time to first token:** `gen_ai.client.time_to_first_token` is a double-valued span attribute in **seconds**, measured with a monotonic clock from the facade's stream initialization (before SDK dispatch) to the first nonempty text delta, tool ID/name, or tool-argument delta received by the adapter. It measures client-observed latency, including connection setup and retry waits before that content—not server-only generation time. Headers, message-start and empty deltas do not count. Content from a subsequently failed attempt still counts if it reached the adapter; the first-content timestamp is not reset on retries. The value survives output hiding and errors/cancellation after content; it is absent when no qualifying content arrived. Reasoning/media events currently do not count toward this text/tool latency.

Streaming telemetry stores at most 32,000 content characters and 128 content blocks for the current attempt; additional content still reaches your handler unchanged. `gen_ai.bedrock.output_truncated=true` marks this limit. Large JSON attributes use an omission marker to stay valid JSON. Stream `raw.output` is an assembled message, not the wire event sequence. `InvokeModelWithResponseStream` and streamed reasoning/media aggregation are not supported; unknown events are still forwarded. The facade does not close your SDK client.

`InvokeModel` normalizes Titan text (`inputTextTokenCount` and all `results[].tokenCount`), Meta (`prompt_token_count`/`generation_token_count`), and Nova nested messages and camelCase usage, in addition to existing snake_case usage responses. Unknown provider payloads still receive a lifecycle span; raw content respects privacy controls.

**Token and cache accounting:** `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` retain the provider's input/output counts without adding cache counts. `gen_ai.usage.total_tokens` retains a supplied total; when absent it is input plus output. Cache reads and writes are separate `gen_ai.usage.cache_read_tokens` and `gen_ai.usage.cache_write_tokens` attributes. Do not add them to the reported input or total without checking that model's billing semantics. Converse and ConverseStream expose cache counters only when the installed AWS SDK models them; older SDKs such as `2.25.67` discard those fields, so traceAI leaves them absent, not zero. Usage survives input/output hiding. No monetary cost is calculated.

### Azure AI Search paging

Use [Azure's native OpenTelemetry tracing](https://learn.microsoft.com/azure/developer/java/sdk/tracing) for SDK and HTTP spans. Add `TracedSearchClient` when you need Future AGI retrieval attributes such as index name, search mode, vector dimensions, result count, and top score. Choose the artifact that matches your application's `azure-search-documents` major version. Add the Azure SDK dependency explicitly; it is provided-scope in traceAI. Neither adapter wraps `SearchAsyncClient`.

| Azure SDK | traceAI artifact | Wrapper import |
| --- | --- | --- |
| 11.6.2–11.8.1 | `traceai-java-azure-search` | `ai.traceai.azure.search.TracedSearchClient` |
| 12.x (compilation floor: 12.0.3; validated: 12.0.3 and 12.1.1) | `traceai-java-azure-search-v12` | `ai.traceai.azure.search.v12.TracedSearchClient` |

The SDK 12 adapter is a separate module because Azure changed its public paging types. Do not combine the two adapters on one classpath. The 12.x adapter provides text, vector, filtered-vector, and hybrid retrieval; use `unwrap()` for Azure document operations. Existing 11.x constructors and document-operation methods remain unchanged. Use the SDK 12 artifact only after a release containing it is available, or build it locally with the reactor below.

Search methods return lazy `SearchPagedIterable` results. Consume the results to execute the search. Each requested page has a completed `RETRIEVER` span under the context active when you called the search method. `azure_search.result_scope=page` identifies page-level `db.vector.results.count` and `azure_search.top_score`; these are not totals for the query. The captured FI session, user, metadata, and tags belong to the caller, even if another thread or user consumes the results. With SDK 11, reading metadata or iterating again reuses the first page and its trace. SDK 12 does not cache pages: iterating again requests them again, and count/coverage metadata is read from each `SearchPagedResponse`. Both adapters retain the underlying SDK's continuation tokens and replay behavior. Stopping early does not fetch remaining pages or leave an unfinished retrieval span. Query text, filters, and SDK 11 document lookup keys respect `hideInputs`, and error details respect `hideErrorDetails`. These settings apply to traceAI spans; configure Azure's native SDK/HTTP telemetry separately.

## Build and test

Build with **JDK 21** (also used by CI and JitPack) and Maven. The checked-in `./mvnw` wrapper pins Maven 3.9.9 and verifies the distribution checksum; JitPack uses it rather than an image-dependent Maven version. The SDK modules target Java 17 bytecode; the Arconia and Quarkus examples have their own Java 21 requirements.

```bash
cd java
mvn -B -ntp verify
mvn -B -ntp install
```

`verify` runs the SDK reactor's tests. Examples are separate applications, not published modules. A local `install` makes the `com.futureagi` coordinates available for the example commands above. After installing, run `python3 -I scripts/test_consumers.py` to compile independent applications using the README OpenAI snippet, the additive Spring Boot dependencies, and BOM-managed libraries on the main classpath. CI repeats these checks for both the normal and JitPack POMs.

### Validate README coordinates

The consumer check reads the advertised Maven, BOM and Gradle versions from this README, requires them to match the reactor, and compiles the OpenAI quickstart and borrowed-SDK example outside the reactor. CI checks source-installed artifacts; that alone is not evidence that an immutable Central artifact contains an API.

After a new release containing the ownership API is published, verify its release-managed README coordinates against Central in a fresh Maven repository:

```bash
# From java/; resolves public artifacts only, without a local source install.
python3 -I scripts/test_consumers.py --central --core-only
```

This check is expected to fail for Central `1.0.0` and `1.0.1`. Remove the unreleased/source-build notices only after it passes for a new published version; do not replace those immutable releases. The check never publishes or calls a provider.

### Validate release artifacts without publishing

```bash
mvn -B -ntp -P release -Dgpg.skip package
mvn -B -ntp -P release help:effective-pom -Doutput=target/central-effective-pom.xml
python3 -I scripts/check_central.py --artifacts
```

The `release` profile attaches source and Javadoc JARs, signs at `verify`, and configures the Sonatype Central publishing extension. Normal builds do none of these release steps. The checker validates every effective POM's Central-required metadata, inherited coordinates, BOM coverage, and release JARs.

**Maintainer-only publishing contract:** `mvn -B -ntp -P release deploy` from `java/` signs with the environment-backed BC signer and deploys using server ID `central`. It requires `MAVEN_GPG_PRIVATE_KEY` (an armored OpenPGP secret key), `MAVEN_GPG_PASSPHRASE`, and a Maven `settings.xml` server mapping `central` to `${env.MAVEN_CENTRAL_USERNAME}` / `${env.MAVEN_CENTRAL_PASSWORD}`. Credentials and namespace verification must be set up by a maintainer. `autoPublish=true` and `waitUntil=published` mean this command publishes rather than merely stages; do not use it for a dry run.

## Resources

- [Tracing documentation](https://docs.futureagi.com/docs/tracing)
- [Integration documentation](https://docs.futureagi.com/docs/integrations)
- [Report an issue](https://github.com/future-agi/traceAI/issues)
- [Apache-2.0 license](../LICENSE)
