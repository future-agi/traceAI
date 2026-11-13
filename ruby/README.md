# traceai-auto

Zero-code tracing for AI calls made from Ruby. Start your application with one
`RUBYOPT` flag and every recognized AI HTTP call (OpenAI, Anthropic, Gemini,
Bedrock, Cohere, Ollama, speech and transcription APIs, OpenAI-compatible
gateways and more) becomes an OpenTelemetry CLIENT span with model, messages,
usage, timing and errors, exported to Future AGI or to your own OpenTelemetry
destination. Your application code does not change.

- Gem name `traceai-auto`, require path `traceai/auto`, Ruby 3.1 or newer.
- No runtime gem dependencies. The agent never activates a gem version on your
  application's behalf, so it is safe to inject before Bundler.
- Status: version 0.1.0, not yet published to RubyGems. Install it from this
  repository as shown below.

## Quick start

From a checkout of this repository, point Ruby at the gem's `lib` folder and
require the agent:

```sh
export FI_API_KEY=your-api-key FI_SECRET_KEY=your-secret-key
export RUBYLIB="/path/to/traceAI/ruby/lib${RUBYLIB:+:$RUBYLIB}"
export RUBYOPT="${RUBYOPT:+$RUBYOPT }-rtraceai/auto"
bundle exec ruby app.rb   # or: ruby app.rb
```

Keep any existing `RUBYOPT` value; append to it rather than replacing it.

With Bundler you can also add the gem to the application's `Gemfile` and keep the
same `RUBYOPT`:

```ruby
gem 'traceai-auto', path: '/path/to/traceAI/ruby'
```

What happens at startup:

- In a Bundler project the agent waits for your application's own
  `require 'bundler/setup'` (or `Bundler.setup`) before it activates anything.
- In a standalone script it installs dependency-free hooks and initializes on the
  first client request.
- If the application has already loaded `opentelemetry-sdk`, spans go through
  the SDK. Otherwise a small built-in exporter sends OTLP/HTTP protobuf to
  `${FI_BASE_URL}/tracer/v1/traces` (default base `https://api.futureagi.com`).
- On Ruby older than 3.1 the agent prints one warning and the application runs
  untraced. Any initialization error also leaves the application running.

## Send spans to your own OpenTelemetry destination

FI keys are optional when the application already has an OpenTelemetry SDK
destination. An existing global SDK tracer provider is used automatically. You
can also pass one explicitly:

```ruby
require 'opentelemetry/sdk'
require 'traceai/auto/agent'
TraceAI::Auto.init(provider: my_tracer_provider)
# or exporter: my_exporter, or processor: my_span_processor
```

Without FI keys no Future AGI exporter is created. To export to Future AGI as
well as to explicit destinations, pass `fi_export: true` and a complete FI key
pair. `OTEL_SDK_DISABLED=true` turns the agent off.

## In code

```ruby
require 'traceai/auto/agent'
TraceAI::Auto.init(project_name: 'checkout', sample_rate: 0.5)

TraceAI::Auto.using_user('user-7') { client.chat(...) }
TraceAI::Auto.using_metadata({'tier' => 'gold'}) { ... }
TraceAI::Auto.with_attributes({'tag.tenant' => 'acme'}) { ... }

TraceAI::Auto.register_secret(token_from_somewhere_else) # never exported
TraceAI::Auto.flush(timeout: 5)
TraceAI::Auto.shutdown(timeout: 5)
```

`init` configures once per process and returns the tracer provider; later calls
return it unchanged. `TraceAI::Auto.auto_instrument(instrumentations: ['wire', 'rack'])`
adds integrations later. Normal exit and SIGTERM flush pending spans within five
seconds; SIGKILL and `exit!` cannot.

## Configuration

Precedence: explicit `init` arguments, then environment variables, then the
config file (`FI_CONFIG_FILE`, default `./futureagi.toml`), then defaults.

| Variable | Meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Future AGI credentials. Used only as a complete pair from one source. |
| `FI_BASE_URL` | Collector base URL. |
| `FI_PROJECT_NAME`, `FI_PROJECT_TYPE`, `FI_PROJECT_VERSION_NAME` | Project routing. The default name is `OTEL_SERVICE_NAME`, then the working directory name. |
| `FI_INSTRUMENTATIONS`, `FI_DISABLED_INSTRUMENTATIONS` | Allow or deny integrations (`wire`, `net_http`, `httpx`, `rack`). |
| `FI_WIRE_CAPTURE` | `false` turns off wire capture. |
| `FI_WIRE_MAX_BODY_BYTES` | Body capture limit, default 4 MiB, at most 64 MiB. |
| `FI_SAMPLE_RATE` | Ratio sampler for an agent-created provider. |
| `FI_AUTO_DEBUG` | Print a startup report. |

```toml
# futureagi.toml
project_name = "checkout"
instrumentations = ["wire"]
wire_max_body_bytes = 4194304
sample_rate = 1.0
```

