# traceai-auto

Zero-code tracing for AI calls made from Ruby. Start your application with one
`RUBYOPT` flag and every recognized AI HTTP call (OpenAI, Anthropic, Gemini,
Bedrock, Cohere, Ollama, speech and transcription APIs, OpenAI-compatible
gateways and more) becomes an OpenTelemetry CLIENT span with model, messages,
usage, timing and errors, exported to Future AGI or to your own OpenTelemetry
destination. Your application code does not change.

Tracing starts only when you turn it on (the `RUBYOPT` flag or an explicit
`require`). Once on, every recognized AI call is traced with its content
captured by default (the `FI_HIDE_*` settings hide it). Future AGI receives only
AI spans, masked by the shared privacy catalog; your application's own
OpenTelemetry provider and its other spans are left as they are.

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
bundle exec ruby app.rb
```

Keep any existing `RUBYOPT` value; append to it rather than replacing it. Set
`RUBYOPT` only for the processes you want traced: exported in a shell profile or
an image, it turns the agent on in every Ruby process started there.

In a directory with a `Gemfile` (or with `BUNDLE_GEMFILE` set), `ruby app.rb`
without `bundle exec` is traced only if the script itself runs
`require 'bundler/setup'`; otherwise the agent prints one warning at exit and
traces nothing. In a directory without a `Gemfile`, `ruby app.rb` works as is.

With Bundler you can also add the gem to the application's `Gemfile` and keep the
same `RUBYOPT`. Use `require: false`, so that `RUBYOPT` stays the switch:

```ruby
gem 'traceai-auto', path: '/path/to/traceAI/ruby', require: false
```

Without `require: false`, `Bundler.require` (which Rails runs at boot) loads the
agent in every process that loads the bundle, in every environment: servers,
consoles, rake tasks and test suites.

What happens at startup:

- In a Bundler project the agent waits for your application's own
  `require 'bundler/setup'` (or `Bundler.setup`) before it activates anything.
- Only the first AI request or an explicit `TraceAI::Auto.init` starts the
  agent. An AI request is one the wire catalog recognizes. Any other outgoing
  request (a secrets fetch, a metadata service call, a webhook) starts nothing,
  and neither do the context helpers: what a helper sets is remembered and used
  by the first AI request made inside its block.
- If your application sets its own OpenTelemetry SDK tracer provider (for
  example with `OpenTelemetry::SDK.configure` in a Rails initializer), the agent
  attaches to that provider at once, so wire spans appear under your own spans
  and also reach your own exporters, and your own AI spans reach Future AGI. It
  never makes a provider of its own global. Attaching does not use up `init`:
  until the first AI request, an explicit `TraceAI::Auto.init` still applies its
  destinations and settings.
- If an AI request comes before your OpenTelemetry configuration, those first
  wire spans use a private provider, and the agent moves to your provider as
  soon as you set one.
- Without `opentelemetry-sdk` in the bundle, or in a standalone script, the agent
  uses a dependency-free pipeline that starts on the first AI request.
- Export to Future AGI always uses the agent's built-in exporter, which sends
  OTLP/HTTP protobuf to `${FI_BASE_URL}/tracer/v1/traces` (default base
  `https://api.futureagi.com`). It loads OpenSSL itself when your application has
  not, and it connects directly: `HTTPS_PROXY` and other proxy settings are not
  used.
- On Ruby older than 3.1 the agent prints one warning and the application runs
  untraced. Any initialization error also leaves the application running.
- Files and environment values are read as UTF-8 whatever the process locale,
  so containers started with `LANG=C` work.

## Send spans to your own OpenTelemetry destination

FI keys are optional when the application already has an OpenTelemetry SDK
destination. The global SDK tracer provider your application sets, before or
after the agent loads, is used automatically. You can also pass one explicitly,
also after a `RUBYOPT` preload:

```ruby
require 'opentelemetry/sdk'
require 'traceai/auto/agent'
TraceAI::Auto.init(provider: my_tracer_provider)
# or exporter: my_exporter, or processor: my_span_processor
```

Call `init` before the first AI request. Until then it applies its destinations
and settings, also when the agent has already attached to your provider or
answered a `doctor` report. AI spans started before an explicit `init` are not
sent to Future AGI: the agent drops the ones it had queued rather than send them
with the settings you replaced. After the
first AI request, or after an earlier `init`, further `init` calls return the
provider in use, change nothing and print one warning.

Without FI keys no Future AGI exporter is created. To export to Future AGI as
well as to explicit destinations, pass `fi_export: true` and a complete FI key
pair. `OTEL_SDK_DISABLED=true` turns the agent off.

## In code

```ruby
require 'traceai/auto/agent'
TraceAI::Auto.init(project_name: 'checkout', sample_rate: 0.5)

TraceAI::Auto.using_session('session-42') { client.chat(...) }
TraceAI::Auto.using_user('user-7') { client.chat(...) }
TraceAI::Auto.using_metadata({'tier' => 'gold'}) { ... }
TraceAI::Auto.with_attributes({'tag.tenant' => 'acme'}) { ... }

TraceAI::Auto.register_secret(token_from_somewhere_else) # never exported
TraceAI::Auto.flush(timeout: 5)
TraceAI::Auto.shutdown(timeout: 5)
```

