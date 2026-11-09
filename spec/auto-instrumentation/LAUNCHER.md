# `traceai` launcher contract

One executable in front of any program's start command, in any language:

```bash
export FI_API_KEY=... FI_SECRET_KEY=...
traceai run -- python app.py
traceai run -- uvicorn main:app
traceai run -- node server.js
traceai run -- npm start
traceai run -- java -jar app.jar
traceai run -- ./gradlew bootRun
traceai run -- dotnet MyApp.dll
traceai run -- go run .
traceai run -- bundle exec rails server
traceai run -- ./my-go-service
traceai run -- make serve        # any wrapper: every child process in a supported runtime is traced
```

It is the runner that `fi run -- <cmd>` in the platform CLI uses: the Go package
`github.com/future-agi/traceAI/go/launcher` holds all logic; `cmd/traceai` is a thin `main`.

## Principles

1. **Never break the program.** Any launcher failure (missing agent, unreadable cache, bad
   config) prints one `traceai:` warning on stderr and runs the command exactly as given.
   The program's arguments, stdin, stdout, stderr, working directory, environment (apart
   from the variables listed below), signals and exit code are preserved. On Unix the
   launcher replaces itself with the program (`execve`); on Windows it starts the program,
   forwards Ctrl+C/Ctrl+Break, waits and exits with the program's exit code.
2. **Universal injection.** The launcher does not need to guess the language. For every
   runtime whose agent is available it sets that runtime's injection variable; the variables
   are inert for other runtimes. So `traceai run -- make serve` traces a Python API and the
   Node worker it starts. Detection is used for the report, warnings and Go.
3. **Merge, never clobber.** Existing `NODE_OPTIONS`, `JAVA_TOOL_OPTIONS`,
   `DOTNET_STARTUP_HOOKS`, `PYTHONPATH`, `GOFLAGS` values are kept; the launcher prepends its
   entry once (idempotent when nested: `traceai run -- traceai run -- x` injects once).
4. **Same configuration as the agents.** Flags (`--project`, `--project-type`,
   `--base-url`, `--disable`, `--only`, `--sample-rate`, `--debug`) map to the `FI_*`
   variables in `README.md`; the agents still read the environment and `futureagi.toml`.
   Secrets are never printed (`--dry-run`, `env`, `doctor` mask them).

## Injection table

| Runtime | Variable(s) set | Agent located from (first match) |
| --- | --- | --- |
| Python | `FI_AUTO_INSTRUMENT=true`; `PYTHONPATH=<shims>/python` prepended | The program's own interpreter: the shim `sitecustomize.py` imports `fi_instrumentation.auto._bootstrap` if installed, else does nothing (once, with `TRACEAI_DEBUG`, it says which interpreter lacks `fi-instrumentation-otel`). It chains to the program's own `sitecustomize`. |
| Node | `NODE_OPTIONS=--require "<shims>/node/preload.cjs"` prepended | The preload stub resolves `@traceai/auto/register` from the process's working directory (project install wins), else the embedded bundle `<agents>/node/preload.cjs`. Works on every Node version the agent supports; on Node without `module.register` only CommonJS is patched; older Node runs untouched. |
| JVM | `JAVA_TOOL_OPTIONS=-javaagent:<jar>` prepended | `TRACEAI_JAVA_AGENT`, else the embedded/cached `traceai-javaagent.jar`. Reaches every JVM the program starts (Maven, Gradle and the app they fork). |
| .NET | `DOTNET_STARTUP_HOOKS=<dll>` prepended (platform path-list separator) | `TRACEAI_DOTNET_AGENT`, else the embedded/cached `TraceAI.AutoInstrumentation.dll` directory. |
| Ruby | `RUBYOPT=-r<agent>/traceai/auto` prepended (`-I<agent dir>` too when the gem is not installed) | The `traceai-auto` gem when the program's Ruby can load it, else the embedded copy. |
| Go (build) | `GOWORK` (a generated workspace that adds the agent module) and `GOFLAGS=-overlay=<file>` (adds a blank-import file to each main package being built) | Only for `go run`, `go build`, `go test`, `go install` in a module. The project's `go.mod`/`go.sum` are not modified. If the project already imports the agent, nothing is injected. |
| Go (binary) | none | `debug/buildinfo` of the executable: if it links `github.com/future-agi/traceAI/go/auto` it is reported as traced; otherwise one warning names `traceai setup go`. |

Every process also gets `TRACEAI_LAUNCHER=<version>` so agents can report how they were started.

## Agents and shims on disk

- Shims (tiny, launcher-owned files: Python `sitecustomize.py`, Node `preload.cjs`) are
  embedded in the binary and written to the cache directory
  (`$TRACEAI_CACHE_DIR`, else the OS user cache dir + `/traceai/<version>/`) with an atomic
  rename and a content hash, so concurrent launches are safe.
- Agents (Java jar, .NET startup-hook directory, Node bundle, Go agent module source) are
  embedded in "full" builds (build tag `embedagents`) and extracted the same way; a "slim"
  build finds them in the cache, filled by `traceai install`.

## Commands

| Command | Does |
| --- | --- |
| `traceai run [flags] -- <cmd>` | Inject and run. `--dry-run` prints what would be set (masked) and runs nothing. |
| `traceai env [--format sh\|fish\|powershell\|dockerfile\|json] [<runtime>...]` | Print the injection for Dockerfiles, Kubernetes manifests, CI and process managers. |
| `traceai doctor [--json]` | Detect the project's languages (from `pyproject.toml`, `requirements*.txt`, `package.json`, `go.mod`, `*.csproj`, `pom.xml`, `build.gradle*`), whether each agent is available, the resolved configuration (masked), and whether the collector accepts a test export. |
| `traceai init [--yes]` | Guided setup: detect, write `futureagi.toml`, install or explain each missing agent, print the exact run command. |
| `traceai install <python\|node\|java\|dotnet\|all>` | Python: `<project interpreter> -m pip install fi-instrumentation-otel` (or `uv pip`). Node: add `@traceai/auto` with the project's package manager. Java/.NET: place the agent in the cache (from the embedded copy, `--from <path>`, or the release URL). |
| `traceai setup go [dir]` | Add the blank-import file to each main package and `go get` the agent: for teams that prefer code over `traceai run`. |
| `traceai validate` | Check configuration and keys against the collector; non-zero exit on a problem. |
| `traceai version` | Version, build flavour (full/slim) and embedded agent versions. |

Exit codes: the program's own code for `run`; otherwise 0 ok, 1 problem found, 2 usage error.
