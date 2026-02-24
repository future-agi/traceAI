package ai.traceai.examples.rag;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.FITracer;
import ai.traceai.FISpanKind;
import ai.traceai.openai.TracedOpenAIClient;
import ai.traceai.pgvector.TracedPgVectorStore;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.models.*;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;

import javax.sql.DataSource;
import java.util.*;
import java.util.stream.Collectors;

/**
 * Real-world RAG (Retrieval-Augmented Generation) Example
 *
 * This example demonstrates a complete RAG pipeline with full observability:
 * 1. Document ingestion with embedding generation
 * 2. Vector storage in PostgreSQL with pgvector
 * 3. Semantic search for relevant context
 * 4. LLM generation with retrieved context
 *
 * All operations are automatically traced by TraceAI, providing:
 * - End-to-end latency visibility
 * - Token usage tracking
 * - Vector search performance metrics
 * - Complete conversation history
 *
 * Environment Variables:
 * - TRACEAI_API_KEY: Your TraceAI API key
 * - OPENAI_API_KEY: Your OpenAI API key
 * - POSTGRES_URL: PostgreSQL connection URL (default: jdbc:postgresql://localhost:5432/rag_demo)
 * - POSTGRES_USER: PostgreSQL username (default: postgres)
 * - POSTGRES_PASSWORD: PostgreSQL password
 */
public class RagExample {

    private static final String TABLE_NAME = "documents";
    private static final int EMBEDDING_DIMENSIONS = 1536; // text-embedding-3-small
    private static final String EMBEDDING_MODEL = "text-embedding-3-small";
    private static final String CHAT_MODEL = "gpt-4o-mini";

    private final TracedOpenAIClient openaiClient;
    private final TracedPgVectorStore vectorStore;
    private final FITracer tracer;

    public RagExample(TracedOpenAIClient openaiClient, TracedPgVectorStore vectorStore) {
        this.openaiClient = openaiClient;
        this.vectorStore = vectorStore;
        this.tracer = TraceAI.getTracer();
    }

