import { queryAttributes } from "../queryAttributes";

describe("vector query metadata", () => {
  test.each([
    ["<->", "l2"], ["<#>", "inner_product"], ["<=>", "cosine"],
    ["<+>", "l1"], ["<~>", "hamming"], ["<%>", "jaccard"],
  ])("detects %s", (operator, metric) => {
    expect(queryAttributes(`SELECT id FROM docs ORDER BY embedding ${operator} $1 LIMIT 10`, [[1, 2]]).attributes).toMatchObject({
      "fi.span.kind": "VECTOR_DB",
      "db.vector.distance_metric": metric,
      "db.vector.query.top_k": 10,
      "db.vector.query.dimensions": 2,
    });
  });

  test.each([
    "SELECT '<=>'", 'SELECT "<->"', "SELECT $$<#>$$", "SELECT $text$<->$text$",
    "SELECT 1 -- <=>", "SELECT 1 /* <=> */", "SELECT 'it''s <=>'",
  ])("ignores syntax inside literals and comments: %s", (sql) => {
    expect(queryAttributes(sql, []).attributes["fi.span.kind"]).toBeUndefined();
  });

  test.each(["insert", "update", "delete"])("retains %s classification", (operation) => {
    const { name, attributes } = queryAttributes(`${operation} FROM docs`, []);
    expect(name).toBe(`pgvector ${operation}`);
    expect(attributes["db.operation.name"]).toBe(operation);
    expect(attributes["fi.span.kind"]).toBeUndefined();
  });

  test("explicit vector insert is vector data, not a vector search", () => {
    const { attributes } = queryAttributes("INSERT INTO docs VALUES ($1::vector)", ["[1,2]"]);
    expect(attributes["fi.span.kind"]).toBe("VECTOR_DB");
    expect(attributes["db.operation.type"]).toBeUndefined();
  });

  test("selects the vector parameter instead of an unrelated numeric array", () => {
    const { attributes } = queryAttributes("SELECT id FROM docs WHERE id = ANY($1) ORDER BY embedding <-> $2 LIMIT $3", [[1, 2, 3, 4], "[0.1,0.2]", "5"]);
    expect(attributes["db.vector.query.dimensions"]).toBe(2);
    expect(attributes["db.vector.query.top_k"]).toBe(5);
    expect(attributes["db.collection.name"]).toBeUndefined();
  });

  test("omits ambiguous limits and nonnumeric vector contents", () => {
    const { attributes } = queryAttributes("WITH candidates AS (SELECT * FROM docs LIMIT 100) SELECT * FROM candidates ORDER BY embedding <-> $1 LIMIT 10", ["[\"private\"]"]);
    expect(attributes["db.vector.query.top_k"]).toBeUndefined();
    expect(attributes["db.vector.query.dimensions"]).toBeUndefined();
    expect(JSON.stringify(attributes)).not.toContain("private");
  });
});
