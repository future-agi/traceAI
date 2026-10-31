# traceAI for Java

OpenTelemetry instrumentation for Java AI applications. Trace LLM calls, embeddings, retrieval, and framework operations in [Future AGI](https://futureagi.com).

## Install from Maven Central

Release coordinates use **`com.futureagi`**. Artifact names are unchanged, and **Java package names remain `ai.traceai.*`**; existing imports do not change.

> These coordinates are prepared for the first Central release; this PR does not publish them. Until that release is available, use the [commit-pinned JitPack alternative](#jitpack-for-unreleased-commits) or [build locally](#build-and-test). Do not assume the old `ai.traceai` Maven coordinates are on Central.

The version below is `1.0.0`, the version in [`pom.xml`](pom.xml). All SDK modules inherit that version. The BOM aligns all 24 SDK modules.

### Maven

Import the BOM once, then add the instrumentation you need:

```xml
<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>com.futureagi</groupId>
            <artifactId>traceai-bom</artifactId>
            <version>1.0.0</version>
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

Without the BOM, specify `<version>1.0.0</version>` on each traceAI dependency. For Spring Boot auto-configuration, **add** `traceai-spring-boot-starter` alongside `traceai-java-openai` and the OpenAI SDK shown above:

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
    implementation platform('com.futureagi:traceai-bom:1.0.0')
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

Error-detail hiding is opt-in in Java: it defaults to `false`, independently of the input/output flags. Set `FI_HIDE_ERROR_DETAILS=true` to enable it through the environment; an explicit `.hideErrorDetails(true)` or `.hideErrorDetails(false)` overrides the environment.

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
| Retrieval | Azure AI Search | `traceai-java-azure-search` |
| Retrieval | PostgreSQL pgvector | `traceai-java-pgvector` |
| Retrieval | Elasticsearch | `traceai-java-elasticsearch` |
| Framework | LangChain4j | `traceai-langchain4j` |
| Framework | Spring AI | `traceai-spring-ai` |
| Framework | Spring Boot auto-configuration | `traceai-spring-boot-starter` |
| Framework | Microsoft Semantic Kernel | `traceai-java-semantic-kernel` |

Spring AI currently targets the `1.0.0-M4` dependency in the parent POM. The separate [Spring AI 1.1 compatibility PR #204](https://github.com/future-agi/traceAI/pull/204) is not included in this publishing change.

Additional examples are in [`examples/`](examples), including [LangChain4j](examples/langchain4j-example), [Spring AI](examples/spring-ai-example), [Semantic Kernel](examples/semantic-kernel-example), and [RAG](examples/rag-example). [Arconia](examples/arconia-example) and [Quarkus LangChain4j](examples/quarkus-langchain4j-example) use their frameworks' own OpenTelemetry export rather than an additional traceAI artifact.

## Build and test

Build with **JDK 21** (also used by CI and JitPack) and Maven. The checked-in `./mvnw` wrapper pins Maven 3.9.9 and verifies the distribution checksum; JitPack uses it rather than an image-dependent Maven version. The SDK modules target Java 17 bytecode; the Arconia and Quarkus examples have their own Java 21 requirements.

```bash
cd java
mvn -B -ntp verify
mvn -B -ntp install
```

`verify` runs the SDK reactor's tests. Examples are separate applications, not published modules. A local `install` makes the `com.futureagi` coordinates available for the example commands above. After installing, run `python3 -I scripts/test_consumers.py` to compile independent applications using the README OpenAI snippet, the additive Spring Boot dependencies, and BOM-managed libraries on the main classpath. CI repeats these checks for both the normal and JitPack POMs.

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
