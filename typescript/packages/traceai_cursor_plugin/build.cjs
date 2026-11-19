const esbuild = require("esbuild");
// Self-contained distributions, never executable references into an ephemeral npx cache.
Promise.all(
  ["hook", "observer", "cloud-hook", "cloud-runtime"].map((name) =>
    esbuild
      .build({
        entryPoints: [`src/${name === "observer" ? "runtime" : name}.ts`],
        outfile: `runtime/${name}.cjs`,
        bundle: true,
        platform: "node",
        target: "node22",
        format: "cjs",
        legalComments: "linked",
        metafile: true,
        plugins:
          (name === "hook" || name === "cloud-hook")
            ? [
                {
                  name: "terminal-only-observer",
                  setup(build) {
                    build.onResolve({ filter: /^\.\/(?:cloud-runtime|runtime)\.js$/ }, () => ({
                      path: name === "hook" ? "./observer.cjs" : "./cloud-runtime.cjs",
                      external: true,
                    }));
                  },
                },
              ]
            : [],
      })
      .then((result) =>
        require("node:fs").writeFileSync(
          `runtime/${name}.meta.json`,
          JSON.stringify(result.metafile),
        ),
      ),
  ),
).catch(() => {
  process.exitCode = 1;
});
