import type { SpanConverter } from "@mastra/otel-exporter";

type InstalledFormat = ConstructorParameters<typeof SpanConverter>[0]["format"];

// Mastra exposes the converter's format as a type, not a runtime constant.
// Resolve its public manifest from this module (in both CJS and ESM builds),
// since the consumer's converter can differ from the one we built against.
const { version }: { version: string } = require("@mastra/otel-exporter/package.json");
const [major, minor] = version.split(".").map(Number);
const format = major > 1 || minor >= 6 ? "GenAI_v1_41_0" : "GenAI_v1_38_0";
export const converterFormat = format as InstalledFormat;
