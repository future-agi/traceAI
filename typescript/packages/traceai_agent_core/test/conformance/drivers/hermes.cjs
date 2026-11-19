const path = require("node:path");
const { run } = require("../index.cjs");
module.exports = {
  agent: "hermes",
  async decodeRequest(bytes) {
    const r = await run(
      "/work/conformance-venv/bin/python",
      ["-I", path.join(__dirname, "decode-otlp.py")],
      { input: bytes },
    );
    if (r.code !== 0) throw Error("OTLP protobuf decode failed: " + r.stderr);
    return JSON.parse(r.stdout);
  },
  async create(f) {
    return {
      async turn(s) {
        const r = await run(
          "/work/conformance-venv/bin/python",
          ["-I", path.join(__dirname, "hermes.py")],
          {
            cwd: f.project,
            env: f.env,
            input: JSON.stringify({
              endpoint: f.endpoint,
              project: f.project,
              turns: [s],
            }),
          },
        );
        if (r.code !== 0)
          throw Error(`Hermes driver exit ${r.code}: ${r.stderr.slice(-1500)}`);
      },
    };
  },
};
