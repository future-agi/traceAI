# Future AGI plugin for Hermes

This directory is a **Hermes directory-plugin wrapper**, not a separately
published Python distribution. Its `pyproject.toml` tells the Hermes plugin
manager to install `traceAI-hermes-agent`, which implements tracing for turns,
model calls, and tool calls.

## Install and enable

For a managed Hermes installation:

```sh
hermes plugins install future-agi/traceAI/python/frameworks/hermes-agent/hermes-plugin/futureagi --enable
```

For validated saved configuration, use `npx @traceai/agents setup hermes --project coding-agents` instead; it invokes the native installer for you. Shared setup enables content capture by default; add `--no-capture-content` for metadata only. Node is not needed at runtime.

For a Python-only installation, set `FI_API_KEY`, `FI_SECRET_KEY`, and `FI_PROJECT_NAME` through your secret manager, then restart Hermes. Env-only installs retain the earlier metadata-only default; opt in with `FI_AGENT_CAPTURE_CONTENT=true`. No keys belong in this wrapper or a committed configuration file. Review Hermes' dependency consent prompt.

See the [integration README](https://github.com/future-agi/traceAI/tree/dev/python/frameworks/hermes-agent)
for supported Hermes versions, installation alternatives, privacy controls, and
troubleshooting, or the [tracing documentation](https://docs.futureagi.com/docs/tracing)
for Future AGI setup.

## License

Apache-2.0.
