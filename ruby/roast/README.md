# Roast workflow instrumentation

`traceai-roast` adds OpenTelemetry workflow and cog spans to Shopify Roast 1.1–1.x. It runs in the execution fiber, not Roast's asynchronous log/event consumer. Async cogs are children of their workflow; model spans emitted inside a cog can become its children. Prompts, responses, exception messages and workflow file paths are not captured. Use non-sensitive cog names because names are exported.

## Install

Add `traceai-roast`, `opentelemetry-sdk`, and `opentelemetry-exporter-otlp` to your Gemfile, then run `bundle install`. Ruby 3.3 or newer is required.

## Configure

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `FI_PROJECT_NAME` in your server environment. Configure the OpenTelemetry SDK before starting Roast:

```ruby
require "opentelemetry/sdk"
require "opentelemetry/exporter/otlp"
require "traceai/roast"

exporter = OpenTelemetry::Exporter::OTLP::Exporter.new(
  endpoint: "https://api.futureagi.com/tracer/v1/traces",
  headers: {
    "X-Api-Key" => ENV.fetch("FI_API_KEY"),
    "X-Secret-Key" => ENV.fetch("FI_SECRET_KEY")
  },
  compression: "none"
)
OpenTelemetry::SDK.configure do |config|
  config.service_name = "roast-app"
  config.resource = OpenTelemetry::SDK::Resources::Resource.create(
    "project_name" => ENV.fetch("FI_PROJECT_NAME"),
    "project_type" => "observe"
  )
  config.add_span_processor(OpenTelemetry::SDK::Trace::Export::BatchSpanProcessor.new(exporter))
end
TraceAI::Roast.install!
at_exit { OpenTelemetry.tracer_provider.shutdown }
```

Load this configuration once before running a Roast workflow. If your app already configures OpenTelemetry, reuse that provider instead of creating a second one. For model spans, enable native RubyLLM tracing on a compatible RubyLLM release using the same provider; this gem does not instrument RubyLLM or Roast's external agent subprocesses. Do not enable an additional RubyLLM instrumentation simultaneously.
