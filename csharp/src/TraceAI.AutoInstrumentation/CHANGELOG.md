# Changelog

This package is versioned together with FIInstrumentation (`fi-instrumentation-otel`): the
startup hook embeds the SDK sources, so a change to either one releases both at one version.
Release notes are added here by the release process.

## Unreleased

### Added
- ASP.NET Core hosts are traced with no application code: the startup hook activates the companion assembly `TraceAI.AutoInstrumentation.HostingStartup`, which wraps registered `IChatClient`, `IEmbeddingGenerator<,>` and `AIAgent` services in the libraries' own OpenTelemetry wrappers when `extensions_ai` or `agents` is enabled. An application that already calls `UseOpenTelemetry()` is not wrapped twice, content capture follows the traceAI hide settings, and an existing `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` value is kept.
- The agent starts next to `System.Diagnostics.DiagnosticSource` 11, which `Microsoft.Extensions.AI` 11 ships.

