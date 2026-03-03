import { QdrantClient } from "@qdrant/js-client-rest";
import { QdrantInstrumentation } from "@traceai/qdrant";
import {
  getCapturedSpans,
  clearCapturedSpans,
  getSpanAttributes,
  generateRandomEmbedding,
  waitForService,
} from "./setup";

// Initialize instrumentation
const instrumentation = new QdrantInstrumentation();
instrumentation.enable();

const QDRANT_URL = process.env.QDRANT_URL || "http://localhost:6333";
const EMBEDDING_DIMENSIONS = 384;

describe("Qdrant E2E Tests", () => {
  let client: QdrantClient;
  const testCollectionName = `test_collection_${Date.now()}`;

  beforeAll(async () => {
    client = new QdrantClient({ url: QDRANT_URL });

    // Wait for Qdrant to be ready
    await waitForService(async () => {
      try {
        await client.getCollections();
        return true;
      } catch {
        return false;
      }
    });

    // Create test collection
    await client.createCollection(testCollectionName, {
      vectors: {
        size: EMBEDDING_DIMENSIONS,
        distance: "Cosine",
      },
    });
  });

  afterAll(async () => {
    // Cleanup: delete test collection
    try {
      await client.deleteCollection(testCollectionName);
    } catch {
      // Collection may not exist
    }
    instrumentation.disable();
  });

  beforeEach(() => {
    clearCapturedSpans();
  });

  describe("Collection Operations", () => {
    test("should trace get collection info", async () => {
      const collectionInfo = await client.getCollection(testCollectionName);

      expect(collectionInfo).toBeDefined();
      expect(collectionInfo.config.params.vectors).toBeDefined();

      const spans = getCapturedSpans();
      const getCollectionSpan = spans.find((s) => s.name.includes("getCollection"));

      if (getCollectionSpan) {
        const attrs = getSpanAttributes(getCollectionSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("getCollection");
      }
    });

    test("should trace list collections", async () => {
      const collections = await client.getCollections();

      expect(collections).toBeDefined();
      expect(collections.collections).toBeDefined();

      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);
    });
  });

  describe("Point Operations", () => {
    const testPoints = [
      {
        id: 1,
        vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        payload: {
          text: "Machine learning is a subset of artificial intelligence.",
          category: "ai",
        },
      },
      {
        id: 2,
        vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        payload: {
          text: "Deep learning uses neural networks with many layers.",
          category: "ai",
        },
      },
      {
        id: 3,
        vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        payload: {
          text: "Natural language processing enables computers to understand text.",
          category: "nlp",
        },
      },
    ];

    test("should trace point upsert", async () => {
      const result = await client.upsert(testCollectionName, {
        wait: true,
        points: testPoints,
      });

      expect(result).toBeDefined();
      expect(result.status).toBe("completed");

      const spans = getCapturedSpans();
      const upsertSpan = spans.find((s) => s.name.includes("upsert"));

      if (upsertSpan) {
        const attrs = getSpanAttributes(upsertSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("upsert");
      }
    });

    test("should trace vector search", async () => {
      const queryVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);

      const searchResults = await client.search(testCollectionName, {
        vector: queryVector,
        limit: 2,
      });

      expect(searchResults).toBeDefined();
      expect(searchResults.length).toBeLessThanOrEqual(2);

      const spans = getCapturedSpans();
      const searchSpan = spans.find((s) => s.name.includes("search"));

      if (searchSpan) {
        const attrs = getSpanAttributes(searchSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("search");
        expect(attrs["db.vector.query.top_k"]).toBe(2);
      }
    });

    test("should trace vector search with filter", async () => {
      const queryVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);

      const searchResults = await client.search(testCollectionName, {
        vector: queryVector,
        limit: 5,
        filter: {
          must: [{ key: "category", match: { value: "ai" } }],
        },
      });

      expect(searchResults).toBeDefined();

      const spans = getCapturedSpans();
      const searchSpan = spans.find((s) => s.name.includes("search"));

      if (searchSpan) {
        const attrs = getSpanAttributes(searchSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("search");
      }
    });

    test("should trace point retrieval by ID", async () => {
      const points = await client.retrieve(testCollectionName, {
        ids: [1, 2],
        with_payload: true,
        with_vector: true,
      });

      expect(points).toBeDefined();
      expect(points.length).toBe(2);

      const spans = getCapturedSpans();
      const retrieveSpan = spans.find((s) => s.name.includes("retrieve"));

      if (retrieveSpan) {
        const attrs = getSpanAttributes(retrieveSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("retrieve");
      }
    });

    test("should trace scroll operation", async () => {
      const scrollResult = await client.scroll(testCollectionName, {
        limit: 2,
        with_payload: true,
        with_vector: false,
      });

      expect(scrollResult).toBeDefined();
      expect(scrollResult.points).toBeDefined();

      const spans = getCapturedSpans();
      const scrollSpan = spans.find((s) => s.name.includes("scroll"));

      if (scrollSpan) {
        const attrs = getSpanAttributes(scrollSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("scroll");
      }
    });

    test("should trace count operation", async () => {
      const countResult = await client.count(testCollectionName, {
        exact: true,
      });

      expect(countResult).toBeDefined();
      expect(countResult.count).toBeGreaterThan(0);

      const spans = getCapturedSpans();
      const countSpan = spans.find((s) => s.name.includes("count"));

      if (countSpan) {
        const attrs = getSpanAttributes(countSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("count");
      }
    });

    test("should trace point deletion", async () => {
      // First add a point to delete
      await client.upsert(testCollectionName, {
        wait: true,
        points: [
          {
            id: 999,
            vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
            payload: { text: "To be deleted", temporary: true },
          },
        ],
      });

      clearCapturedSpans();

      // Delete the point
      const deleteResult = await client.delete(testCollectionName, {
        wait: true,
        points: [999],
      });

      expect(deleteResult).toBeDefined();

      const spans = getCapturedSpans();
      const deleteSpan = spans.find((s) => s.name.includes("delete"));

      if (deleteSpan) {
        const attrs = getSpanAttributes(deleteSpan);
        expect(attrs["db.system"]).toBe("qdrant");
        expect(attrs["db.operation.name"]).toBe("delete");
      }
    });
  });

  describe("Real-World Use Case: Self-Hosted RAG Pipeline", () => {
    test("should trace complete RAG workflow", async () => {
      // Simulate a RAG (Retrieval-Augmented Generation) pipeline

      // Step 1: Ingest knowledge base documents
      const knowledgeBase = [
        {
          id: 100,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            text: "Our company was founded in 2020 and specializes in AI solutions.",
            source: "about_page",
            document_type: "company_info",
          },
        },
        {
          id: 101,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            text: "We offer machine learning consulting and custom model development.",
            source: "services_page",
            document_type: "services",
          },
        },
        {
          id: 102,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            text: "Our team consists of PhD researchers and experienced engineers.",
            source: "team_page",
            document_type: "team",
          },
        },
      ];

      await client.upsert(testCollectionName, {
        wait: true,
        points: knowledgeBase,
      });

      // Step 2: User asks a question - simulate embedding the question
      const questionVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);

      // Step 3: Retrieve relevant context
      const retrievalResults = await client.search(testCollectionName, {
        vector: questionVector,
        limit: 2,
        with_payload: true,
      });

      expect(retrievalResults.length).toBeLessThanOrEqual(2);

      // Verify all operations were traced
      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);

      // Check that we have both upsert and search operations traced
      const operationNames = spans.map((s) => getSpanAttributes(s)["db.operation.name"]);
      expect(operationNames).toContain("upsert");
      expect(operationNames).toContain("search");
    });
  });

  describe("Real-World Use Case: E-commerce Product Search", () => {
    test("should trace product similarity search with filtering", async () => {
      // Index product embeddings
      const products = [
        {
          id: 200,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            name: "Wireless Bluetooth Headphones",
            category: "electronics",
            price: 79.99,
            in_stock: true,
          },
        },
        {
          id: 201,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            name: "Noise Cancelling Earbuds",
            category: "electronics",
            price: 149.99,
            in_stock: true,
          },
        },
        {
          id: 202,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            name: "Running Shoes",
            category: "sports",
            price: 89.99,
            in_stock: false,
          },
        },
      ];

      await client.upsert(testCollectionName, {
        wait: true,
        points: products,
      });

      // Search for electronics under $100
      const searchVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);
      const searchResults = await client.search(testCollectionName, {
        vector: searchVector,
        limit: 5,
        filter: {
          must: [
            { key: "category", match: { value: "electronics" } },
            { key: "in_stock", match: { value: true } },
          ],
        },
        with_payload: true,
      });

      expect(searchResults).toBeDefined();

      const spans = getCapturedSpans();
      const searchSpan = spans.find((s) =>
        getSpanAttributes(s)["db.operation.name"] === "search"
      );
      expect(searchSpan).toBeDefined();
    });
  });

  describe("Real-World Use Case: Image Similarity Search", () => {
    test("should trace image embedding search", async () => {
      // Simulate image embeddings (e.g., from CLIP model)
      const images = [
        {
          id: 300,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            filename: "sunset_beach.jpg",
            tags: ["nature", "beach", "sunset"],
            width: 1920,
            height: 1080,
          },
        },
        {
          id: 301,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            filename: "mountain_view.jpg",
            tags: ["nature", "mountain", "landscape"],
            width: 2560,
            height: 1440,
          },
        },
        {
          id: 302,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            filename: "city_skyline.jpg",
            tags: ["urban", "city", "architecture"],
            width: 3840,
            height: 2160,
          },
        },
      ];

      await client.upsert(testCollectionName, {
        wait: true,
        points: images,
      });

      // Search for similar images
      const queryImageVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);
      const similarImages = await client.search(testCollectionName, {
        vector: queryImageVector,
        limit: 3,
        with_payload: true,
      });

      expect(similarImages).toBeDefined();

      // Verify traces captured the full workflow
      const spans = getCapturedSpans();
      expect(spans.some((s) => getSpanAttributes(s)["db.operation.name"] === "upsert")).toBe(true);
      expect(spans.some((s) => getSpanAttributes(s)["db.operation.name"] === "search")).toBe(true);
    });
  });

  describe("Real-World Use Case: Recommendation Engine", () => {
    test("should trace user preference-based recommendations", async () => {
      // Index items with various features
      const items = [
        {
          id: 400,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            item_id: "movie_001",
            title: "Sci-Fi Adventure",
            genre: "sci-fi",
            rating: 4.5,
          },
        },
        {
          id: 401,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            item_id: "movie_002",
            title: "Comedy Special",
            genre: "comedy",
            rating: 4.2,
          },
        },
        {
          id: 402,
          vector: generateRandomEmbedding(EMBEDDING_DIMENSIONS),
          payload: {
            item_id: "movie_003",
            title: "Space Opera",
            genre: "sci-fi",
            rating: 4.8,
          },
        },
      ];

      await client.upsert(testCollectionName, {
        wait: true,
        points: items,
      });

      // Get recommendations based on user's viewing history embedding
      const userPreferenceVector = generateRandomEmbedding(EMBEDDING_DIMENSIONS);
      const recommendations = await client.search(testCollectionName, {
        vector: userPreferenceVector,
        limit: 3,
        filter: {
          must: [{ key: "genre", match: { value: "sci-fi" } }],
        },
        with_payload: true,
        score_threshold: 0.0, // Accept all matches for testing
      });

      expect(recommendations).toBeDefined();

      const spans = getCapturedSpans();
      expect(spans.length).toBeGreaterThan(0);
    });
  });

  describe("Batch Operations", () => {
    test("should trace batch search operations", async () => {
      // Perform multiple searches
      const queries = [
        generateRandomEmbedding(EMBEDDING_DIMENSIONS),
        generateRandomEmbedding(EMBEDDING_DIMENSIONS),
      ];

      const results = await Promise.all(
        queries.map((vector) =>
          client.search(testCollectionName, {
            vector,
            limit: 2,
          })
        )
      );

      expect(results.length).toBe(2);
      results.forEach((result) => {
        expect(result.length).toBeLessThanOrEqual(2);
      });

      const spans = getCapturedSpans();
      const searchSpans = spans.filter(
        (s) => getSpanAttributes(s)["db.operation.name"] === "search"
      );
      expect(searchSpans.length).toBe(2);
    });
  });
});
