# Changelog

This package is versioned together with FIInstrumentation (`fi-instrumentation-otel`): the
startup hook embeds the SDK sources, so a change to either one releases both at one version.
Release notes are added here by the release process.

## Unreleased

### Added
- ASP.NET Core hosts are traced with no application code: the startup hook activates the companion assembly `TraceAI.AutoInstrumentation.HostingStartup`, which wraps registered `IChatClient`, `IEmbeddingGenerator<,>` and `AIAgent` services in the libraries' own OpenTelemetry wrappers when `extensions_ai` or `agents` is enabled. An application that already calls `UseOpenTelemetry()` is not wrapped twice, no hide setting is overridden, and an existing `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` value is kept.
- The agent starts next to `System.Diagnostics.DiagnosticSource` 11, which `Microsoft.Extensions.AI` 11 ships.

### Changed
- Prompt and response content is now recorded by default in ASP.NET Core hosts, like in every other language. Set `FI_AUTO_CAPTURE_CONTENT=false`, or any of `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` or `FI_HIDE_INPUT_IMAGES` set to `true`, to keep it off. `FI_AUTO_CAPTURE_CONTENT` also accepts `'false'` as a TOML literal string in the config file, and an unrecognised value keeps content off with one warning that names only the setting. The libraries' switch puts content on their spans for every listener in the application, not only this agent, and the agent prints one line at startup when it turns the switch on and keeps the registration.
- A blank (empty or whitespace) `FI_*` environment value now counts as not set, so the config file's value or the default applies. Before, `FI_BASE_URL=`, `FI_PROJECT_TYPE=` (both rejected at startup), `FI_WIRE_CAPTURE=` (wire capture off), `FI_PROJECT_NAME=`, `FI_EVAL_TAGS=`, `FI_METADATA=`, `FI_SAMPLE_RATE=`, `FI_INSTRUMENTATIONS=`, `FI_DISABLED_INSTRUMENTATIONS=` and `FI_CONFIG_FILE=` overrode the file with an empty value.

