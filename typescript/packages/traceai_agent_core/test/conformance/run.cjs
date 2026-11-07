// Run groups in isolated child processes: adapters read process.env at hook boundaries.
const fs = require("node:fs/promises"),
  path = require("node:path");
const { run, runConformance } = require("./index.cjs");
const agents = [
  "claude",
  "codex",
  "pi",
  "oh-my-pi",
  "opencode",
  "opencode-v1",
  "antigravity",
  "copilot",
  "goose",
  "hermes",
  "openclaw",
  "grok",
  "cursor",
  "cursor-cli",
];
(async () => {
  const selected = process.argv[2];
  if (selected) {
    if (!agents.includes(selected)) throw Error("Unknown adapter: " + selected);
    const driver = require(`./drivers/${selected}.cjs`);
    driver.prepareHost = require("./host-fixture.cjs").prepareHost;
    const report = await runConformance(driver);
    console.log("CONFORMANCE_JSON=" + JSON.stringify(report));
    return;
  }
  const results = [];
  let next = 0;
  await Promise.all(
    Array.from({ length: 3 }, async () => {
      while (next < agents.length) {
        const agent = agents[next++];
        const r = await run(process.execPath, [__filename, agent], {
          env: process.env,
          timeout: 480000,
        });
        const line = r.stdout
          .split("\n")
          .find((s) => s.startsWith("CONFORMANCE_JSON="));
        const report = line
          ? JSON.parse(line.slice(17))
          : {
              adapter: agent,
              group: agent,
              checks: [],
              failures: [
                `HARNESS_ERROR exit=${r.code} timeout=${r.timedOut}: ${r.stderr.slice(-1600)}`,
              ],
            };
        results.push(report);
        console.log(
          `${agent}: ${report.checks.filter((c) => c.status === "PASS").length} pass / ${report.failures.length} fail`,
        );
      }
    }),
  );
  results.sort((a, b) => agents.indexOf(a.adapter) - agents.indexOf(b.adapter));
  const groups = new Map();
  for (const r of results) {
    if (!groups.has(r.group)) groups.set(r.group, []);
    groups.get(r.group).push(...r.failures.map((f) => `${r.adapter}: ${f}`));
  }
  const directory =
    process.env.CONFORMANCE_RESULTS || "/work/conformance-results";
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(
    path.join(directory, "checks.json"),
    JSON.stringify(results, null, 2) + "\n",
  );
  await fs.writeFile(
    path.join(directory, "groups.json"),
    JSON.stringify(
      [...groups].map(([group, failures]) => ({ group, failures })),
      null,
      2,
    ) + "\n",
  );
  process.exitCode = results.some((r) => r.failures.length) ? 1 : 0;
})().catch((e) => {
  console.error(e);
  process.exitCode = 2;
});
