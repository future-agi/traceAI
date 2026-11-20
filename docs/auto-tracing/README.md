# Zero-code tracing

Trace the AI calls in an application without adding tracing code. You install one package, start the app with one flag or variable, and supported model calls become OpenTelemetry spans in Future AGI: model, input and output, token usage, timing and errors.

Pick your language:

| Language | Package | Turn it on | Guide |
| --- | --- | --- | --- |
| Python 3.10+ | `fi-instrumentation-otel` (PyPI) | `fi-instrument python app.py` | [Python](python.md) |
| Node.js 20.6+ | `@traceai/auto` (npm) | `node --import @traceai/auto/register app.mjs` | [Node.js and TypeScript](node.md) |
| Java 17+ | `com.futureagi:traceai-javaagent` (Maven Central) | `java -javaagent:traceai-javaagent-<version>.jar -jar app.jar` | [Java](java.md) |
| Go | `github.com/future-agi/traceAI/go/auto` | `import _ "github.com/future-agi/traceAI/go/auto"` | [Go](go.md) |
| .NET 8 / .NET 10 | `TraceAI.AutoInstrumentation` (build from source) | `DOTNET_STARTUP_HOOKS=<path>/TraceAI.AutoInstrumentation.dll` | [.NET](dotnet.md) |
| Ruby 3.2+ | `traceai-auto` (install from this repository) | `RUBYOPT="-rtraceai/auto"` | [Ruby](ruby.md) |

The .NET and Ruby agents are not on NuGet or RubyGems yet; their guides show how to use them from this repository.

## The same three steps in every language

1. **Install** the package for your language.
2. **Set your Future AGI keys** in the environment of the process you trace:

   ```sh
   export FI_API_KEY="your-future-agi-api-key"
   export FI_SECRET_KEY="your-future-agi-secret-key"
   export FI_PROJECT_NAME="my-ai-app"   # optional; names the project in Future AGI
   ```

   Keep your model provider's own keys (for example `OPENAI_API_KEY`) as they are.
3. **Start the app** with the flag or variable from the table above, make an AI request, and open **Observe → your project** in Future AGI.

## Prompt and response content

Each language guide lists the exact settings. The common ones are environment variables read at startup:

| Setting | Effect |
| --- | --- |
| `FI_HIDE_INPUTS=true` | Do not record request content (prompts, input messages). |
| `FI_HIDE_OUTPUTS=true` | Do not record response content (completions, output messages). |
| `FI_HIDE_ERROR_DETAILS=true` | Do not record error messages and stack traces. |

Content is recorded by default in every language. In .NET, the clients and agents the ASP.NET Core hosting startup wraps keep prompt and response content off unless `FI_AUTO_CAPTURE_CONTENT=true` is set, as its guide explains. Check the guide for your language for the full list of hide settings and their exact names.

## Your existing OpenTelemetry setup

The agents send AI spans to Future AGI and leave your application's own OpenTelemetry provider and its other spans alone. Each guide describes how the agent works next to an existing setup.

## More

- [Environment variables](../configuration/environment-variables.md)
- [TraceConfig and data redaction](../configuration/trace-config.md)
- [Cross-language behaviour spec](../../spec/auto-instrumentation/README.md)
