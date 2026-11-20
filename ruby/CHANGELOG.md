# Changelog

All notable changes to `traceai-auto` are documented here. Release notes are added
by the release process.

## Unreleased

### Added
- RubyLLM 2.x (`>= 2.0, < 3`) is instrumented with no application code: each model request has a `chat <model>` span (streamed or not) and each tool call an `execute_tool <name>` span, with GenAI model, usage, finish-reason and message attributes, tool arguments and results, and errors recorded and re-raised. The HTTP request inside a chat span is not reported a second time. `FI_DISABLED_INSTRUMENTATIONS=ruby_llm` turns it off, and the doctor reports `ruby_llm` as `instrumented` when a supported RubyLLM is installed.
- Raw input and raw output (`raw.input`, `raw.output` and their children) are not recorded while any content hide setting is on, and a tool call's name and id stay visible while its arguments and result follow the input and output hide settings.
