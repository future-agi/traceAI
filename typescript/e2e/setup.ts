import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  SimpleSpanProcessor,
  InMemorySpanExporter,
} from "@opentelemetry/sdk-trace-base";

// Global in-memory span exporter for capturing traces during tests
export const spanExporter = new InMemorySpanExporter();

// Set up OpenTelemetry provider for tests
const provider = new NodeTracerProvider();
provider.addSpanProcessor(new SimpleSpanProcessor(spanExporter));
provider.register();

// Helper to get captured spans
export function getCapturedSpans() {
  return spanExporter.getFinishedSpans();
}

// Helper to clear captured spans
export function clearCapturedSpans() {
  spanExporter.reset();
}

// Helper to find span by name
export function findSpanByName(name: string) {
  return getCapturedSpans().find((span) => span.name === name);
}

// Helper to find all spans by name prefix
export function findSpansByPrefix(prefix: string) {
  return getCapturedSpans().filter((span) => span.name.startsWith(prefix));
}

// Helper to get span attributes
export function getSpanAttributes(span: any) {
  return span.attributes;
}

// Utility to generate random embeddings for testing
export function generateRandomEmbedding(dimensions: number): number[] {
  return Array.from({ length: dimensions }, () => Math.random() * 2 - 1);
}

// Utility to generate test documents
export function generateTestDocuments(count: number, dimensions: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `doc_${i}`,
    text: `This is test document number ${i} with some content for testing.`,
    embedding: generateRandomEmbedding(dimensions),
    metadata: {
      index: i,
      category: i % 2 === 0 ? "even" : "odd",
      timestamp: new Date().toISOString(),
    },
  }));
}

// Wait for service to be ready
export async function waitForService(
  checkFn: () => Promise<boolean>,
  maxAttempts = 30,
  intervalMs = 1000
): Promise<void> {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      if (await checkFn()) {
        return;
      }
    } catch (e) {
      // Service not ready yet
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error("Service did not become ready in time");
}

// Clean up after all tests
afterAll(async () => {
  spanExporter.reset();
});

// Clear spans before each test
beforeEach(() => {
  spanExporter.reset();
});
