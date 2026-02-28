# TraceAI Java SDK

Enterprise-grade AI observability for Java applications. Part of the [TraceAI](https://futureagi.com) platform.

## Overview

TraceAI Java SDK provides comprehensive instrumentation for AI/LLM applications in Java, enabling:

- **Automatic Tracing**: Capture LLM calls, embeddings, vector searches, tool executions, and agent interactions
- **GenAI Semantic Conventions**: Full compliance with OpenTelemetry GenAI semantic conventions
- **LLM Providers**: OpenAI, Azure OpenAI, Anthropic, Cohere, Google GenAI, Ollama, AWS Bedrock, Google Vertex AI, IBM watsonx.ai
- **Vector Databases**: Pinecone, Qdrant, Milvus, Weaviate, ChromaDB, MongoDB Atlas, Redis, Azure AI Search, PostgreSQL pgvector, Elasticsearch
- **Frameworks**: LangChain4j, Spring AI, Microsoft Semantic Kernel with Spring Boot auto-configuration
- **Low Overhead**: Minimal performance impact on your applications

## Quick Start

### Maven

```xml
<!-- Core + OpenAI -->
<dependency>
    <groupId>ai.traceai</groupId>
    <artifactId>traceai-java-openai</artifactId>
    <version>0.1.0</version>
</dependency>

<!-- Or for Spring Boot -->
<dependency>
    <groupId>ai.traceai</groupId>
    <artifactId>traceai-spring-boot-starter</artifactId>
    <version>0.1.0</version>
</dependency>
```

### Gradle

```groovy
// Core + OpenAI
implementation 'ai.traceai:traceai-java-openai:0.1.0'

// Or for Spring Boot
implementation 'ai.traceai:traceai-spring-boot-starter:0.1.0'
```

## Basic Usage

### OpenAI

```java
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.openai.TracedOpenAIClient;

// Initialize TraceAI
TraceAI.init(TraceConfig.builder()
    .baseUrl("https://api.futureagi.com")
    .apiKey(System.getenv("TRACEAI_API_KEY"))
    .projectName("my-java-app")
    .build());

// Create traced client
OpenAIClient openai = OpenAIOkHttpClient.builder()
    .apiKey(System.getenv("OPENAI_API_KEY"))
    .build();
TracedOpenAIClient traced = new TracedOpenAIClient(openai);

// All calls are now traced!
ChatCompletion response = traced.createChatCompletion(
    ChatCompletionCreateParams.builder()
        .model("gpt-4")
        .addMessage(ChatCompletionMessageParam.user("Hello!"))
        .build()
);
```

### LangChain4j

```java
import ai.traceai.langchain4j.TracedChatLanguageModel;
import ai.traceai.langchain4j.TracedAiServices;

// Wrap a ChatLanguageModel
ChatLanguageModel model = OpenAiChatModel.builder()
    .apiKey("...")
    .modelName("gpt-4")
    .build();

ChatLanguageModel traced = new TracedChatLanguageModel(model, "openai");

// Or wrap an AiService
interface Assistant {
    String chat(String message);
}

Assistant assistant = TracedAiServices.create(
    Assistant.class,
    AiServices.builder(Assistant.class)
        .chatLanguageModel(model)
        .build()
);
```

### Cohere

```java
import ai.traceai.cohere.TracedCohereClient;

CohereClient cohere = CohereClient.builder()
    .apiKey(System.getenv("COHERE_API_KEY"))
    .build();
TracedCohereClient traced = new TracedCohereClient(cohere);

// Chat
ChatResponse chat = traced.chat(ChatRequest.builder()
    .message("What is AI?")
    .build());

// Embeddings
EmbedResponse embed = traced.embed(EmbedRequest.builder()
    .texts(List.of("Hello world"))
    .model("embed-english-v3.0")
    .build());

// Rerank
RerankResponse rerank = traced.rerank(RerankRequest.builder()
    .query("What is AI?")
    .documents(List.of("AI is...", "Machine learning..."))
    .model("rerank-english-v3.0")
    .build());
```

### Ollama (Local LLMs)

```java
import ai.traceai.ollama.TracedOllamaAPI;

OllamaAPI ollama = new OllamaAPI("http://localhost:11434");
TracedOllamaAPI traced = new TracedOllamaAPI(ollama);

// Generate
OllamaResult result = traced.generate("llama2", "What is AI?", null);

// Embeddings
List<double[]> embeddings = traced.embed("llama2", List.of("Hello world"));
```

### Azure OpenAI

```java
import ai.traceai.azure.openai.TracedAzureOpenAIClient;
import com.azure.ai.openai.OpenAIClientBuilder;

// Create Azure OpenAI client
OpenAIClient client = new OpenAIClientBuilder()
    .endpoint(System.getenv("AZURE_OPENAI_ENDPOINT"))
    .credential(new AzureKeyCredential(System.getenv("AZURE_OPENAI_API_KEY")))
    .buildClient();

TracedAzureOpenAIClient traced = new TracedAzureOpenAIClient(client);

// Chat completions
ChatCompletions response = traced.getChatCompletions("gpt-4-deployment",
    new ChatCompletionsOptions(List.of(
        new ChatRequestUserMessage("What is machine learning?")
    )));

// Embeddings
Embeddings embeddings = traced.getEmbeddings("text-embedding-ada-002",
    new EmbeddingsOptions(List.of("Hello world")));
```

### IBM watsonx.ai

```java
import ai.traceai.watsonx.TracedWatsonxAI;

// Create watsonx client (using reflection-based API)
Object watsonxClient = createWatsonxClient();
TracedWatsonxAI traced = new TracedWatsonxAI(watsonxClient);

// Text generation
Object response = traced.generateText(textGenRequest);

// Chat
Object chatResponse = traced.chat(chatRequest);

// Embeddings
Object embeddings = traced.embedText(embedRequest);
```

### Microsoft Semantic Kernel

```java
import ai.traceai.semantickernel.*;
import com.microsoft.semantickernel.Kernel;

// Create Semantic Kernel
Kernel kernel = Kernel.builder()
    .withAIService(ChatCompletionService.class, chatService)
    .build();

// Wrap with tracing
TracedKernel tracedKernel = new TracedKernel(kernel);

// Invoke functions with tracing
FunctionResult<String> result = tracedKernel.invokeAsync(myFunction, arguments).block();

// Or use TracedChatCompletionService directly
TracedChatCompletionService tracedChat = new TracedChatCompletionService(
    chatService, "gpt-4", "openai");

List<ChatMessageContent> response = tracedChat.getChatMessageContentsAsync(
    chatHistory, kernel, null).block();

// Embeddings with tracing
TracedTextEmbeddingGenerationService tracedEmbedding =
    new TracedTextEmbeddingGenerationService(embeddingService, "text-embedding-ada-002");

List<Embedding> embeddings = tracedEmbedding.generateEmbeddingsAsync(
    List.of("Hello world")).block();
```

### Vector Databases

```java
// Pinecone
import ai.traceai.pinecone.TracedPineconeIndex;

Pinecone pinecone = new Pinecone.Builder().apiKey("...").build();
Index index = pinecone.getIndexConnection("my-index");
TracedPineconeIndex traced = new TracedPineconeIndex(index, "my-index");

QueryResponse results = traced.query(queryVector, 10, null, true, true);

// Qdrant
import ai.traceai.qdrant.TracedQdrantClient;

QdrantClient qdrant = new QdrantClient(QdrantGrpcClient.newBuilder("localhost", 6334, false).build());
TracedQdrantClient traced = new TracedQdrantClient(qdrant);

List<ScoredPoint> results = traced.search("my_collection", queryVector, 10, null, null);

// ChromaDB
import ai.traceai.chromadb.TracedChromaCollection;

Client client = new Client("http://localhost:8000");
Collection collection = client.getOrCreateCollection("my_collection", null, null, null);
TracedChromaCollection traced = new TracedChromaCollection(collection, "my_collection");

Collection.QueryResponse results = traced.query(queryEmbeddings, 10, null, null, null);

// Elasticsearch
import ai.traceai.elasticsearch.TracedElasticsearchClient;

ElasticsearchClient esClient = new ElasticsearchClient(transport);
TracedElasticsearchClient traced = new TracedElasticsearchClient(esClient);

// k-NN vector search
SearchResponse<Map<String, Object>> results = traced.knnSearch(
    "my-index", queryVector, 10, 100, "embedding");

// Index documents with vectors
traced.index("my-index", "doc-1", Map.of(
    "title", "Hello World",
    "embedding", vectorData
));

// PostgreSQL pgvector
import ai.traceai.pgvector.TracedPgVectorStore;

DataSource dataSource = createDataSource();
TracedPgVectorStore store = new TracedPgVectorStore(dataSource);

// Create table and index
store.createTable("documents", 1536);
store.createIndex("documents", "HNSW", 16);

// Insert vectors
store.insert("documents", "doc-1", embedding, Map.of("title", "Hello"));

// Similarity search
List<SearchResult> results = store.search("documents", queryVector, 10, "cosine");

// Azure AI Search
import ai.traceai.azure.search.TracedSearchClient;

SearchClient searchClient = new SearchClientBuilder()
    .endpoint(endpoint)
    .indexName("my-index")
    .credential(new AzureKeyCredential(apiKey))
    .buildClient();

TracedSearchClient traced = new TracedSearchClient(searchClient, "my-index");

// Vector search
SearchPagedIterable results = traced.searchWithVector(null, vectorData, "embedding", 10);

// Hybrid search (text + vector)
SearchPagedIterable hybridResults = traced.hybridSearch("machine learning", vectorData, "embedding", 10);
```

### Spring Boot

```java
@SpringBootApplication
public class MyApp {
    public static void main(String[] args) {
        SpringApplication.run(MyApp.class, args);
    }
}
```

```yaml
# application.yml
traceai:
  base-url: https://api.futureagi.com
  api-key: ${TRACEAI_API_KEY}
  project-name: my-spring-app
```

## Modules

### Core
| Module | Description |
|--------|-------------|
| `traceai-java-core` | Core library with TraceConfig, FITracer, SemanticConventions |

### LLM Providers
| Module | Description | Integration |
|--------|-------------|-------------|
| `traceai-java-openai` | OpenAI Java SDK instrumentation | `com.openai:openai-java` |
| `traceai-java-azure-openai` | Azure OpenAI SDK instrumentation | `com.azure:azure-ai-openai` |
| `traceai-java-anthropic` | Anthropic Java SDK instrumentation | `com.anthropic:anthropic-java` |
| `traceai-java-cohere` | Cohere Java SDK instrumentation | `com.cohere:cohere-java` |
| `traceai-java-google-genai` | Google Generative AI instrumentation | `com.google.ai.client.generativeai:google-genai` |
| `traceai-java-ollama` | Ollama (local LLMs) instrumentation | `io.github.ollama4j:ollama4j` |
| `traceai-java-bedrock` | AWS Bedrock instrumentation | `software.amazon.awssdk:bedrockruntime` |
| `traceai-java-vertexai` | Google Vertex AI instrumentation | `com.google.cloud:google-cloud-vertexai` |
| `traceai-java-watsonx` | IBM watsonx.ai instrumentation | `com.ibm.watsonx:watsonx-ai` |

### Vector Databases
| Module | Description | Integration |
|--------|-------------|-------------|
| `traceai-java-pinecone` | Pinecone vector database instrumentation | `io.pinecone:pinecone-client` |
| `traceai-java-qdrant` | Qdrant vector database instrumentation | `io.qdrant:client` |
| `traceai-java-milvus` | Milvus vector database instrumentation | `io.milvus:milvus-sdk-java` |
| `traceai-java-weaviate` | Weaviate vector database instrumentation | `io.weaviate:client` |
| `traceai-java-chromadb` | ChromaDB vector database instrumentation | `io.github.amikos-tech:chromadb-java-client` |
| `traceai-java-mongodb` | MongoDB Atlas Vector Search instrumentation | `org.mongodb:mongodb-driver-sync` |
| `traceai-java-redis` | Redis Vector Search (RediSearch) instrumentation | `redis.clients:jedis` |
| `traceai-java-azure-search` | Azure AI Search (Cognitive Search) instrumentation | `com.azure:azure-search-documents` |
| `traceai-java-pgvector` | PostgreSQL pgvector instrumentation | `com.pgvector:pgvector` + `org.postgresql:postgresql` |
| `traceai-java-elasticsearch` | Elasticsearch vector search instrumentation | `co.elastic.clients:elasticsearch-java` |

### Frameworks
| Module | Description | Integration |
|--------|-------------|-------------|
| `traceai-langchain4j` | LangChain4j instrumentation | `dev.langchain4j:langchain4j` |
| `traceai-spring-ai` | Spring AI instrumentation | `org.springframework.ai:spring-ai-core` |
| `traceai-spring-boot-starter` | Spring Boot auto-configuration | Spring Boot 3.x |
| `traceai-java-semantic-kernel` | Microsoft Semantic Kernel instrumentation | `com.microsoft.semantic-kernel:semantickernel-api` |

## Configuration

### Programmatic Configuration

```java
TraceConfig config = TraceConfig.builder()
    .baseUrl("https://api.futureagi.com")
    .apiKey("your-api-key")
    .projectName("my-project")
    .serviceName("my-service")
    .hideInputs(false)        // Set to true to hide input values
    .hideOutputs(false)       // Set to true to hide output values
    .enableConsoleExporter(false)  // Set to true for local debugging
    .batchSize(512)           // Span batch size
    .exportIntervalMs(5000)   // Export interval in ms
    .build();

TraceAI.init(config);
```

### Environment Variables

```bash
export TRACEAI_BASE_URL=https://api.futureagi.com
export TRACEAI_API_KEY=your-api-key
export TRACEAI_PROJECT_NAME=my-project

# Then in code:
TraceAI.initFromEnvironment();
```

### Spring Boot Properties

```yaml
traceai:
  enabled: true                    # Enable/disable tracing
  base-url: https://api.futureagi.com
  api-key: ${TRACEAI_API_KEY}
  project-name: my-spring-app
  service-name: my-service         # Defaults to spring.application.name
  hide-inputs: false
  hide-outputs: false
  hide-input-messages: false
  hide-output-messages: false
  enable-console-exporter: false
  batch-size: 512
  export-interval-ms: 5000
```

## Semantic Conventions

TraceAI follows OpenTelemetry GenAI semantic conventions:

### LLM Attributes
| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | Type of AI operation (LLM, EMBEDDING, AGENT, TOOL, RETRIEVER, RERANKER) |
| `llm.system` | The AI system (e.g., "openai", "azure-openai", "anthropic", "watsonx") |
| `llm.provider` | The LLM provider (e.g., "openai", "azure", "ibm") |
| `llm.model_name` | The model name |
| `llm.request.model` | The requested model |
| `llm.response.model` | The model used in response |
| `llm.input_messages` | Input messages (role, content) |
| `llm.output_messages` | Output messages (role, content) |
| `llm.token_count.prompt` | Prompt token count |
| `llm.token_count.completion` | Completion token count |
| `llm.token_count.total` | Total token count |
| `llm.request.temperature` | Temperature parameter |
| `llm.request.top_p` | Top-p parameter |
| `llm.request.max_tokens` | Max tokens parameter |
| `llm.response.finish_reason` | Finish reason (stop, length, tool_calls) |
| `llm.response.id` | Response ID |

### Embedding Attributes
| Attribute | Description |
|-----------|-------------|
| `embedding.model_name` | Embedding model name |
| `embedding.dimensions` | Vector dimensions |
| `embedding.vector_count` | Number of vectors generated |

### Retriever Attributes (Vector Databases)
| Attribute | Description |
|-----------|-------------|
| `retriever.top_k` | Number of results requested |
| `db.system` | Database system (elasticsearch, postgresql, azure-ai-search) |
| `db.operation` | Database operation type |

### Semantic Kernel Attributes
| Attribute | Description |
|-----------|-------------|
| `semantic_kernel.function_name` | Name of the kernel function |
| `semantic_kernel.plugin_name` | Name of the plugin |

## Examples

See the `examples/` directory for complete working examples:

### LLM Providers
- [`openai-example`](examples/openai-example) - Basic OpenAI usage
- [`azure-openai-example`](examples/azure-openai-example) - Azure OpenAI with chat and embeddings
- [`watsonx-example`](examples/watsonx-example) - IBM watsonx.ai text generation and chat

### Vector Databases
- [`elasticsearch-example`](examples/elasticsearch-example) - Elasticsearch k-NN vector search
- [`pgvector-example`](examples/pgvector-example) - PostgreSQL pgvector similarity search
- [`azure-search-example`](examples/azure-search-example) - Azure AI Search vector and hybrid search

### Frameworks
- [`langchain4j-example`](examples/langchain4j-example) - LangChain4j with AiServices
- [`spring-ai-example`](examples/spring-ai-example) - Spring Boot application
- [`semantic-kernel-example`](examples/semantic-kernel-example) - Microsoft Semantic Kernel with TracedKernel

### Real-World Patterns
- [`rag-example`](examples/rag-example) - Complete RAG pipeline with OpenAI + pgvector

## Requirements

- Java 17 or higher
- Maven 3.8+ or Gradle 8+

## Building from Source

```bash
cd java
mvn clean install
```

## License

Apache License 2.0

## Support

- Documentation: https://docs.futureagi.com
- Issues: https://github.com/future-agi/traceai-java/issues
- Email: support@futureagi.com
