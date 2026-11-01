# Future AGI plugin for Hermes

This directory is a **Hermes directory-plugin wrapper**, not a separately
published Python distribution. Its `pyproject.toml` tells the Hermes plugin
manager to install `traceAI-hermes-agent`, which implements tracing for turns,
model calls, and tool calls.

## Install and enable

For a managed Hermes installation:

```sh
hermes plugins install future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi
hermes plugins enable futureagi
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `FI_PROJECT_NAME` in your Hermes environment,
then restart Hermes. Content capture is off by default. No keys belong in this
wrapper or in a committed configuration file.

See the [integration README](https://github.com/future-agi/traceAI/tree/dev/python/frameworks/hermes-agent)
for supported Hermes versions, installation alternatives, privacy controls, and
troubleshooting, or the [tracing documentation](https://docs.futureagi.com/docs/tracing)
for Future AGI setup.

## License

Apache-2.0.
