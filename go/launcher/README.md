# traceai launcher

A single, fail-open launcher in front of the command you already use. The importable
package is `github.com/future-agi/traceAI/go/launcher`; `cmd/traceai` is a thin wrapper.
It is also the implementation an embedding CLI can call for `fi run`.

```sh
export FI_API_KEY='...'
export FI_SECRET_KEY='...'
traceai run -- python app.py
traceai run -- npm start
traceai run -- java -jar app.jar
traceai run -- dotnet MyApp.dll
traceai run -- go run .
traceai run -- bundle exec rails server
traceai run -- make serve
```

## Install and build

From this module's directory, with Go 1.26 or an auto-download-capable Go toolchain:

```sh
GOTOOLCHAIN=auto go install ./cmd/traceai  # slim; installs into GOBIN / GOPATH/bin
make slim                              # bin/traceai
make full                              # bin/traceai-full
```

`make full` copies **existing** Java/.NET/Node build artifacts and Go agent source
from this checkout before building the launcher. It does not rebuild SDKs. All four
payloads are required; Ruby `lib` is included when `ruby/lib/traceai/auto.rb` exists.
Ruby's compatible OpenTelemetry gem dependencies still need to be installed in the
application's Ruby. Python always uses the application's interpreter and its installed
`fi-instrumentation-otel`; the launcher does not embed a Python environment.

`make full` needs Python 3 for the staging script. Go sources are staged with a
`.source` suffix and restored on extraction: Go's embed directive otherwise silently
excludes nested modules containing `go.mod`. The full-payload test guards this case.
Staging prunes hidden SDK scratch/test-result directories, vendor/node_modules,
testdata/commands, symlinks and nested modules before walking production Go source;
module caches created by SDK tests must never enter the executable payload.
Artifacts under `embedded/`, build binaries, and scratch files are ignored by Git;
a fresh full build requires staging from the sibling SDKs. Finish sibling SDK builds
before staging; `make full` does not turn changed Java/.NET/Node source into new
artifacts. The opt-in `TestFullStagingMatchesCurrentAgents` rejects a full binary
whose embedded bytes differ from the current prebuilt SDK artifacts or Go/Ruby
sources. Keep those inputs stable through staging and the full suite.

No public launcher release URL or registry publication is assumed by these instructions.

## Commands

