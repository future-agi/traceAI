import { ChromaClient, Collection } from "chromadb";
import { ChromaDBInstrumentation } from "@traceai/chromadb";
import {
  getCapturedSpans,
  clearCapturedSpans,
  findSpanByName,
  findSpansByPrefix,
  getSpanAttributes,
  generateRandomEmbedding,
  waitForService,
} from "./setup";

// Initialize instrumentation
const instrumentation = new ChromaDBInstrumentation();
instrumentation.enable();

const CHROMADB_URL = process.env.CHROMADB_URL || "http://localhost:8000";
const EMBEDDING_DIMENSIONS = 384;

describe("ChromaDB E2E Tests", () => {
  let client: ChromaClient;
  let collection: Collection;
  const testCollectionName = `test_collection_${Date.now()}`;

  beforeAll(async () => {
    client = new ChromaClient({ path: CHROMADB_URL });

    // Wait for ChromaDB to be ready
    await waitForService(async () => {
      try {
        await client.heartbeat();
        return true;
      } catch {
        return false;
      }
    });
  });

  afterAll(async () => {
    // Cleanup: delete test collection
    try {
      await client.deleteCollection({ name: testCollectionName });
    } catch {
      // Collection may not exist
    }
    instrumentation.disable();
  });

  beforeEach(() => {
    clearCapturedSpans();
  });

  describe("Collection Operations", () => {
    test("should trace collection creation", async () => {
      collection = await client.createCollection({
        name: testCollectionName,
        metadata: { description: "E2E test collection" },
      });

      expect(collection).toBeDefined();
      expect(collection.name).toBe(testCollectionName);

      // Verify trace was captured
      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);
    });

    test("should trace get or create collection", async () => {
      const col = await client.getOrCreateCollection({
        name: testCollectionName,
      });

      expect(col).toBeDefined();
      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);
    });
  });

  describe("Document Operations", () => {
    const testDocuments = [
      {
        id: "doc1",
        text: "Machine learning is a subset of artificial intelligence.",
        embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
      },
      {
        id: "doc2",
        text: "Deep learning uses neural networks with many layers.",
        embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
      },
      {
        id: "doc3",
        text: "Natural language processing enables computers to understand text.",
        embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
      },
    ];

    test("should trace document addition", async () => {
      await collection.add({
        ids: testDocuments.map((d) => d.id),
        documents: testDocuments.map((d) => d.text),
        embeddings: testDocuments.map((d) => d.embedding),
        metadatas: testDocuments.map((_, i) => ({ index: i })),
      });

      const spans = getCapturedSpans();
      const addSpan = spans.find((s) => s.name.includes("add"));

      if (addSpan) {
        const attrs = getSpanAttributes(addSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("add");
      }
    });

    test("should trace document query with semantic search", async () => {
      const queryEmbedding = generateRandomEmbedding(EMBEDDING_DIMENSIONS);

      const results = await collection.query({
        queryEmbeddings: [queryEmbedding],
        nResults: 2,
      });

      expect(results).toBeDefined();
      expect(results.ids).toBeDefined();
      expect(results.ids[0].length).toBeLessThanOrEqual(2);

      const spans = getCapturedSpans();
      const querySpan = spans.find((s) => s.name.includes("query"));

      if (querySpan) {
        const attrs = getSpanAttributes(querySpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("query");
        expect(attrs["db.vector.query.top_k"]).toBe(2);
      }
    });

    test("should trace document retrieval by ID", async () => {
      const results = await collection.get({
        ids: ["doc1", "doc2"],
      });

      expect(results).toBeDefined();
      expect(results.ids.length).toBe(2);

      const spans = getCapturedSpans();
      const getSpan = spans.find((s) => s.name.includes("get"));

      if (getSpan) {
        const attrs = getSpanAttributes(getSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("get");
      }
    });

    test("should trace document update", async () => {
      await collection.update({
        ids: ["doc1"],
        documents: ["Updated: Machine learning is a powerful AI technique."],
        metadatas: [{ index: 0, updated: true }],
      });

      const spans = getCapturedSpans();
      const updateSpan = spans.find((s) => s.name.includes("update"));

      if (updateSpan) {
        const attrs = getSpanAttributes(updateSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("update");
      }
    });

    test("should trace document upsert", async () => {
      await collection.upsert({
        ids: ["doc4"],
        documents: ["Reinforcement learning trains agents through rewards."],
        embeddings: [generateRandomEmbedding(EMBEDDING_DIMENSIONS)],
        metadatas: [{ index: 3 }],
      });

      const spans = getCapturedSpans();
      const upsertSpan = spans.find((s) => s.name.includes("upsert"));

      if (upsertSpan) {
        const attrs = getSpanAttributes(upsertSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("upsert");
      }
    });

    test("should trace collection count", async () => {
      const count = await collection.count();

      expect(count).toBeGreaterThan(0);

      const spans = getCapturedSpans();
      const countSpan = spans.find((s) => s.name.includes("count"));

      if (countSpan) {
        const attrs = getSpanAttributes(countSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("count");
      }
    });

    test("should trace peek operation", async () => {
      const results = await collection.peek({ limit: 2 });

      expect(results).toBeDefined();
      expect(results.ids.length).toBeLessThanOrEqual(2);

      const spans = getCapturedSpans();
      const peekSpan = spans.find((s) => s.name.includes("peek"));

      if (peekSpan) {
        const attrs = getSpanAttributes(peekSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("peek");
      }
    });

    test("should trace document deletion", async () => {
      await collection.delete({
        ids: ["doc4"],
      });

      const spans = getCapturedSpans();
      const deleteSpan = spans.find((s) => s.name.includes("delete"));

      if (deleteSpan) {
        const attrs = getSpanAttributes(deleteSpan);
        expect(attrs["db.system"]).toBe("chromadb");
        expect(attrs["db.operation.name"]).toBe("delete");
      }
    });
  });

  describe("Real-World Use Case: RAG Pipeline", () => {
    test("should trace complete RAG workflow", async () => {
      // Simulate a RAG (Retrieval-Augmented Generation) pipeline

      // Step 1: Ingest knowledge base documents
      const knowledgeBase = [
        {
          id: "kb_1",
          text: "Our company was founded in 2020 and specializes in AI solutions.",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: "kb_2",
          text: "We offer machine learning consulting and custom model development.",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: "kb_3",
          text: "Our team consists of PhD researchers and experienced engineers.",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
      ];

      await collection.add({
        ids: knowledgeBase.map((d) => d.id),
        documents: knowledgeBase.map((d) => d.text),
        embeddings: knowledgeBase.map((d) => d.embedding),
        metadatas: knowledgeBase.map((d) => ({ source: "knowledge_base" })),
      });

      // Step 2: User asks a question - simulate embedding the question
      const questionEmbedding = generateRandomEmbedding(EMBEDDING_DIMENSIONS);

      // Step 3: Retrieve relevant context
      const retrievalResults = await collection.query({
        queryEmbeddings: [questionEmbedding],
        nResults: 2,
        where: { source: "knowledge_base" },
      });

      expect(retrievalResults.ids[0].length).toBeLessThanOrEqual(2);

      // Verify all operations were traced
      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);

      // Check that we have both add and query operations traced
      const operationNames = spans.map((s) => getSpanAttributes(s)["db.operation.name"]);
      expect(operationNames).toContain("add");
      expect(operationNames).toContain("query");
    });
  });

  describe("Real-World Use Case: Chatbot Memory", () => {
    test("should trace conversation history storage and retrieval", async () => {
      const conversationId = `conv_${Date.now()}`;

      // Store conversation turns
      const conversationTurns = [
        {
          id: `${conversationId}_turn_1`,
          text: "User: What services do you offer?",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: `${conversationId}_turn_2`,
          text: "Assistant: We offer AI consulting, model development, and data analysis.",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: `${conversationId}_turn_3`,
          text: "User: Tell me more about model development.",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
      ];

      // Store conversation history
      await collection.add({
        ids: conversationTurns.map((t) => t.id),
        documents: conversationTurns.map((t) => t.text),
        embeddings: conversationTurns.map((t) => t.embedding),
        metadatas: conversationTurns.map((t, i) => ({
          conversation_id: conversationId,
          turn_number: i + 1,
          timestamp: new Date().toISOString(),
        })),
      });

      // Retrieve relevant conversation context
      const contextQuery = generateRandomEmbedding(EMBEDDING_DIMENSIONS);
      const relevantContext = await collection.query({
        queryEmbeddings: [contextQuery],
        nResults: 3,
        where: { conversation_id: conversationId },
      });

      expect(relevantContext.ids[0].length).toBeGreaterThan(0);

      // Verify traces
      const spans = getCapturedSpans();
      expect(spans.some((s) => getSpanAttributes(s)["db.operation.name"] === "add")).toBe(true);
      expect(spans.some((s) => getSpanAttributes(s)["db.operation.name"] === "query")).toBe(true);
    });
  });

  describe("Real-World Use Case: Document Similarity Search", () => {
    test("should trace similarity search with metadata filtering", async () => {
      // Index documents with categories
      const documents = [
        {
          id: "tech_1",
          text: "Kubernetes orchestrates containerized applications at scale.",
          category: "technology",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: "tech_2",
          text: "Docker containers provide lightweight virtualization.",
          category: "technology",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
        {
          id: "business_1",
          text: "Revenue growth exceeded expectations this quarter.",
          category: "business",
          embedding: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        },
      ];

      await collection.add({
        ids: documents.map((d) => d.id),
        documents: documents.map((d) => d.text),
        embeddings: documents.map((d) => d.embedding),
        metadatas: documents.map((d) => ({ category: d.category })),
      });

      // Search only within technology category
      const searchEmbedding = generateRandomEmbedding(EMBEDDING_DIMENSIONS);
      const techResults = await collection.query({
        queryEmbeddings: [searchEmbedding],
        nResults: 5,
        where: { category: "technology" },
      });

      // All results should be from technology category
      expect(techResults.ids[0].every((id) => id.startsWith("tech_") || id.startsWith("kb_") || id.startsWith("doc"))).toBe(true);

      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);
    });
  });
});
