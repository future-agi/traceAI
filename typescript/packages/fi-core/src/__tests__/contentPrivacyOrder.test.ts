import { redactEmbeddingValues } from "../trace/trace-config/structuredRedaction";

test("late embedding discriminator protects data without erasing public metadata", () => {
  const input = JSON.stringify({ visible: "PUBLIC", data: [0.314159265, 0.271828182], padding: "x".repeat(20000), object: "embedding" });
  const clean = redactEmbeddingValues(input);
  expect(clean).not.toContain("0.314159265");
  expect(clean).toContain("PUBLIC");
  expect(JSON.parse(clean).object).toBe("embedding");
  expect(redactEmbeddingValues(JSON.stringify({ object: "table", data: [1, 2] }))).toBe('{"object":"table","data":[1,2]}');
});