| Command | Behaviour |
| --- | --- |
| `traceai run [flags] -- PROGRAM [ARGS...]` | Universal, merge-only runtime injection. Unix replaces itself with the program, retaining PID, streams, working directory, arguments, signal semantics and exit status. Windows starts and waits for a child sharing its console. |
| `traceai run --dry-run -- PROGRAM` | Print a masked JSON plan/configuration and execute nothing. Arguments are deliberately not printed because they can be secrets. |
| `traceai env [--format sh\|fish\|powershell\|dockerfile\|json] [RUNTIME...]` | Print injection only, optionally filtered to `python`, `node`, `java`/`jvm`, `dotnet`, `ruby`, or `go`. Defaults to all. It cannot add a Go build overlay without a command. |
| `traceai doctor [--json] [--offline]` | Detect manifest languages, probe Python/Node/Ruby installation, report cached/embedded agent paths and masked configuration, and test collector acceptance. `--offline` skips the network but still checks keys and local availability. Availability is not proof of runtime/dependency compatibility. |
| `traceai init [--yes]` | Prompt for a project name, or use the directory name with `--yes`; create `futureagi.toml` without overwriting anything; print install advice and example run commands. It does not install dependencies implicitly. Non-interactive input requires `--yes`. |
| `traceai install python` | Use the active/project `.venv` interpreter (`TRACEAI_PYTHON` can override) with `-m pip install fi-instrumentation-otel`; prefer `uv pip --python` when `uv.lock` and `uv` are present. |
| `traceai install node` | Add `@traceai/auto` using pnpm/yarn/bun/npm selected from project lockfiles. This is an explicit project mutation. |
| `traceai install ruby` | Run `gem install traceai-auto` in the inherited Ruby environment. Bundler-managed projects may instead add the gem to their own Gemfile. |
| `traceai install java\|dotnet\|go` | Install an embedded payload into the cache. A slim build needs `--from` or, for Java/.NET, an explicit release URL. |
| `traceai install RUNTIME --from PATH` | Cache a trusted local jar for Java; use a complete payload directory for .NET, Node, Go or Ruby (`lib`, containing `traceai/auto.rb`). Symlinks and traversal paths are rejected. No downloaded code is executed. |
| `traceai install java\|dotnet --url HTTPS_URL --sha256 DIGEST` | Explicit, checksum-verified release download. Java expects a jar; .NET expects a zip with the startup DLL and dependencies at its root. HTTPS only; bounded download, unpacking and redirects. Redirect requests never send `Referer`, including same-origin and later hops, so signed source URLs are not disclosed to redirect targets. Obtain the digest independently from a trusted release publisher. There is no guessed default URL. |
| `traceai install all` | Explicitly install Python, Node, Ruby and all available embedded payloads. Attempts every runtime and reports failure if any installation fails. |
| `traceai setup go [DIR]` | Persist a blank-import file in each uninstrumented main package and run `go get` for the agent. Never overwrite an existing file. Failed setup removes only newly created imports; inspect `go.mod`/`go.sum` for changes made by Go itself. Commit successful setup changes. |
| `traceai validate [--json] [--offline]` | Validate configuration and keys; by default submit an empty OTLP test export to `/tracer/v1/traces`. Only a 2xx response counts as acceptance. Redirects are not followed with credentials; timeout is five seconds. Offline success does not establish collector acceptance. |
| `traceai version` | Launcher version, full/slim flavour, and embedded agent staging versions. Go is explicitly an unversioned source snapshot; staging metadata is not a signed artifact attestation. |

Run flags map directly to the agent environment:

| Flag | Variable |
| --- | --- |
| `--project` | `FI_PROJECT_NAME` |
| `--project-type observe\|experiment` | `FI_PROJECT_TYPE` |
| `--base-url` | `FI_BASE_URL` |
| `--disable` | `FI_DISABLED_INSTRUMENTATIONS` |
| `--only` | `FI_INSTRUMENTATIONS` |
| `--sample-rate` | `FI_SAMPLE_RATE` |
| `--debug` | `FI_AUTO_DEBUG=true`, `TRACEAI_DEBUG=true` |
| `--go-inject` | Opt in to experimental Go workspace/overlay injection for this command; alternatively set `TRACEAI_GO_INJECT=1`. Off by default. |

`run` returns the application's own exit code (including 3), or 127 for a missing
program / 126 for a non-executable program. Other commands return 0 for success,
1 for a detected problem, and 2 for usage errors.

## Injection table

Every runtime is injected when its agent/shim is available: detection does not limit
injection. A shell, Make, npm, Maven or Gradle wrapper passes the variables down to
its supported-runtime children. Every child gets `TRACEAI_LAUNCHER=<version>`.

