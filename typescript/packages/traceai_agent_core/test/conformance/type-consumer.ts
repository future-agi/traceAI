// Compile-only consumer of the published test subpath (no execution).
import {
  runConformance,
  type Driver,
  type Report,
} from "@traceai/agent-core/test/conformance";
declare const driver: Driver;
const report: Promise<Report> = runConformance(driver);
void report;
