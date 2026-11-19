const fs = require("node:fs");
const path = require("node:path");

function dependencyName(name, spec) {
  if (typeof spec !== "string" || !spec.startsWith("npm:")) return name;
  // npm aliases can conceal a private workspace package behind a public name.
  return spec.slice(4).match(/^(@[^/]+\/[^@]+|[^@]+)/)?.[1] ?? name;
}

function releaseClosure(packages, roots) {
  const result = new Map();
  function visit(name, chain) {
    const pkg = packages.get(name);
    if (!pkg) throw new Error(`Missing release package: ${[...chain, name].join(" -> ")}`);
    if (pkg.private) throw new Error(`Private release dependency: ${[...chain, name].join(" -> ")}`);
    if (result.has(name)) return;
    result.set(name, pkg);
    const next = [...chain, name];
    for (const field of ["dependencies", "optionalDependencies", "peerDependencies"]) {
      for (const [key, spec] of Object.entries(pkg[field] ?? {})) {
        const dependency = dependencyName(key, spec);
        if (packages.has(dependency)) visit(dependency, next);
        else if (String(spec).startsWith("workspace:")) {
          throw new Error(`Missing workspace dependency: ${[...next, dependency].join(" -> ")}`);
        }
      }
    }
    const bundled = pkg.bundledDependencies ?? pkg.bundleDependencies;
    if (Array.isArray(bundled)) {
      for (const name of bundled) if (packages.has(name)) visit(name, next);
    }
  }
  for (const name of roots) visit(name, []);
  return result;
}

function readReleaseGraph(root) {
  const config = JSON.parse(fs.readFileSync(path.join(root, "release-please-config.json"), "utf8"));
  const packages = new Map();
  const packageRoot = path.join(root, "typescript/packages");
  for (const dir of fs.readdirSync(packageRoot, { withFileTypes: true })) {
    const manifest = path.join(packageRoot, dir.name, "package.json");
    if (!dir.isDirectory() || !fs.existsSync(manifest)) continue;
    const pkg = JSON.parse(fs.readFileSync(manifest, "utf8"));
    packages.set(pkg.name, { ...pkg, directory: path.dirname(manifest) });
  }
  const releases = new Set();
  const roots = [];
  for (const [dir, entry] of Object.entries(config.packages)) {
    if (entry["release-type"] !== "node") continue;
    const pkg = JSON.parse(fs.readFileSync(path.join(root, dir, "package.json"), "utf8"));
    if (entry["package-name"] !== pkg.name) throw new Error(`Release name mismatch: ${dir}`);
    releases.add(pkg.name);
    // Held private packages are version-tracked, but never npm publish roots.
    if (!pkg.private) roots.push(pkg.name);
  }
  const closure = releaseClosure(packages, roots);
  for (const name of closure.keys()) {
    if (!releases.has(name)) throw new Error(`Public dependency lacks release metadata: ${name}`);
  }
  return { packages, roots, closure };
}

module.exports = { releaseClosure, readReleaseGraph };