| Runtime | Injection and resolution |
| --- | --- |
| Python | Prepend the cached shim directory to `PYTHONPATH`. `sitecustomize.py` starts `fi_instrumentation.auto._bootstrap` inside its fail-open guard, then chains the application's own `sitecustomize` through the interpreter's meta-path finders. Do not set `FI_AUTO_INSTRUMENT=true`: that would activate an installed `.pth` before the guarded shim. An explicit inherited value is preserved. Missing/broken instrumentation is caught. With `TRACEAI_DEBUG=true`, a missing package emits one value-free warning; interpreter paths are deliberately omitted because they may contain credentials. |
| Node | Prepend `--require "<shim>/preload.cjs"` to `NODE_OPTIONS`, before ordinary application preloads (which may themselves make requests). A Yarn Plug'n'Play `.pnp.js`/`.pnp.cjs` preload in `NODE_OPTIONS` or command-line `-r` is a resolution prerequisite: initialize synchronously just after it returns, before subsequent application preloads, not after all preloads. Resolve the application's own `@traceai/auto/register` from its current directory first; otherwise load the cached/embedded Node bundle. Only agent failures are caught; application loader errors and return values are preserved. The agent controls version support and ESM-hook support; older Node versions can have CommonJS-only coverage. |
| JVM | Prepend a correctly quoted `-javaagent:<jar>` to `JAVA_TOOL_OPTIONS`. Resolve `TRACEAI_JAVA_AGENT`, then an explicitly installed cache payload, then the embedded jar. A missing explicit override does not silently select another agent. If the resolved path contains a known credential, skip injection with a value-free warning: the JVM prints this variable before the agent starts, even when `OTEL_SDK_DISABLED=true`. Inherited options remain unchanged. |
| .NET | Prepend the startup-hook DLL to `DOTNET_STARTUP_HOOKS` with the platform path separator. Resolve `TRACEAI_DOTNET_AGENT`, cache, then embedded payload. Full builds stage the **net8.0** hook and its complete dependency directory, compatible with .NET 8 and 10. Staging validates the dependency manifest's net8 target and refuses to substitute a net10-only hook when net8 is missing. Both runtime apps are exercised through `traceai run` against a local provider/OTLP collector. |
| Ruby | Prepend `-I<shimdir> -rtraceai_launcher_<hash>` to `RUBYOPT`; for paths with whitespace, put the directory in `RUBYLIB` instead. The small shim requires the program's `traceai/auto` first, then tries `TRACEAI_RUBY_AGENT` / cache / embedded `lib`. Every exception, including missing/incomplete gem code, is rescued. |
| Go build | **Off by default.** Direct `go run/build/test/install` commands run with their original `GOFLAGS`/`GOWORK` and no `go env/list` discovery; print one `traceai setup go` hint (not repeated by nested launchers). With experimental `--go-inject` or `TRACEAI_GO_INJECT=1`, generate a cache-only workspace and overlay adding blank imports to uninstrumented target mains/test packages. Existing `GOFLAGS` overlays are merged; discovery is readonly, capped at 8 MiB and ten seconds per probe. No project source, `go.mod`, or `go.sum` is changed by injection. |
| Go binary | Inspect build information. An already-linked agent needs no injection; an uninstrumented Go binary gets a warning pointing to `traceai setup go`. No binary patching. |

Existing runtime variables are preserved and injection is idempotent across nested
launches. Paths with spaces are tested. Node option paths use Node's own literal
quoting, including tabs, Unicode, apostrophes, double quotes and backslashes;
Ruby option detection uses Ruby's whitespace rules (apostrophes are not quotes).
Python chaining preserves the application's imported `sitecustomize` module and
its exported helpers, not only its initialization side effects. Application startup
exceptions retain Python's native behavior, including `SystemExit` and interrupts;
application finder/loader errors also propagate. Only instrumentation and shim-internal
setup failures are swallowed. Different nested cache roots
bootstrap once and traverse each shim at most once before reaching the application. Overrides also exist as
`TRACEAI_NODE_AGENT`, `TRACEAI_GO_AGENT`, and `TRACEAI_RUBY_AGENT`; directory overrides
must contain the runtime's entry point. Override entry points must be nonempty,
readable regular files; FIFOs/devices/sockets are rejected before opening so they
cannot block unrelated application startup.

## Go: safe default and experimental injection

The default is detection/advice only: existing Go binaries are inspected through build
information, and direct Go build commands receive a setup hint. No toolchain subprocess
is launched for detection, and no build configuration is rewritten. For persistent,
reviewable instrumentation use `traceai setup go`, then build/run normally.

To try cache-only source injection with a full launcher:

```sh
traceai run --go-inject -- go run .
TRACEAI_GO_INJECT=1 traceai run -- go test ./...
```

Only the exact environment value `1` opts in. These flags do not instrument Go builds
hidden inside shell/Make wrappers; use persistent setup there. The following limits
apply **only to experimental injection**, not the default Go path.