`init` configures once per process and returns the tracer provider; later calls
return it unchanged. `TraceAI::Auto.auto_instrument(instrumentations: ['wire', 'rack'])`
adds integrations later. Pending spans are flushed within five seconds at exit,
including an exit caused by SIGTERM or by your own SIGTERM handler calling
`exit`; the agent installs no signal handlers. SIGKILL and `exit!` cannot flush.

## Configuration

Precedence: explicit `init` arguments, then environment variables, then the
config file (`FI_CONFIG_FILE`, default `./futureagi.toml`), then defaults.
A config file that is over 64 KiB or cannot be read is ignored as a whole, and
a line in it that is not valid UTF-8 is skipped (a hide line included); each of
these prints one warning that names the file and what failed, never its
content, and a file that does not exist prints nothing.

| Variable | Meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Future AGI credentials. Used only as a complete pair from one source. |
| `FI_BASE_URL` | Collector base URL. A `base_url` in the config file is used only when the key pair comes from the same file. |
| `FI_PROJECT_NAME`, `FI_PROJECT_TYPE`, `FI_PROJECT_VERSION_NAME` | Project routing. The default name is `OTEL_SERVICE_NAME`, then the working directory name. |
| `FI_INSTRUMENTATIONS`, `FI_DISABLED_INSTRUMENTATIONS` | Allow or deny integrations. `wire` covers both transports; `net_http` or `httpx` alone enables the wire layer for that transport only; `rack` controls server spans. An allowlist must name every integration you want, for example `wire,rack`. |
| `FI_WIRE_CAPTURE` | `false` turns off wire capture. |
| `FI_WIRE_MAX_BODY_BYTES` | Body capture limit, default 4 MiB, at most 64 MiB. |
| `FI_SAMPLE_RATE` | Ratio sampler for an agent-created provider. |
| `FI_AUTO_DEBUG` | Print a startup report. |

