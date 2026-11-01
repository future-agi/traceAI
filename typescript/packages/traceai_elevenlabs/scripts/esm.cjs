require("node:fs").writeFileSync(
  require("node:path").join(__dirname, "../dist/esm/package.json"),
  JSON.stringify({ type: "module" }),
);
