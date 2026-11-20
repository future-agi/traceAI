# Changelog

This package is versioned together with FIInstrumentation (`fi-instrumentation-otel`): the
startup hook embeds the SDK sources, so a change to either one releases both at one version.
Release notes are added here by the release process.

## Unreleased

### Added
- ASP.NET Core hosts are traced with no application code: the startup hook activates the companion assembly `TraceAI.AutoInstrumentation.HostingStartup`, which wraps registered `IChatClient`, `IEmbeddingGenerator<,>` and `AIAgent` services in the libraries' own OpenTelemetry wrappers when `extensions_ai` or `agents` is enabled. An application that already calls `UseOpenTelemetry()` is not wrapped twice, no hide setting is overridden, and an existing `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` value is kept.
- The agent starts next to `System.Diagnostics.DiagnosticSource` 11, which `Microsoft.Extensions.AI` 11 ships.

### Changed
- Prompt and response content is recorded by default in ASP.NET Core hosts, like in every other language. Set `FI_AUTO_CAPTURE_CONTENT=false`, or any of `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` or `FI_HIDE_INPUT_IMAGES` set to `true`, to keep it off; a `TraceConfig` supplied to the agent with any of those hides keeps it off too, and is used instead of the variables. `FI_AUTO_CAPTURE_CONTENT` also accepts `'false'` as a TOML literal string in the config file, and an unrecognised value keeps content off with one warning that names only the setting. The libraries' switch puts content on their spans for every listener in the application, not only this agent; the agent prints one line when it first turns the switch on for a service it keeps (factory and type registrations print it on first resolve).
- A blank (empty or whitespace) `FI_*` environment value counts as not set, so the config file's value or the default applies, except where that would be the less cautious reading: a blank `FI_BASE_URL` stops the agent (one line names `FI_BASE_URL` and says traces are off; the application keeps running untraced), a blank `FI_CONFIG_FILE` reads no config file, and a blank `FI_WIRE_CAPTURE` is off. A blank `FI_SAMPLE_RATE` leaves `OTEL_TRACES_SAMPLER` in charge and a blank `FI_AUTO_DEBUG` lets the file decide. The README lists every key.

