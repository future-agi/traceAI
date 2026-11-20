# Zero-code tracing for .NET

`TraceAI.AutoInstrumentation` is a .NET startup hook. You point the
`DOTNET_STARTUP_HOOKS` environment variable at it, and it traces AI calls in your
application without code changes. It has two capture layers: the built-in GenAI
`ActivitySource` telemetry of supported libraries, and protocol-aware `HttpClient`
capture of calls to AI providers. Spans go to Future AGI. Your application's own
OpenTelemetry setup keeps working as before. The hook is not a CLR profiler, so it
does not capture every possible AI call; see [Limits](#limits).

## Requirements

- An application that runs on .NET 8 LTS or .NET 10. One published hook directory
  serves both. The application needs only its own runtime.
- A .NET 10 SDK to build the hook from source.
- A Future AGI API key and secret key.

| Application runtime | Supported |
| --- | --- |
| .NET 8 LTS | Yes |
| .NET 10 | Yes |
| .NET 9, .NET Framework, .NET 6/7 or earlier | No |
| NativeAOT, trimmed or single-file deployments, browser/WASM | No |

## Install

The startup hook is not on NuGet yet; build it from this repository as shown below.

From the repository root, publish the hook with a .NET 10 SDK:

```sh
dotnet publish csharp/src/TraceAI.AutoInstrumentation/TraceAI.AutoInstrumentation.csproj \
  -c Release -o csharp/artifacts/auto
```

This writes the hook directory to `csharp/artifacts/auto`. Keep **all** files in
that directory together, not only `TraceAI.AutoInstrumentation.dll`. Deploy the whole
directory with your application.

Once the package is on NuGet, `dotnet add package TraceAI.AutoInstrumentation`
unpacks the same directory into the NuGet global packages folder under
`~/.nuget/packages/traceai.autoinstrumentation/<version>/tools/net8.0/any/`. The
package holds no library to reference.

## Turn it on

Set `DOTNET_STARTUP_HOOKS` to the absolute path of the hook DLL on the command that
starts your application:

```sh
FI_API_KEY='...' FI_SECRET_KEY='...' \
DOTNET_STARTUP_HOOKS="$PWD/csharp/artifacts/auto/TraceAI.AutoInstrumentation.dll" \
  dotnet /absolute/path/to/YourApplication.dll
```

- Tracing starts only when `DOTNET_STARTUP_HOOKS` is set.
- Set the variables on the application's command line, as above. Do not `export`
  `DOTNET_STARTUP_HOOKS` in your shell: it would also attach the hook to later
  `dotnet build`, `dotnet test` or `dotnet run` commands.
- Child processes started by the application inherit the variable, and with it the
  hook, unless the application removes it from their environment.
- To run several startup hooks, separate the paths with the OS path separator
  (`:` on Unix, `;` on Windows).
- On Windows, set the same environment variables in PowerShell.

### ASP.NET Core hosts

`Microsoft.Extensions.AI` and `Microsoft.Agents.AI` emit spans only through their own
OpenTelemetry wrappers, which an application usually adds with `UseOpenTelemetry()`.
In an ASP.NET Core host, the startup hook adds the companion assembly
`TraceAI.AutoInstrumentation.HostingStartup` to `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES`.
It appends to any existing value and keeps it. Before the host builds its container,
that assembly wraps every registered `IChatClient`, `IEmbeddingGenerator<,>` and
`AIAgent` in the library's own wrapper. The application needs no tracing code.

- A registration that already carries the wrapper (the application called
  `UseOpenTelemetry()`) is left as it is, so each call produces one span.
- Wrapping is off when the agent is not running (`OTEL_SDK_DISABLED`, missing
  credentials). It is off per kind when `extensions_ai` or `agents` is disabled.
- Open generic registrations and keyed services are left as registered.
- Incoming web request spans are sent only for requests that made an AI call. Set
  `FI_DISABLED_INSTRUMENTATIONS=aspnetcore` to send none.

## Send traces to Future AGI

| Environment variable | Default / meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Both are required for the Future AGI collector. |
| `FI_BASE_URL` | `https://api.futureagi.com`. Traces are sent as OTLP HTTP/protobuf to `/tracer/v1/traces`. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the working directory name, then `DEFAULT_PROJECT_NAME`. |
| `FI_PROJECT_TYPE` | `observe`; or `experiment`. |
| `FI_CONFIG_FILE` | `./futureagi.toml`. |
| `FI_SAMPLE_RATE` | When set, a parent-based ratio sampler clamped to 0–1. It does not change the application's own sampler. |
| `OTEL_SDK_DISABLED` | `true` turns the agent off. |

`FI_API_KEY` and `FI_SECRET_KEY` resolve as one pair, from the environment or from
the config file. An incomplete or blank pair is ignored with one warning that names
the source and the missing half. Keep credentials out of committed files.

Settings can also live in `./futureagi.toml`. Precedence is environment, then file,
then defaults. The file supports a flat subset of TOML:

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["openai", "semantic_kernel", "aspnetcore", "wire"]
disabled_instrumentations = []
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 0.25
debug = false
```

## Hide prompt and response content

The `HttpClient` layer captures content by default. Use these switches to hide it:

| Environment variable | Hides |
| --- | --- |
| `FI_HIDE_INPUTS` | All input content |
| `FI_HIDE_OUTPUTS` | All output content |
| `FI_HIDE_INPUT_MESSAGES` | Input messages |
| `FI_HIDE_OUTPUT_MESSAGES` | Output messages |
| `FI_HIDE_INPUT_TEXT` | Input text |
| `FI_HIDE_OUTPUT_TEXT` | Output text |
| `FI_HIDE_INPUT_IMAGES` | Input images |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | Invocation parameters |
| `FI_HIDE_EMBEDDING_VECTORS` | Embedding vectors |
| `FI_HIDE_ERROR_DETAILS` | Error messages and stack traces |

- `true`, `1`, `yes` or `on` hides; `false`, `0`, `no` or `off` shows. Values are
  trimmed and case-insensitive. A blank value counts as not set.
- Any other value hides that content and prints one warning naming the variable.
- The lower-case file equivalents work too, for example `hide_inputs = true`.
- Error details are hidden whenever any input or output hide switch is on. An
  explicit `FI_HIDE_ERROR_DETAILS` overrides that.
- `FI_WIRE_CAPTURE=false` turns off the `HttpClient` capture layer (`wire`). Only
  `true`, `1` or `yes` keep it on.

**Library content in ASP.NET Core hosts.** For the chat clients and agents the
hosting startup wraps, prompt and response content stays off unless
`FI_AUTO_CAPTURE_CONTENT=true` and no hide setting is on. The libraries' content
switch puts content on the span for every listener the application has, not only
this agent. Content also stays off when `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`
is set to anything other than `true`.

## What gets traced

| Integration name | What it covers |
| --- | --- |
| `openai` | OpenAI .NET client (`OpenAI.*`) |
| `extensions_ai` | `Microsoft.Extensions.AI`; registered chat clients and embedding generators are wrapped in ASP.NET Core hosts |
| `semantic_kernel` | Semantic Kernel (`Microsoft.SemanticKernel*`) |
| `azure_inference` | Azure AI Inference (`Azure.AI.Inference*`) |
| `azure_openai` | Azure OpenAI (`Azure.AI.OpenAI*`); modern clients also emit `OpenAI.*`, so enable `openai` too |
| `agents` | Microsoft Agent Framework (`Microsoft.Agents.AI*`); registered agents are wrapped in ASP.NET Core hosts |
| `aspnetcore` | Incoming web requests that made an AI call |
| `wire` | `HttpClient` calls to AI providers |

The `wire` layer reads the shared protocol catalog and implements all 25 of its
formats, including chat/completions and Responses, embeddings, rerank, moderation,
image JSON, speech-to-text and text-to-speech, and the Anthropic, Gemini, Cohere,
Bedrock, Ollama, Deepgram, ElevenLabs, AssemblyAI and Cartesia formats.

Outside ASP.NET Core hosts, `Microsoft.Extensions.AI` and agent framework calls need
the library's own wrapper (`UseOpenTelemetry()`); the status shows them as `manual`.

## Turn integrations off or choose them

| Environment variable | Default / meaning |
| --- | --- |
| `FI_INSTRUMENTATIONS` | All integrations; otherwise a comma-separated allowlist of integration names. |
| `FI_DISABLED_INSTRUMENTATIONS` | Empty; a comma-separated denylist. It takes priority over the allowlist. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` (4 MiB), for request and response capture separately. |

Example: trace only OpenAI and HTTP capture, with no web request spans:

```sh
FI_INSTRUMENTATIONS=openai,wire FI_DISABLED_INSTRUMENTATIONS=aspnetcore \
FI_API_KEY='...' FI_SECRET_KEY='...' \
DOTNET_STARTUP_HOOKS="$PWD/csharp/artifacts/auto/TraceAI.AutoInstrumentation.dll" \
  dotnet /absolute/path/to/YourApplication.dll
```

Disabling an integration after the agent has started is not supported. Settings are
read once per process; restart the process to change them.

## Check that it works

Start the application with `FI_AUTO_DEBUG=true`. At startup the hook prints its
configuration, with secret values masked, and a status for each integration:

| Status | Meaning |
| --- | --- |
| `instrumented` | A listener attached to the library's source. With `auto (web host)`, the hosting startup wrapped a registered service. |
| `pending` | The library is in the application, but its supported source has not started yet. |
| `not_installed` | The library is not in the application. |
| `disabled` | Turned off by your settings, or the agent is not running (`OTEL_SDK_DISABLED`, missing credentials). |
| `manual` | Needs the library's own wrapper in code. |
| `failed` | Initialization failed. |

The hook makes no network calls at startup. If the collector rejects an export, it
warns once and drops that telemetry. Then make an AI call and look for the trace in
your Future AGI project.

## Troubleshooting

- **No traces from a deployed app.** Copy the whole published hook directory, not
  only the DLL. If runtime files are missing, the agent prints one line and the
  application runs without tracing.
- **The app does not start.** A `DOTNET_STARTUP_HOOKS` path that does not exist fails
  in the .NET runtime before the agent runs. Check that the path is absolute and
  correct.
- **Builds or tests pick up the hook.** `DOTNET_STARTUP_HOOKS` was exported in the
  shell. Set it on the application's command line instead.
- **Hosting-startup load error in a child process.** Child processes inherit
  `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES`. Start ASP.NET Core children that run without
  the hook with that variable removed.
- **Missing-key warning.** Set both `FI_API_KEY` and `FI_SECRET_KEY` in the same
  place. Halves from different sources are not combined.
- **A chat client shows `manual`.** Clients built directly
  (`new ChatClientBuilder(...).Build()`), keyed registrations and non-ASP.NET Core
  apps are not wrapped. Call `UseOpenTelemetry()` on them.

## Limits

- No CLR profiler, native SDK interposition, custom `ActivitySource` discovery,
  WebSocket or gRPC AI transport, or metrics capture.
- `HttpClient` subclasses or handlers that do not emit normal diagnostics are not
  covered.
- Native SDK telemetry is experimental, and operation coverage varies by library
  version.
- A web request span is sent only when an AI span of its trace ended before the
  request did.
- Continuous integration runs on macOS and Linux. Windows and other architectures are
  not exercised.
- Initialization happens once per process.

## More detail

- [TraceAI.AutoInstrumentation README](../../csharp/src/TraceAI.AutoInstrumentation/README.md):
  the in-process `TraceAIAuto.Init()` entry point, sampling, diagnostics APIs, wire
  behaviour and the full list of limits.
- [Shared auto-instrumentation spec](../../spec/auto-instrumentation/README.md):
  behaviour shared by all traceAI SDKs.