## What is captured

The agent recognizes the 25 wire formats of the shared catalog in
`spec/auto-instrumentation`: chat, Responses, completions, embeddings, rerank,
moderation, image generation, speech and transcription, across the provider
hosts and paths listed there. Unknown hosts are captured when the path and body
match a known format.

| Transport | Coverage |
| --- | --- |
| `Net::HTTP` | Buffered and streamed requests and responses, multipart uploads, gzip and deflate. Covers Faraday's default adapter, `ruby-openai`, the official `openai` and `anthropic` gems and RubyLLM. |
| `HTTPX` | Buffered and `:stream` responses, retries and `:follow_redirects`. |

Spans carry `gen_ai.*` request, response and usage attributes, input and output
messages, `gen_ai.response.time_to_first_chunk` and
`gen_ai.response.time_to_first_text` for streams, and `traceai.wire.*` markers
when a body was truncated, incomplete or not decodable. If a library already
records the call as an LLM, embedding, reranker or guardrail span, the wire span
is suppressed so the call is not counted twice. The agent never adds retries,
reads or network calls to your requests.

When a span has more attributes than the SDK allows (128 by default), the agent
keeps identity, outcome, model, usage, timing and provenance attributes first,
then output, then the newest input messages, and records the number of omitted
content attributes in `traceai.capture.dropped_content_count`.

## Privacy

Content capture is on by default. Before export, every string is checked
against the shared privacy catalog (`lib/traceai/auto/privacy.json`, a byte copy
of `spec/auto-instrumentation/privacy.json`, version 3):

- Credential-named keys, headers and URL query parameters are replaced with
  `<redacted>`. Token counts and timing names such as `max_tokens` or
  `time_to_first_token` stay visible as metrics.
- Credential-shaped values are redacted wherever they appear: long provider
  keys, AWS access keys, JWTs, `Bearer` and `Basic` values and private keys.
- Credentials the process has seen (FI keys, credential-like environment values,
  credential headers and query values on instrumented requests, and values passed
  to `register_secret`) are removed from every exported string.
- Media is omitted: data URLs, base64 images and audio, and payloads next to a
  media MIME type.

Logprob tokens in ordinary payloads are treated like any other token-named field.
Code that holds a parsed OpenAI chat response can call
`TraceAI::Auto::Privacy.scrub_provider_response(response, secrets)` instead: it
keeps ordinary logprob tokens, still checks them against known secrets and
credential shapes, and masks a choice's whole logprobs block when its message
text had to be masked.

Hide controls replace content with `__REDACTED__`: `FI_HIDE_INPUTS`,
`FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`,
`FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`,
`FI_HIDE_EMBEDDING_VECTORS`, `FI_HIDE_LLM_INVOCATION_PARAMETERS` and
`FI_HIDE_ERROR_DETAILS`. Other exporters in your application keep their original
data; only the copy exported by the agent is changed.

## Doctor and bootstrap

With `RUBYLIB` set as in the quick start:

```sh
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor              # report plus one test export
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor --no-export  # report only
ruby /path/to/traceAI/ruby/bin/traceai-auto bootstrap           # integration inventory
```

The report never contains credentials. `bootstrap` only gives advice; it does not
install anything.

## Limitations

- Not covered: non-default Faraday adapters (Typhoeus, Excon, HTTP.rb), direct
  sockets, WebSockets, gRPC and custom HTTPX transport plugins.
- There are no library-level instrumentors in this gem; calls are traced at the
  HTTP layer. Rack server spans need `opentelemetry-instrumentation-rack` in the
  application's bundle. The `traceai-roast` gem is not enabled automatically.
- The built-in exporter needs the application to have loaded OpenSSL before it
  can send to an HTTPS collector (any HTTPS provider call does this), and it does
  not use HTTP proxies.
- Brotli and zstd bodies are not decoded. AWS Bedrock binary event streams keep
  request and status attributes only.
- Privacy recognition is structural, not general PII detection. Use the hide
  controls when content must never leave the process.
- Session-named fields, including the `session.id` set by
  `TraceAI::Auto.using_session`, are exported as `<redacted>` because the
  catalog treats names containing `session` as credentials.
- The context helpers apply to the current Fiber. Other threads and Fibers must
  propagate their own OpenTelemetry context.
- Written for CRuby 3.1 and newer. JRuby, TruffleRuby and Windows have not been
  tested.

## Development

```sh
cd ruby
bundle config set --local path .bundle/vendor
bundle install
gem install json -v 2.6.3 --no-document --install-dir "$(bundle exec ruby -e 'print Gem.dir')"
bundle exec rake test
```

Several tests start child Ruby processes with an older pinned `json` gem, which is
why `json` 2.6.3 is installed next to the bundle. The privacy and wire tests read
the shared specification in `spec/auto-instrumentation`; after the specification
changes, copy `privacy.json`, `privacy-fixtures.json` and `wire-protocols.json`
into `lib/traceai/auto` unchanged.
