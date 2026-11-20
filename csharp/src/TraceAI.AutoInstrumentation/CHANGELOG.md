# Changelog

This package is versioned together with FIInstrumentation (`fi-instrumentation-otel`): the
startup hook embeds the SDK sources, so a change to either one releases both at one version.
Release notes are added here by the release process.

## Unreleased

### Added
- ASP.NET Core hosts are traced with no application code: the startup hook activates the companion assembly `TraceAI.AutoInstrumentation.HostingStartup`, which wraps registered `IChatClient`, `IEmbeddingGenerator<,>` and `AIAgent` services in the libraries' own OpenTelemetry wrappers when `extensions_ai` or `agents` is enabled. An application that already calls `UseOpenTelemetry()` is not wrapped twice, no hide setting is overridden, and an existing `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` value is kept.
- The agent starts next to `System.Diagnostics.DiagnosticSource` 11, which `Microsoft.Extensions.AI` 11 ships.

### Changed
- Prompt and response content is now recorded by default in ASP.NET Core hosts, like in every other language. Set `FI_AUTO_CAPTURE_CONTENT=false` or any `FI_HIDE_*` setting to keep it off. The libraries' switch puts content on their spans for every listener in the application, not only this agent, and the agent prints one line at startup when it turns the switch on.

