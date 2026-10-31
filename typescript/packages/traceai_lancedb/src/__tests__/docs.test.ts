import { createServer } from "node:http";
import { AddressInfo } from "node:net";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

it("runs the README example and exports query metadata without record content", async () => {
  const root = resolve(__dirname, "../..");
  const readme = await readFile(join(root, "README.md"), "utf8");
  const source = readme.match(/```javascript\n([\s\S]*?)\n```/)![1];
  const script = join(root, "scripts", `.readme-example-${process.pid}.mjs`);
  const dir = await mkdtemp(join(tmpdir(), "traceai-lancedb-docs-"));
  const payloads: any[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    payloads.push(JSON.parse(Buffer.concat(chunks).toString()));
    res.setHeader("Content-Type", "application/json");
    res.end("{}");
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    await writeFile(script, source);
    const host = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    let output = "";
    const exit = await new Promise<number | null>((resolve, reject) => {
      const child = spawn(process.execPath, [script], {
        cwd: dir,
        env: { PATH: process.env.PATH, FI_BASE_URL: host, FI_API_KEY: "synthetic-docs", FI_SECRET_KEY: "synthetic-docs" },
      });
      child.stdout.on("data", chunk => { output += chunk; });
      child.stderr.on("data", chunk => { output += chunk; });
      child.on("error", reject);
      child.on("close", resolve);
    });
    expect({ exit, output: exit === 0 ? "" : output }).toEqual({ exit: 0, output: "" });
    const spans = payloads.flatMap(payload => payload.resourceSpans.flatMap((resource: any) =>
      resource.scopeSpans.flatMap((scope: any) => scope.spans)));
    expect(spans.map((span: any) => span.name)).toEqual(["lancedb createTable", "lancedb search"]);
    expect(spans[1].attributes).toEqual(expect.arrayContaining([
      { key: "db.vector.query.top_k", value: { intValue: 1 } },
      { key: "db.vector.query.dimensions", value: { intValue: 2 } },
      { key: "db.response.returned_rows", value: { intValue: 1 } },
    ]));
    const wire = JSON.stringify(payloads);
    expect(wire).not.toContain("Search documentation");
    expect(wire).not.toContain("Deployment guide");
    expect(wire).not.toContain("synthetic-docs");
  } finally {
    await rm(script, { force: true });
    await rm(dir, { recursive: true, force: true });
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
