import type { SpanConverter } from "@mastra/otel-exporter";
import { readConverterVersion } from "./readConverterVersion.mjs";

type InstalledFormat = ConstructorParameters<typeof SpanConverter>[0]["format"];

// Validate the entire SemVer, including optional prerelease and build identifiers.
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-([\da-zA-Z-]+(?:\.[\da-zA-Z-]+)*))?(?:\+[\da-zA-Z-]+(?:\.[\da-zA-Z-]+)*)?$/;

function installedFormat() {
  // Mastra exposes the supported format as a type, not a runtime constant.
  try {
    const version = readConverterVersion();
    const match = typeof version === "string" && semver.exec(version);
    if (match && !match[3]?.split(".").some(id => /^0\d+$/.test(id))) {
      const major = Number(match[1]), minor = Number(match[2]);
      // The 1.6 prereleases (including 1.6.0-alpha.2) already emit 1.41.
      if (major > 1 || (major === 1 && minor >= 6)) return "GenAI_v1_41_0";
    }
  } catch {
    // The peer may not expose its manifest or the manifest may be unreadable.
  }
  return "GenAI_v1_38_0";
}

export const converterFormat = installedFormat() as InstalledFormat;
