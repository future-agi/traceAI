# Changelog

## [0.1.0] - pending release

- Instrument Microsoft Prompt flow (`promptflow-tracing`, `promptflow-core`): flex and DAG flow runs as `CHAIN` spans, nodes and `@trace` tools as child spans, LLM calls made through Prompt flow's OpenAI integration as `LLM` spans with model and token usage; sync, async and streaming.