Existing `GOWORK` settings/workspaces are preserved rather than
replaced; modules with a `vendor` directory are left untouched so patched/offline
vendored dependencies never silently change. Run `traceai setup go` and re-vendor
when persistent instrumentation is wanted. Explicit `.go` filename invocations,
versioned install targets, command-line overlays/modfiles, and module flags
incompatible with workspaces (including trailing `go test . -mod=mod` flags) are
left untouched. Application arguments to `go run` and test arguments after `-args`
are not interpreted as build flags. Existing `GOFLAGS` spelling, including literal
backslashes and Go's non-shell quoting, is retained; the effective `GOENV`-persisted
value is used when the environment value is absent or empty. The merged overlay is
appended as the final flag. Valid flag terminators (`go run -- ./cmd/app`) still
select the named package. A test-only agent import does not suppress production injection.
Before activating an overlay, readonly module-graph probes compare the application's
original selections with the proposed workspace. Any changed version/replacement,
missing selection or failed probe declines injection with a warning. Newly added
agent-only dependencies are allowed; application dependencies are never upgraded
implicitly. Already-instrumented packages are skipped individually, not globally:
selecting one traced executable does not suppress other uninstrumented targets.
Go test profiling flags (`-cpuprofile`, `-memprofile`, `-blockprofile`, `-mutexprofile`,
`-trace`) consume their filename rather than treating it as a package.
Use package targets (`go run .`) or `traceai setup go`.
Wrappers that compile Go internally need persistent setup;
Go build injection needs the agent's dependencies/toolchain available, and Go may
fetch them during the build. Very short-lived Go applications must call the agent's
bounded shutdown/flush explicitly; there is no Go atexit hook.

## Configuration

