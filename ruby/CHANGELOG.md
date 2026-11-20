# Changelog

All notable changes to `traceai-auto` are documented here. Release notes are added
by the release process.

## Unreleased

### Added
- RubyLLM 2.x (`>= 2.0, < 3`) is instrumented with no application code: each model request has a `chat <model>` span (streamed or not) and each tool call an `execute_tool <name>` span, with GenAI model, usage, finish-reason and message attributes, tool arguments and results, and errors recorded and re-raised. The HTTP request inside a chat span is not reported a second time. `FI_DISABLED_INSTRUMENTATIONS=ruby_llm` turns it off, and the doctor reports `ruby_llm` as `instrumented` when a supported RubyLLM is installed.
- Raw input and raw output (`raw.input`, `raw.output` and their children) are not recorded while any content hide setting is on, and a tool call's name and id stay visible while its arguments and result follow the input and output hide settings.

### Changed
- Known secrets that overlap one another, in any number and at any overlap (a shared edge of any length, a secret inside another, chains of three or more), are replaced as a whole in the copy sent to Future AGI at export and in a span captured after they were known; a value that would need more matching work than the bounded limit is replaced entirely. Values registered as secret after a span has ended are replaced in the copy sent to Future AGI; the application's own exporters receive spans as they were when they ended. A RubyLLM span that is still open when a secret is learned has its values replaced again when it ends, so both copies are clean.
- Future AGI keys are sent only over a checked transport. Literal loopback (`localhost`, `127.0.0.1`, `::1`) may use plaintext; Future AGI hosts (`futureagi.com` and subdomains, case and one trailing dot ignored) require `https://` and no setting relaxes that. Any other `http://` `FI_BASE_URL` is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1`, which logs one warning per process. Other spellings of loopback (`127.1`, `2130706433`, `127.0.0.1.`) are not literal loopback. A URL with userinfo, a backslash, `%` or `@` anywhere is refused, as is a loopback name that connects to a non-loopback peer. No proxy from the environment is used, and a 3xx answer to an export is a refusal. Refusals never print the URL or the keys.
