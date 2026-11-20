# traceAI for Java

OpenTelemetry instrumentation for Java AI applications. Trace LLM calls, embeddings, retrieval, and framework operations in [Future AGI](https://futureagi.com).

## Install from Maven Central

Release coordinates use **`com.futureagi`**. Artifact names are unchanged, and **Java package names remain `ai.traceai.*`**; existing imports do not change.

The install examples track Java SDK `1.4.1`, matching [`pom.xml`](pom.xml); release-please updates them together. All SDK modules inherit that version, and the BOM aligns them. <!-- x-release-please-version -->

> **Unreleased API:** the [provider ownership and shutdown](#provider-ownership-and-shutdown) behavior below, including `TraceAI.init(TraceConfig, OpenTelemetry)`, is not in the immutable Central releases `1.0.0` or `1.0.1`. Until a new release containing it is published and passes the [external-consumer check](#validate-readme-coordinates), that section requires a verified source build of this checkout. Installing an older Central release or the historical JitPack commit below is not sufficient. The basic OpenAI quickstart remains available from Central.

### Maven

Import the BOM once, then add the instrumentation you need:

```xml
<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>com.futureagi</groupId>
            <artifactId>traceai-bom</artifactId>
            <version>1.4.1</version> <!-- x-release-please-version -->
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

Without the BOM, specify `<version>1.4.1</version>` on each traceAI dependency. <!-- x-release-please-version -->

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
    implementation platform('com.futureagi:traceai-bom:1.4.1') // x-release-please-version
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

Unset connection fields fall back to the `FI_*` environment variables. The SDK appends `/tracer/v1/traces` to `FI_BASE_URL` for OTLP/HTTP export. Export requires both `FI_API_KEY` and `FI_SECRET_KEY`. Future AGI hosts use `https://`; plaintext `http://` is accepted for literal `localhost`, `127.0.0.1` and `::1`, and for other hosts only when `FI_ALLOW_INSECURE_HTTP=1`. Export requests do not follow redirects, and a plaintext send does not use a proxy. Console export can include request and response content; enable it only when that is appropriate.

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
    <version>1.4.1</version> <!-- x-release-please-version -->
</dependency>
```

`TraceAI.init(config)` and `TraceAI.initFromEnvironment()` create a provider used by `TraceAI.getTracer()` and the TraceAI wrappers. They do **not** register, replace or read global OpenTelemetry. Global tracers do not send spans through this local provider. This lets a Java agent or application SDK coexist without registration errors, including when the application registers its SDK after TraceAI starts.

For these owned providers, `TraceConfig.builder().sampler(Sampler.alwaysOff())` (import `io.opentelemetry.sdk.trace.samplers.Sampler`) selects an explicit sampler. If the option is absent or null, `FI_SAMPLE_RATE` wins and selects `ParentBased(TraceIdRatio(rate))`; otherwise `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` select one of the six [standard sampler choices](traceai-javaagent/README.md#configuration). With no settings, or invalid standard settings (one warning), the default is `ParentBased(TraceIdRatio(1.0))`. Parent-based choices preserve the parent's sampled decision; even rate `0` retains children of sampled parents. This sampler option is unreleased and requires the source build above.

On TraceAI-owned providers, sampled spans carry the double attribute `fi.sampling.ratio` only for a known partial keep probability (`0 < r < 1`); dashboards can weight counts and cost by `1/r` as estimated, and absence means `1.0`. Our own partial ratio decisions write W3C tracestate `fi=r:<ratio>` while preserving other entries from valid parents; parent-based children copy only a valid ratio from their sampled parent's `fi` entry, including remote parents, and retain that entry. Only a valid span context counts as a parent: for recognized factory samplers, an invalid or absent parent makes a locally decided root and its inherited tracestate is reset (all vendors, including stale `fi`), even on a dropped root; a kept partial root writes only its own `fi`, while a full-rate root stays empty. This copying also applies to the default sampler, `parentbased_always_on/off`, and parent-based root rates `0` or `1` (including `FI_SAMPLE_RATE=1`); roots with rate `1` never gain a ratio attribute or `fi` entry. Upstream parents without valid metadata, non-parent-based always-on/off samplers, dropped/record-only spans, unknown custom samplers and application-owned providers are not stamped. Non-parent-based `traceidratio` uses only its own partial ratio, never copying upstream metadata. An explicit builder sampler is recognized only when obtained from `TraceConfig.samplerFromEnvironment(...)`, not from an arbitrary OTel/custom sampler's description.

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

You can pass a non-global `OpenTelemetry` instance in place of `GlobalOpenTelemetry.get()`. This overload uses only the `FITracer` privacy options in `TraceConfig`: it does not create a provider, exporter, processor or shutdown hook, and it does not apply the config's connection, resource or sampler settings. An application-owned provider keeps its own sampler, regardless of these options or environment variables. Configure the application-owned exporter with the Future AGI trace endpoint, authentication headers and project resource attributes; retain the application's existing propagation configuration. Do not enable another instrumentor for the same client operations you wrap with TraceAI.

Known secrets: `TraceAI.init` registers the configured API and secret keys. The Future AGI and console exporters it creates replace every occurrence of a known secret (including overlapping ones) with `<redacted>` at export time, on span names, string and string-array attributes, event and link attributes, status descriptions, and resource and instrumentation scope names, versions and attributes; other attribute types are not rewritten, and spans keep their raw values. Values shorter than 8 characters are not registered (one warning per process says so). The registry holds at most 1024 values and 4 MiB of characters; beyond that it fails closed, the exporter drops spans, and one warning per process names the bound. Where TraceAI itself cuts content to a length cap, a known secret that straddles the cut is replaced first, or the text kept before the cut loses its last characters; the cut text ends with `...[truncated]`. At export every `...[truncated]` marker in a value is a cut point, so a secret learned later also loses its start before a marker inside a JSON attribute, and a secret is replaced in its raw and its JSON-escaped (quotes, backslashes, control characters) form. Exporters on an application-owned SDK are not covered unless you wrap them: `SecretReplacingSpanExporter.wrap(exporter)` (package `ai.traceai.privacy`), and `KnownSecrets.process().register(value)` adds any other credential your application wants replaced. What is replaced before a truncation marker: any run of characters right before a `...[truncated]` marker that is a prefix of a known secret (one character or more) becomes `<redacted>`, so a short legitimate run there can be replaced, and a one-character prefix shows the full placeholder. A replaced region never splits a JSON escape: one that would start or end inside an escape (`\n`, `\t`, `\"`, `\uXXXX`) takes the whole escape, so JSON attributes still parse. `KnownSecrets.cap` and `sealCut(text, limit)` return at most `limit` characters (the cut moves back when a replacement lengthens the text); `sealCut(text)` without a limit has no fixed growth bound over the text plus the marker (each replaced match of eight or nine characters becomes the ten-character placeholder, and the number of matches is not bounded); use `sealCut(text, limit)` for a bounded result. An escaped surrogate pair (`\ud83d\ude00`) is replaced as one unit, so no lone surrogate is left. A value too large for the replacement work bound (its length times the number of secrets) is replaced whole by `<redacted>`, which is not JSON even when the attribute held JSON. Not matched: a secret written as `\u2028` or `\u2029`, and a secret inside double-encoded JSON.

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
- `hideInputMessages` and `hideOutputMessages` suppress the message attributes of their direction (including Cohere's preamble and tool calls respectively) and also redact that direction's aggregate and raw text (string) content, because it can contain the same text: `input.value`, `raw.input`, `fi.raw_input` and the prompt-template variables under `hideInputMessages`; `output.value`, `raw.output` and `fi.raw_output` under `hideOutputMessages`. Embedding input text is redacted under `hideInputMessages` too. Setting a message flag to `false` cannot override `hideInputs(true)` or `hideOutputs(true)`.
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
  allow-insecure-http: false      # the explicit form of FI_ALLOW_INSECURE_HTTP=1
  fail-on-refused-transport: true # false: log one value-free warning and disable export instead of failing the startup
```

`allow-insecure-http` lets plaintext `http://` go to a host that is neither literal loopback nor a Future AGI host; it never applies to a Future AGI host and a plaintext send is never proxied. When the target, or `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS`, may not receive the keys, the starter fails the application context with a `KeyTransport.Refused` error whose message names no value (and `FI_ALLOW_INSECURE_HTTP` when plaintext is the reason). With `fail-on-refused-transport: false` the context starts, one value-free warning is logged and nothing is exported anywhere: no exporter is created and `FI_BASE_URL` and the keys are not read again, so an approved `FI_BASE_URL` is not used either.

## Instrumentation modules

Each name below is an artifact in the Maven reactor. Provider/framework dependency versions are pinned in the module POMs and parent properties; this list is not a claim of compatibility with every upstream SDK version.

| Area | Integration | Artifact |
| --- | --- | --- |
| Core | Tracer, configuration, semantic attributes | `traceai-java-core` |
| Core | Method annotations (`@Agent`, `@Chain`, `@LLM`, `@Tool`, `@Span`, `@ExcludeFromSpan`) | `traceai-java-annotations` |
| LLM | OpenAI | `traceai-java-openai` |
| LLM | Azure OpenAI | `traceai-java-azure-openai` |
| LLM | Anthropic | `traceai-java-anthropic` |
| LLM | Cohere | `traceai-java-cohere` |
| LLM | Google GenAI | `traceai-java-google-genai` |
| Framework | Google ADK for Java (1.0.0 to 1.15.0) | `traceai-java-google-adk` |
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

### Trace your own methods

Annotate application methods and the agent turns each call into a typed span, with no tracing code:

```java
import ai.traceai.annotations.*;

public class Assistant {
    @Agent(name = "planner")
    public String answer(String question, @ExcludeFromSpan String apiKey) {
        String facts = lookup(question);
        return generate("Answer using: " + facts);
    }

    @Tool(name = "search", description = "Searches the knowledge base")
    String lookup(String query) { ... }

    @LLM
    String generate(String prompt) { ... }

    @Span(kind = "retriever", name = "fetch_docs")
    List<String> fetch(String query) { ... }
}
```

Add the annotations (they have no dependencies; the BOM manages the version) and run with the agent:

```xml
<dependency>
    <groupId>com.futureagi</groupId>
    <artifactId>traceai-java-annotations</artifactId>
</dependency>
```

```bash
java -javaagent:traceai-javaagent-<version>.jar -jar app.jar
```

Without `-javaagent`, put `traceai-javaagent` on the class path and call `TraceAIAuto.init()` once at startup; it attaches itself to the JVM and also instruments classes that are already loaded. Calling it with the agent attached changes nothing, and one annotation transformer is installed per JVM, whichever copy of the engine asks first, so no method is traced twice. Two things to know about this mode: on JDK 21 and later the JVM prints a warning when a program attaches an agent to itself, and a future JDK will refuse it unless you start the JVM with `-XX:+EnableDynamicAgentLoading`, so pass that flag (or use `-javaagent`). The woven code finds the agent first through the system class loader and then through the class loader of the annotated class, so the jar can sit on the system class path, in a Spring Boot fat jar or in a web application next to the annotated classes; if `init()` ran in a loader that the annotated classes cannot see, those methods are not traced and nothing is reported.

* **Kinds:** `@Agent` is `AGENT`, `@Chain` is `CHAIN`, `@LLM` is `LLM`, `@Tool` is `TOOL` (`gen_ai.tool.name` / `tool.name` and `gen_ai.tool.description`), and `@Span(kind = "...")` takes any other kind (`RETRIEVER`, `EMBEDDING`, `RERANKER`, `GUARDRAIL`, `WORKFLOW`; blank means `CHAIN`). If a method carries several, the first of `@Agent`, `@Chain`, `@LLM`, `@Tool`, `@Span` is used and it gets one span. The span name is the annotation's `name`, else the method name.
* **Input and output:** `input.value` is a JSON object of the arguments by parameter name (compile with `-parameters` for real names, otherwise `arg0`, `arg1`), without any `@ExcludeFromSpan` parameter. `output.value` is the JSON of the return value, omitted for `void`. Capturing a value runs no application code except a record's accessor methods and an application class's own declared `toString()`, each at most once per capture and with any failure shown as a type placeholder; values are bounded while they are read (depth 6, 100 items per collection, 2 000 values in all, at most 64 KiB of text per input or output, whichever of that and `FI_WIRE_MAX_BODY_BYTES` is smaller; the result is always valid JSON, never a cut one); a reference back to a value that is already being shown is `"<cycle>"`. What is captured for each kind of value:

| Argument or return value | Captured as |
| --- | --- |
| `null`, booleans, JDK numbers, strings, characters, enums (by name), `UUID`, `Path`, `Class`, `java.time` values, `Date`, `Locale` | the value itself (numbers that are not finite as text); strings are cut as they are read |
| `URI` | its scheme, host, port and path; never the user information or the fragment, and a query shows its keys with `<redacted>` for each value |
| `char[]` and `byte[]` | the placeholder `"<char[n]>"` or `"<byte[n]>"`, never the content (they carry passwords and tokens) |
| other arrays | a list of the first 100 elements |
| `ArrayList`, `LinkedList`, `ArrayDeque`, `Vector`, `HashMap`, `LinkedHashMap`, `TreeMap`, `EnumMap`, `Hashtable`, `HashSet`, `LinkedHashSet`, `TreeSet`, `PriorityQueue`, the `java.util.concurrent` map and list classes, `List.of` / `Set.of` / `Map.of`, `Arrays.asList`, and the `Collections` empty, singleton and `nCopies` collections | a list or object of the first 100 entries, each captured by these rules (map keys are shown only when they are scalars, otherwise as a type placeholder). |
| a `Collections` unmodifiable, synchronized or checked wrapper (it may wrap a lazy collection or be guarded by a lock), any other `Iterable`, `Map`, `Iterator`, `Enumeration`, `Stream`, `AutoCloseable` (a `DirectoryStream`, a stream-backed iterable, a lazy map, a cursor), `Future` or `Throwable` | the placeholder `"<fully.qualified.Type>"`: it is never iterated, closed or read |
| `Optional` | the contained value, or `null` |
| records | an object of their components (read through the record's accessor methods, each run once) |
| other application objects | the text of their own `toString()` when their class declares one (run once per capture, a failure is the type placeholder), with the value of every `name=value` or `name: value` pair whose name is a credential name (such as `password` or `apiToken`) replaced by `<redacted>`: a value that starts with a quote, `[`, `{` or `(` is replaced through its matching close (across line breaks), a private key through its `END` line, and any other value up to the next pair at the same nesting level, so a comma inside it does not end it; if the end cannot be found, the rest of that text is replaced; else the placeholder `"<fully.qualified.Type>"`. Fields are never read by reflection, so private fields without a `toString()` are never exported |

The text of an application's own `toString()` is built whole by that method before it is cut, so keep it cheap. Known credentials and the credential shapes (keys, JWTs, `Bearer` and `Basic` values, private keys) are replaced in a string before it is cut, reading past the cut so that one that straddles it is replaced whole, and what could still be the start of a known secret is dropped before the cut marker; all of it is replaced again on export. Numbers count against the text limit like any other text.
* **Errors:** an exception sets the error status, `error.type` and the exception event, and is rethrown unchanged.
* **Async:** a method returning `CompletableFuture` (any `CompletionStage`) or a Reactor `Mono` / `Flux` (found by name, so Reactor is not required) ends its span when the result completes, fails or is cancelled (a cancelled `CompletableFuture` carries `traceai.cancelled=true` and is not an error); `output.value` of a `Flux` is the list of its first 100 items. A cancelled span (for example `take(2)`, `next()` or `blockFirst()`) carries `traceai.cancelled=true`, its status is left as for any other span that did not fail, and its `output.value` is the items emitted before the cancel (a cancelled `Mono` has none). A `Mono` or `Flux` that is never subscribed never ends its span, and code that runs on another thread after the method returns does not see the annotated span as its parent.
* **Nesting:** an annotated call inside another is its child; an AI provider call made inside an annotated method is traced as before, as a child of the annotated span, even in an `@LLM` method. With no enclosing annotated span, your own active OpenTelemetry span is the parent.
* **Privacy:** the agent's hide switches apply (`FI_HIDE_INPUTS` drops `input.value`, `FI_HIDE_OUTPUTS` drops `output.value`, `FI_HIDE_ERROR_DETAILS` keeps only the exception type), known credentials are replaced on export, and the tool name is never withheld.
* **Scope:** only methods that carry the annotation are traced; an overriding method needs its own annotation. A default method of an interface is traced when the interface declares the annotation on it. Constructors and abstract methods are not traced, and neither are the compiler's bridge and other synthetic methods, so one call is one span. `FI_DISABLED_INSTRUMENTATIONS=annotations` turns it off. Like the rest of the agent, it stays off when the OpenTelemetry Java agent is also running.

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

Build with **JDK 21** (also used by CI and JitPack) and Maven. The checked-in `./mvnw` wrapper pins Maven 3.9.9 and verifies the distribution checksum; JitPack uses it rather than an image-dependent Maven version. The SDK modules target Java 17 bytecode, except `traceai-java-quarkus-langchain4j`, which requires Java 21; the Arconia and Quarkus examples also have their own Java 21 requirements. CI tests the complete reactor on Java 21, and on Java 17 without `traceai-java-quarkus-langchain4j`.

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