    /**
     * Ingest documents into the vector store.
     * Creates embeddings and stores them with metadata.
     */
    public void ingestDocuments(List<Document> documents) {
        // Create a parent span for the entire ingestion process
        Span span = tracer.startSpan("RAG Document Ingestion", FISpanKind.WORKFLOW);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute("document.count", documents.size());

            System.out.println("\n=== Ingesting " + documents.size() + " documents ===\n");

            for (Document doc : documents) {
                // Generate embedding for document content
                // TraceAI automatically captures: model, dimensions, token usage
                float[] embedding = generateEmbedding(doc.content());

                // Store in vector database with metadata
                // TraceAI captures: table, dimensions, operation type
                Map<String, Object> metadata = new HashMap<>();
                metadata.put("title", doc.title());
                metadata.put("source", doc.source());
                metadata.put("timestamp", System.currentTimeMillis());

                vectorStore.insert(TABLE_NAME, doc.id(), embedding, metadata);

                System.out.println("  Ingested: " + doc.title());
            }

            span.setAttribute("status", "success");
            System.out.println("\nIngestion complete!");

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Document ingestion failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Answer a question using RAG.
     * Retrieves relevant context and generates an answer.
     */
    public String answerQuestion(String question) {
        // Create a parent span for the entire RAG query
        Span span = tracer.startSpan("RAG Query", FISpanKind.WORKFLOW);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute("question", question);

            System.out.println("\n=== RAG Query ===");
            System.out.println("Question: " + question + "\n");

            // Step 1: Generate embedding for the question
            // TraceAI captures embedding generation with model and dimensions
            float[] questionEmbedding = generateEmbedding(question);

            // Step 2: Search for relevant documents
            // TraceAI captures vector search with top_k, distance function, results count
            List<TracedPgVectorStore.SearchResult> results =
                vectorStore.search(TABLE_NAME, questionEmbedding, 3, "cosine");

            System.out.println("Retrieved " + results.size() + " relevant documents:");
            for (TracedPgVectorStore.SearchResult result : results) {
                String title = result.getMetadata().get("title").toString();
                System.out.println("  - " + title + " (distance: " +
                    String.format("%.4f", result.getDistance()) + ")");
            }

            // Step 3: Build context from retrieved documents
            String context = buildContext(results);
            span.setAttribute("context.length", context.length());

            // Step 4: Generate answer with LLM
            // TraceAI captures: model, messages, token usage, finish reason
            String answer = generateAnswer(question, context);

            span.setAttribute("answer.length", answer.length());
            span.setAttribute("status", "success");

            System.out.println("\nAnswer: " + answer);

            return answer;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("RAG query failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Generate embedding for text using OpenAI.
     */
    private float[] generateEmbedding(String text) {
        EmbeddingCreateParams params = EmbeddingCreateParams.builder()
            .model(EMBEDDING_MODEL)
            .input(text)
            .build();

        CreateEmbeddingResponse response = openaiClient.createEmbedding(params);

        List<Double> embeddingList = response.data().get(0).embedding();
        float[] embedding = new float[embeddingList.size()];
        for (int i = 0; i < embeddingList.size(); i++) {
            embedding[i] = embeddingList.get(i).floatValue();
        }
        return embedding;
    }

    /**
     * Build context string from search results.
     */
    private String buildContext(List<TracedPgVectorStore.SearchResult> results) {
        StringBuilder context = new StringBuilder();
        for (int i = 0; i < results.size(); i++) {
            TracedPgVectorStore.SearchResult result = results.get(i);
            String title = result.getMetadata().get("title").toString();
            // In a real app, you'd retrieve the full document content
            context.append("Document ").append(i + 1).append(": ").append(title).append("\n");
        }
        return context.toString();
    }

    /**
     * Generate answer using OpenAI with retrieved context.
     */
    private String generateAnswer(String question, String context) {
        String systemPrompt = """
            You are a helpful assistant that answers questions based on the provided context.
            Only use information from the context to answer. If the context doesn't contain
            enough information, say so.
            """;

        String userPrompt = """
            Context:
            %s

            Question: %s

            Please provide a helpful answer based on the context above.
            """.formatted(context, question);

        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model(CHAT_MODEL)
            .addMessage(ChatCompletionMessageParam.ofSystem(
                ChatCompletionSystemMessageParam.builder()
                    .content(systemPrompt)
                    .build()))
            .addMessage(ChatCompletionMessageParam.ofUser(
                ChatCompletionUserMessageParam.builder()
                    .content(userPrompt)
                    .build()))
            .temperature(0.7)
            .maxTokens(500)
            .build();

        ChatCompletion response = openaiClient.createChatCompletion(params);

        return response.choices().get(0).message().content();
    }

    /**
     * Initialize the vector store table and index.
     */
    public void initializeVectorStore() {
        System.out.println("Initializing vector store...");

        try {
            // Create table with vector column
            vectorStore.createTable(TABLE_NAME, EMBEDDING_DIMENSIONS);
            System.out.println("  Created table: " + TABLE_NAME);

            // Create HNSW index for fast similarity search
            vectorStore.createIndex(TABLE_NAME, "HNSW", 16);
            System.out.println("  Created HNSW index");

        } catch (Exception e) {
            // Table might already exist
            System.out.println("  Table already exists, continuing...");
        }
    }

    public static void main(String[] args) {
        // Initialize TraceAI
        TraceConfig config = TraceConfig.builder()
            .baseUrl(System.getenv().getOrDefault("TRACEAI_BASE_URL", "https://api.futureagi.com"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("rag-demo")
            .serviceName("rag-example")
            .enableConsoleExporter(true)  // Enable console output for demo
            .build();

        TraceAI.init(config);

        try {
            // Create OpenAI client
            OpenAIClient openai = OpenAIOkHttpClient.builder()
                .apiKey(System.getenv("OPENAI_API_KEY"))
                .build();
            TracedOpenAIClient tracedOpenAI = new TracedOpenAIClient(openai);

            // Create PostgreSQL data source
            DataSource dataSource = createDataSource();
            TracedPgVectorStore vectorStore = new TracedPgVectorStore(dataSource);

            // Create RAG instance
            RagExample rag = new RagExample(tracedOpenAI, vectorStore);

            // Initialize vector store
            rag.initializeVectorStore();

            // Sample documents about AI/ML
            List<Document> documents = List.of(
                new Document("doc-1", "Introduction to Machine Learning",
                    "Machine learning is a subset of artificial intelligence that enables systems to learn from data.",
                    "ml-guide"),
                new Document("doc-2", "Deep Learning Fundamentals",
                    "Deep learning uses neural networks with multiple layers to model complex patterns.",
                    "dl-book"),
                new Document("doc-3", "Natural Language Processing",
                    "NLP enables computers to understand, interpret, and generate human language.",
                    "nlp-course"),
                new Document("doc-4", "Computer Vision Applications",
                    "Computer vision allows machines to derive meaningful information from visual inputs.",
                    "cv-tutorial"),
                new Document("doc-5", "Reinforcement Learning",
                    "RL trains agents to make decisions by rewarding desired behaviors.",
                    "rl-textbook")
            );

            // Ingest documents
            rag.ingestDocuments(documents);

            // Ask questions
            System.out.println("\n" + "=".repeat(60) + "\n");
            rag.answerQuestion("What is machine learning?");

            System.out.println("\n" + "=".repeat(60) + "\n");
            rag.answerQuestion("How does deep learning work?");

            System.out.println("\n" + "=".repeat(60) + "\n");
            rag.answerQuestion("What are the applications of computer vision?");

            // Summary
            System.out.println("\n" + "=".repeat(60));
            System.out.println("RAG Demo Complete!");
            System.out.println("=".repeat(60));
            System.out.println("\nTraceAI captured the following traces:");
            System.out.println("  - RAG Document Ingestion (parent span)");
            System.out.println("    - 5x OpenAI Embedding calls");
            System.out.println("    - 5x PgVector Insert operations");
            System.out.println("  - 3x RAG Query (parent spans)");
            System.out.println("    - Question embedding generation");
            System.out.println("    - Vector similarity search");
            System.out.println("    - LLM answer generation");
            System.out.println("\nView traces at: https://app.futureagi.com");

        } finally {
            TraceAI.shutdown();
        }
    }

    private static DataSource createDataSource() {
        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(System.getenv().getOrDefault("POSTGRES_URL",
            "jdbc:postgresql://localhost:5432/rag_demo"));
        config.setUsername(System.getenv().getOrDefault("POSTGRES_USER", "postgres"));
        config.setPassword(System.getenv("POSTGRES_PASSWORD"));
        config.setMaximumPoolSize(5);
        return new HikariDataSource(config);
    }

    /**
     * Simple document record.
     */
    record Document(String id, String title, String content, String source) {}
}
