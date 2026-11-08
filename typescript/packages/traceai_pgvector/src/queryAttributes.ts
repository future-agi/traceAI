import { Attributes } from "@opentelemetry/api";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

const metrics: Record<string, string> = {
  "<->": "l2", "<#>": "inner_product", "<=>": "cosine",
  "<+>": "l1", "<~>": "hamming", "<%>": "jaccard",
};

/** Best-effort metadata, not a SQL parser. Never export parameter or row contents. */
export function queryAttributes(text: string, values: unknown): { name: string; attributes: Attributes } {
  // Ignore comments, literals and quoted identifiers when detecting operators.
  const sql = text.replace(/--[^\n]*|\/\*[\s\S]*?\*\/|'(?:''|\\.|[^'\\])*'|"(?:""|[^"])*"|\$([a-zA-Z_][\w]*|)\$[\s\S]*?\$\1\$/g, " ");
  const operator = sql.match(/<[-#=+~%]>/)?.[0];
  const vector = !!operator || /::\s*(?:vector|halfvec|sparsevec)\b|\b(?:vector|halfvec|sparsevec)\s*\(/i.test(sql);
  const command = sql.trim().match(/^\w+/)?.[0].toLowerCase();
  const operation = operator && (command === "select" || command === "with")
    ? "vectorSearch"
    : ["insert", "update", "delete"].includes(command ?? "") ? command! : "query";
  const attributes: Attributes = { "db.system": "postgresql", "db.operation.name": operation };
  if (vector) attributes[SemanticConventions.FI_SPAN_KIND] = "VECTOR_DB";
  if (operator) {
    attributes["db.operation.type"] = "vector_search";
    attributes["db.vector.distance_metric"] = metrics[operator];
    const params = Array.isArray(values) ? values : [];
    // Omit ambiguous metadata (e.g. several LIMIT clauses in a CTE).
    const limits = [...sql.matchAll(/\blimit\s+(\$\d+|\d+)\b/gi)];
    if (limits.length === 1) {
      const limit = limits[0][1];
      const value = limit.startsWith("$") ? params[Number(limit.slice(1)) - 1] : Number(limit);
      if ((typeof value === "number" || typeof value === "string") && /^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value))) {
        attributes["db.vector.query.top_k"] = Number(value);
      }
    }
    // Inspect only the parameter used by a distance operator, not arbitrary array parameters.
    const vectorParam = sql.match(/<[-#=+~%]>\s*\$(\d+)/)?.[1];
    let value = vectorParam ? params[Number(vectorParam) - 1] : undefined;
    if (typeof value === "string" && value.length <= 1_000_000 && value.startsWith("[")) {
      try { value = JSON.parse(value); } catch { value = undefined; }
    }
    if (Array.isArray(value) && value.length > 0 && value.every((entry) => typeof entry === "number" && Number.isFinite(entry))) {
      attributes["db.vector.query.dimensions"] = value.length;
    }
  }
  return { name: `pgvector ${operation}`, attributes };
}