```toml
# futureagi.toml
project_name = "checkout"
instrumentations = ["wire", "rack"]
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

Future AGI receives only AI spans: the agent's own wire spans, spans that carry
`gen_ai.span.kind` or `traceai.capture.layer`, and a server span whose trace
contained one of those. Other spans on your provider (HTTP, database, queue and
custom spans, and vendor spans that set only standard GenAI attributes) stay with
your own exporters and are never sent to Future AGI.

Content capture is on by default. Before export, every string is checked
against the shared privacy catalog (`lib/traceai/auto/privacy.json`, a byte copy
of `spec/auto-instrumentation/privacy.json`, version 3):

- Credential-named keys, headers and URL query parameters are replaced with
  `<redacted>`. Token counts and timing names such as `max_tokens` or
  `time_to_first_token` stay visible as metrics.
- Credential-shaped values are redacted wherever they appear: long provider
  keys, AWS access keys, JWTs, `Bearer` and `Basic` values and private keys.
- Credentials the process has seen (FI keys, credential-like environment values,
  credential headers and query values on outgoing `Net::HTTP` and `HTTPX`
  requests, including requests made before the first AI request, and values
  passed to `register_secret`) are removed from every exported string.
- Media is omitted: data URLs, base64 images and audio, and payloads next to a
  media MIME type.
- The `session.id` you set with `TraceAI::Auto.using_session` (or
  `with_attributes`) is exported as set, so Future AGI can group a session's
  traces, also when the same request sends that id to the provider in its body.
  A registered secret or credential-shaped value inside it is still redacted,
  and every other session-named field (`session_id`, session tokens, cookies) is
  replaced with `<redacted>`.

Logprob tokens in ordinary payloads are treated like any other token-named field.
Code that holds a parsed OpenAI chat response can call
`TraceAI::Auto::Privacy.scrub_provider_response(response, secrets)` instead: it
keeps ordinary logprob tokens, still checks them against known secrets and
credential shapes, and masks a choice's whole logprobs block when its message
text had to be masked.

Hide controls replace content with `__REDACTED__`. Other exporters in your
application keep their original data; only the copy exported by the agent is
changed. Each control can be set as an environment variable, as an `init` option
or as a `futureagi.toml` key. The option and the file key are the variable's
name in lower case without `FI_`:

| Variable | `init` option and `futureagi.toml` key |
| --- | --- |
| `FI_HIDE_INPUTS` | `hide_inputs` |
| `FI_HIDE_OUTPUTS` | `hide_outputs` |
| `FI_HIDE_INPUT_MESSAGES` | `hide_input_messages` |
| `FI_HIDE_OUTPUT_MESSAGES` | `hide_output_messages` |
| `FI_HIDE_INPUT_TEXT` | `hide_input_text` |
| `FI_HIDE_OUTPUT_TEXT` | `hide_output_text` |
| `FI_HIDE_INPUT_IMAGES` | `hide_input_images` |
| `FI_HIDE_EMBEDDING_VECTORS` | `hide_embedding_vectors` |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `hide_llm_invocation_parameters` |
| `FI_HIDE_ERROR_DETAILS` | `hide_error_details` |

```toml
# futureagi.toml
hide_inputs = true
hide_output_text = "on"
```

Write the file key exactly as in the table. A file line that uses the variable
name instead (`FI_HIDE_INPUTS = true`) is not a hide control and is ignored.

A hide control reads `true`, `1`, `yes` or `on` as hide and `false`, `0`, `no`
or `off` as show, ignoring case and surrounding spaces; a blank value counts as
not set. Any other value hides that content and prints one warning that names
the variable and the accepted values, never the value, so a mistyped control
never leaves content visible. The warning names the variable also when the
value came from the `init` option or the file key. In `futureagi.toml` a hide
value may be a boolean, a number, a string in double or single quotes, or a bare
word (`hide_inputs = on`); a hide line whose value cannot be read is kept as
written, so it hides with that warning instead of being skipped.

With `RUBYOPT=-rtraceai/auto` the agent reads the environment and the config
file when it loads, before your code runs, and prints the warning then. An
`init` option for the same control still decides (`init(hide_inputs: false)`
shows the content), which is why the warning says the content is hidden "unless
an init option sets it".

`FI_WIRE_CAPTURE`, `FI_AUTO_DEBUG` and `OTEL_SDK_DISABLED` are not hide
controls: they are on only for `true`, in any letter case.

## Doctor and bootstrap

Run these from your application's directory, where they load your
application's bundle as your application does (prefixing `bundle exec` works
too). The executable loads the agent that ships next to it, so it needs neither
`RUBYLIB` nor a `traceai-auto` entry in your Gemfile:

```sh
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor              # report plus one test export
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor --no-export  # report only
ruby /path/to/traceAI/ruby/bin/traceai-auto bootstrap           # integration inventory
```

The test export sends one diagnostic span to Future AGI and reports `collector`
as `accepted` or `failed`; when it could not run, `collector_detail` says why.
The report never contains credentials. `bootstrap` only gives advice; it does not
install anything.

## Limitations

- Not covered: non-default Faraday adapters (Typhoeus, Excon, HTTP.rb), direct
  sockets, WebSockets, gRPC and custom HTTPX transport plugins.
- There are no library-level instrumentors in this gem; calls are traced at the
  HTTP layer. Rack server spans need `opentelemetry-instrumentation-rack` in the
  application's bundle and your own OpenTelemetry provider; the agent installs
  it only after your application has loaded Rack and set its provider, and the
  report shows `pending` until then. Your framework's instrumentation still
  decides where the Rack middleware goes. The `traceai-roast` gem is not enabled
  automatically.
- The built-in exporter does not use HTTP proxies (`HTTPS_PROXY` is ignored).
  It loads OpenSSL itself only when that cannot conflict with a version your
  application pins.
- Before the agent has started, request credentials are learned only when the
  process has a Future AGI key pair or an OpenTelemetry SDK tracer provider, and
  a proxy `CONNECT` is not observed: the proxy credentials of a connection
  opened before the first AI request are learned from later connections only.
- The agent's startup hooks (one on completed class and module definitions, and
  targeted ones on `Bundler.setup` and on setting the OpenTelemetry tracer
  provider) are turned off as soon as nothing is left to find, and in any case
  at the end of a startup window of 60 seconds after the agent loads. The
  window is fixed and cannot be changed in this release. It is checked when a class or module definition completes, so an idle
  process drops the hooks at its next definition. This also bounds the hooks in
  tools such as `bundle`, `gem` and `irb` when `RUBYOPT` is exported. After the
  window:
  - a client library loaded for the first time is not traced until you call
    `TraceAI::Auto.auto_instrument`;
  - a `Bundler.setup` that has still not run is no longer waited for, and the
    process is not traced (one warning at exit when keys are set);
  - a tracer provider set later is used from the next AI request instead of at
    once.
- If your application replaces its global tracer provider a second time, the
  agent stays on the first application provider it attached to.
- Future AGI receives only AI spans, so a span it receives can name a parent
  that it never receives: for example a wire span created under an HTTP client,
  job or controller span that carries no AI marker. The span keeps its real
  trace id and parent id, so it still lines up with the full trace in your own
  backend, but the parent is missing from the trace in Future AGI.
- Brotli and zstd bodies are not decoded. AWS Bedrock binary event streams keep
  request and status attributes only.
- Privacy recognition is structural, not general PII detection. Use the hide
  controls when content must never leave the process.
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
why `json` 2.6.3 is installed next to the bundle. The suite also runs under the
POSIX locale (`LANG=C LC_ALL=C bundle exec rake test`). The privacy and wire tests read
the shared specification in `spec/auto-instrumentation`; after the specification
changes, copy `privacy.json`, `privacy-fixtures.json` and `wire-protocols.json`
into `lib/traceai/auto` unchanged.
