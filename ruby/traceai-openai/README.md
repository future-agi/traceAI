# traceai-openai (Ruby)

Opt-in OpenTelemetry instrumentation for the official [`openai` Ruby gem](https://github.com/openai/openai-ruby). Covers `client.chat.completions.create`, `stream_raw`, `client.responses.create`, and `stream_raw`; emits model, operation, response ID, token counts and errors. It never records prompts, completions, tools, request headers, or API keys. SDK streaming must be consumed or closed to finish its span. Other OpenAI operations pass through untouched.

This gem is not yet published. The Ruby release pipeline is configured, but RubyGems credentials and a verified registry release are still required. Until then, use this package from source for development only; do not put `gem 'traceai-openai'` in customer setup guides.

```ruby
require 'openai'
require 'opentelemetry/sdk'
require 'traceai/openai'

OpenTelemetry::SDK.configure
TraceAI::OpenAI.install! # install once, before creating requests
client = OpenAI::Client.new(api_key: ENV.fetch('OPENAI_API_KEY'))
client.responses.create(model: 'gpt-4o-mini', input: 'Hello')
```

Configure the OpenTelemetry OTLP HTTP exporter separately with Future AGI's traces endpoint and credential headers. Do not install `opentelemetry-instrumentation-openai` alongside this adapter. Installing it first prevents `TraceAI::OpenAI.install!`; if auto-instrumentation installs it afterward, it owns Chat spans and this adapter continues to cover Responses without duplicate Chat spans. The standard adapter may use different metadata and content-capture settings. The instrumentation gem intentionally does not own exporter configuration or read credentials. See `test/instrumentation_test.rb` for synthetic, network-free contract tests.
