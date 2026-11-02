import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

// Fresh processes exercise the actual CJS hook and immutable ESM namespaces;
// Jest's module loader does not model either import path faithfully.
it.each(["cjs", "esm"])(
  "built %s import preserves instrumentation lifecycle",
  (mode) => {
    const output = execFileSync(
      process.execPath,
      [resolve(__dirname, "sdk-smoke.cjs"), mode],
      { encoding: "utf8", timeout: 15000 },
    );
    expect(output).toContain(
      `${mode}: automatic/manual import, disable and re-enable passed`,
    );
  },
);