Agents use flags > environment > `futureagi.toml` > defaults. `FI_CONFIG_FILE` selects
another file. The launcher's diagnostics parse the shared flat TOML subset; the
runtime agents own initialization and wire/library capture. Table sections (including
all their keys) are ignored, never promoted to root-level collector configuration.
Default collector:
`https://api.futureagi.com`; project type: `observe`; sample rate: `1.0`.

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["wire", "openai"]
wire_capture = true
sample_rate = 0.25
debug = false
```

## Privacy

Keep `FI_API_KEY` and `FI_SECRET_KEY` in the environment or a secret manager, never
in the committed config. Dry-run, env, doctor and validate mask credential fields
and known secret values echoed in other fields, using the **complete inherited
environment** (including non-FI credentials such as `AWS_SECRET_ACCESS_KEY` and
`HTTP_AUTHORIZATION` / proxy authorization headers) as redaction context. Standard
Bearer/Basic credentials are masked both with and without their scheme. Raw values,
Go/JSON diagnostic escapes and Node's distinct option escapes are recognized before
output serialization; quoting a credential is not redaction. Opaque `GOFLAGS` (including `GOENV`-persisted values), JVM option
variables, `NODE_OPTIONS` and `RUBYOPT` are masked entirely in diagnostic output
(including `env --format json`) when they contain inherited application options:
embedded credentials may be unknown to the launcher. Executable `env` formats
(sh, fish, PowerShell, Dockerfile) emit only launcher-owned injection literals and
references to the inherited variables, preserving their values without printing them.
Merge-capable variables are referenced even when absent/empty during generation, so
settings supplied later at script evaluation are retained.
An injection literal containing a known credential is omitted with a warning rather
than replaced with an executable redaction placeholder. Display masking never changes
the child's actual flags. Launcher errors
and flag-parser diagnostics are redacted too,
including quoted/escaped credential values. `init` masks known credentials in its
project-name prompt and interpreter-path advice **before** shell quoting. Redacted
example paths are display-only; use the original trusted interpreter path locally.
Collector diagnostics do not print
server bodies or network error URLs. `run` performs no collector request or dependency
installation. Shim failures are caught; cache failures produce one combined warning
and run without the unavailable injection. The launcher cannot sandbox an explicitly
supplied, incompatible or malicious JVM/.NET agent: only use trusted artifacts built
for the target runtime. Regular application output is deliberately not rewritten.

Agent capture privacy (content hiding, credential/signed-URL scrubbing, inline-media
omission and bounded streaming) is defined in
`../../spec/auto-instrumentation/README.md`; the launcher does not duplicate its wire
parser. `OTEL_SDK_DISABLED=true` disables agents. `FI_WIRE_CAPTURE=false` or
`FI_DISABLED_INSTRUMENTATIONS=wire` disables wire capture. Instrumentation is not a
substitute for checking the runtime agent's own verified transport coverage.

## Cache integrity

Cache location: `TRACEAI_CACHE_DIR`, otherwise the OS user cache directory plus
`traceai/<version>`. Shims and payloads are content-addressed and atomically written;
per-runtime cache pointers are published last. Cache hits verify **every payload
file** against the directory's content hash, not just the entry point. Missing,
modified, unexpected or symlinked files invalidate the hit. Full builds repair
missing/modified bytes and prune unexpected files/directories from embedded payloads;
explicit reinstallation repairs the same content-addressed directory, including
extra-file corruption. Slim builds skip invalid cached agents until reinstalled. Integrity verification is bounded to 256 MiB
and 4,096 files per payload. Explicit `TRACEAI_*_AGENT` overrides remain trusted
caller-supplied artifacts rather than signed/validated release attestations.

The launcher creates cache directories with mode `0700`. On Unix an existing root
or payload directory must be private (no group/other permissions). Every intermediate
Go source/workspace cache directory receives the same checks. Symlinked or shared
directories, including mode `0777`, are refused without chmod or Go injection.
Use a dedicated `0700` directory, not an existing shared temporary directory.
Windows mode bits are not an ACL check; Windows cache ACL behaviour is unverified.

## Docker and Kubernetes with `traceai env`

Generate paths **inside the target image**, not on a developer host. The image must
include the same binary, agent dependencies and cache at those paths. For example,
a Python image can bake a secret-free injection script:

```dockerfile
# Base image already provides Python and fi-instrumentation-otel.
COPY traceai /usr/local/bin/traceai
ENV TRACEAI_CACHE_DIR=/opt/traceai/cache
RUN mkdir -p /opt/traceai && traceai env --format sh python > /opt/traceai/injection.sh
WORKDIR /app
COPY . /app
ENTRYPOINT ["/bin/sh", "-c", ". /opt/traceai/injection.sh; exec \"$@\"", "--"]
CMD ["python", "app.py"]
```

Pass credentials at runtime. A generated **sh startup script** merges runtime
variables even when they were absent during image construction. In contrast,
`traceai env --format dockerfile python node` prints `ENV` directives whose references
are evaluated by the image builder, not container startup. Container runtime overrides
can replace image `ENV` values; use the sh entrypoint above or `traceai run` when
runtime merging is required. `--format json` provides a masked diagnostic map
for generating Kubernetes `env` entries. Do not bake masked placeholders as keys.
A pod using the image above can retain its entrypoint and supply a Secret:

```yaml
containers:
  - name: app
    image: your-app:tested
    args: ["python", "app.py"]
    envFrom:
      - secretRef:
          name: futureagi-credentials
```

When using a read-only root filesystem, bake the cache during image construction
or mount a writable cache; an unwritable cache disables injection without stopping
the command. These Docker/Kubernetes recipes are examples, not deployment-tested
manifests. `traceai run -- ...` is usually simpler if changing the entrypoint is allowed.

## Verification

```sh
make test  # native vet, Windows-target vet, full unit/command suite
make full  # stage existing artifacts and compile the full launcher
make e2e   # serialized real-runtime matrix, local provider and OTLP collector
# Include full-payload extraction assertions and optional Ruby shim tests:
TMPDIR="$PWD/.scratch" TRACEAI_E2E=1 sh scripts/host-step.sh heavy \
  go test -tags embedagents -count=1 -v ./...
```

On the shared development laptop, Make targets check the owner's host-status script
before builds/tests, wait 60 seconds when memory/load exceeds budget, and take the
shared heavy-step lock for whole test suites. All test scratch output is on the
external volume. No sibling SDK agent is rebuilt by these tests; .NET applications
use prebuilt binaries, without starting an SDK build server. Outside that laptop
these guards are inert.

The macOS real-runtime matrix covers Python requests, Node project-install precedence
and the standalone embedded bundle, Java 17 `-jar` with a spaced agent path, the same
embedded net8 startup hook exporting spans from **both .NET 8 and .NET 10 apps**,
experimental Go package injection without project edits, native/default Go
run/build/test/install parity, explicit flag opt-in, linked/unlinked Go binary
detection, Ruby 3.2 Net::HTTP fallback loading, nested launchers, a shell wrapper
starting Python and Node, stdin/stdout/stderr/argument/cwd preservation, missing
agents/keys with exit 3, exit 127/126, SIGTERM identity, Python sitecustomize chaining
and secret-masked dry-run. Additional real-Ruby shim checks verify project precedence
and missing/broken-agent fail-open behaviour. The untagged unit suite intentionally
skips opt-in real-runtime checks; use `TRACEAI_E2E=1` with `embedagents` for the full
matrix, including SDK-payload freshness assertions.

Verified **2026-10-30** under the shared host guard: the complete embedded/E2E suite
passes **102 top-level tests / 360 leaf cases**, with zero failures or executed-test
skips. Native and Windows-target vet pass. Untagged unit and race runs each pass
**79 top-level tests / 271 leaf cases**, with 20 intentional opt-in runtime-group
skips; no race warnings are reported. Restage and rerun after any sibling SDK
artifact/source changes; freshness checks intentionally reject mixed revisions.

`ship_regressions_unix_test.go` covers exact Go opt-in gating and no default
toolchain discovery, 15 direct/directory/symlink FIFO override cases, absent/empty
late variables in all four env renderers (executing sh), a credential-named Python
interpreter, mixed Go build targets/profiling, and generated-cache/nested-module
exclusion from staging. The runtime matrix also exercises default Go commands and
both opt-in mechanisms through the actual CLI.

`verifier_regressions_test.go` checks checksum-valid release installs through local
HTTPS same-origin, cross-origin and multi-hop redirects: no request may send
`Referer` or collector authentication headers. `verifier_regressions_unix_test.go`
checks `init` with real credential-named interpreter symlinks (FI keys, provider
keys, apostrophe-containing keys, virtualenv discovery and a normal-path control),
plus a credential-bearing project-name prompt. `privacy_regressions_test.go` and
`privacy_regressions_unix_test.go` add raw/quoted/backslash/mixed Node credential
paths through dry-run, JSON and all executable env renderers (executing sh), auth
header/token redaction through dry-run/doctor/validate/init, and fail-open JVM path
privacy. The full CLI matrix compares native `java -version` with embedded and
explicit credential-bearing agent paths, both enabled and SDK-disabled. Separate bounded CLI hand checks
reconfirm FIFO fail-open startup, late Python/Node environment merging, value-free
Python shim diagnostics and redacted `init` advice.

Regression processes cover ten Go build/flag/vendoring scenarios (including
persisted/empty `GOFLAGS` and `go run --`), five Node literal-path scenarios,
offline Yarn Plug'n'Play project-agent discovery, and nested Ruby injection with
an apostrophe in its path. Python startup checks compare native and injected
application exceptions (including a custom loader), preserve helper imports across
one/two/three different cache roots, and retain fail-open instrumentation failures.
Go cache regressions reject symlinked/shared intermediate source and workspace
directories without touching their targets or changing the application's flags.
Round-three checks cover embedded startup hooks on both .NET 8 and 10, ignored TOML
tables with separate collector/provider listeners, opaque runtime-option masking,
Go MVS upgrades/replacement redirects (including a pinned test-only agent) and graph
probe failures, a real installed Python `.pth` raising `SystemExit`, virtual meta-path
`sitecustomize` finders, command-line Yarn PnP preloads including synchronous eval,
caller-specific PATH execute permissions, and concurrent extra-file/directory cache
repair without following extra symlinks. These regressions synchronize on process
exit, completed HTTP requests, and explicit channels/barriers, never sleeps.
Full-payload extraction tests cover Java, .NET, Node and restored Go source filenames,
and check Ruby when staged; cache-repair tests delete Go `auto.go` and tamper with
the Node entry, while integrity tests check dependencies, extra files, symlinks,
oversized data and shared-cache fail-open behaviour. Local HTTPS fixtures verify checksum-gated Java/.NET
release installation and failure atomicity; no public release endpoint was used.
Windows currently has compile/vet coverage only: console control forwarding,
`.bat`/`.cmd` execution and runtime injection have not been exercised on a Windows machine. Package-manager
installation tests use local stand-ins; live registry installs, public release
hosting, Docker/Kubernetes deployments, Windows execution, and compatibility beyond
the locally installed runtimes are not claimed.

## Known limitations

Dated **2026-10-30** (round-three/four OTHER findings and verification boundaries):

- **Same-version embedded payload replacement:** an existing, integrity-valid cache
  pointer takes precedence over embedded agents, including pointers created by
  automatic extraction. Replacing/rebuilding a full launcher without changing its
  version can therefore keep using the previously extracted agent even when the
  new binary embeds different bytes. Automatic extraction is not distinguished
  from an explicit installed pin. For same-version replacements, select a fresh
  dedicated `TRACEAI_CACHE_DIR` (created private, mode `0700`), or explicitly
  reinstall the desired complete runtime payload with `traceai install RUNTIME
  --from PATH`. Integrity checking protects cached bytes; it does not establish
  freshness relative to a replacement binary.
- **Redundant Yarn PnP preload spellings:** if the same `.pnp.js`/`.pnp.cjs` loader
  appears more than once with different spellings across `NODE_OPTIONS` and CLI
  `-r`, the shim waits for the last spelling. An application preload between those
  copies can run before instrumentation (the main app is instrumented). Keep one
  PnP preload, before application preloads; ordinary/single-loader PnP ordering is
  regression-tested. This is not a claim of complete coverage for exotic loader stacks.
- **Persistent Go setup through symlinks:** `traceai setup go <symlink-dir>` can
  silently skip mains because Go reports physical module paths. Invoke setup with
  the physical module directory (`cd -P` / `pwd -P`) instead. Experimental overlay
  injection already canonicalizes its root. Setup is explicit; default `run` does
  not mutate or discover source packages.
- **Experimental Go scope:** injection is off unless `--go-inject` or exactly
  `TRACEAI_GO_INJECT=1` is supplied. Consequently the prior mixed-target/profiling
  injection findings are moot on the default path; both are also fixed/tested in
  the opt-in mechanism. Unsupported/unknown flag spellings may still decline
  experimental discovery. Workspaces, vendor trees and conflicting dependency
  selections are deliberately preserved rather than forced into instrumentation.
- **Credential-bearing JVM paths (2026-10-30):** Java injection is deliberately
  skipped when the resolved embedded/cache/override jar path contains a known
  inherited credential. HotSpot echoes `JAVA_TOOL_OPTIONS` before agent code runs,
  including when the SDK is disabled. Use a secret-free private `TRACEAI_CACHE_DIR`
  or trusted override path to restore tracing. The launcher neither rewrites the
  application's output nor removes inherited JVM options; existing application
  options/arguments that themselves disclose credentials remain the caller's concern.
- **Deployment/runtime certification:** Windows execution/console/ACL behavior,
  live registry installs and Docker/Kubernetes deployments are not verified.
  Fish/PowerShell/Dockerfile output has rendering assertions; late-variable sh
  merging is executed. Dockerfile `ENV` evaluates at build time, not startup.
  The shared net8 hook is tested on .NET 8/10; older runtimes, NativeAOT and trimmed
  or single-file deployments are outside this launcher matrix. Trusted explicit
  JVM/.NET override compatibility remains the caller's responsibility.
