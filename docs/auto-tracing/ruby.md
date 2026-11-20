# Zero-code tracing for Ruby

`traceai-auto` traces AI calls made from a Ruby application without code changes.
You start the application with one `RUBYOPT` flag. Recognized AI HTTP calls
(OpenAI, Anthropic, Gemini, Bedrock, Cohere, Ollama, speech and transcription
APIs, OpenAI-compatible gateways and more) made through `Net::HTTP` or `HTTPX`
become OpenTelemetry CLIENT spans with model, messages, usage, timing and errors.
The spans go to Future AGI or to your own OpenTelemetry destination. Tracing
starts only when you turn it on.

## Requirements

- CRuby 3.2 or newer. On an older Ruby the agent prints one warning and the
  application runs untraced. JRuby, TruffleRuby and Windows have not been tested.
- No runtime gem dependencies. The gem name is `traceai-auto` and the require
  path is `traceai/auto`.
- `opentelemetry-sdk` is optional. Without it in the bundle, or in a standalone
  script, the agent uses a dependency-free pipeline.

## Install

The gem is not on RubyGems yet; install it from this repository as shown below.

Version 0.1.0 runs from a checkout of the traceAI repository. Choose one of these
forms.

**Load path.** Point Ruby at the gem's `lib` folder:

```sh
export RUBYLIB="/path/to/traceAI/ruby/lib${RUBYLIB:+:$RUBYLIB}"
```

**Gemfile.** Add the gem with a `path:` source and `require: false`, so that
`RUBYOPT` stays the switch:

```ruby
gem 'traceai-auto', path: '/path/to/traceAI/ruby', require: false
```

Without `require: false`, `Bundler.require` (which Rails runs at boot) loads the
agent in every process that loads the bundle: servers, consoles, rake tasks and
test suites.

**Installed gem outside the bundle.** If `traceai-auto` is installed separately
and your `Gemfile` does not include it, preload its entry file by absolute path:

```sh
RUBYOPT="${RUBYOPT:+$RUBYOPT }-r/absolute/gem-home/gems/traceai-auto-0.1.0/lib/traceai/auto.rb" \
  bundle exec ruby app.rb
```

Replace the placeholder with the real path for the Ruby you run, and use a path
without spaces. Installing the gem does not turn tracing on by itself.

## Turn it on

Append `-rtraceai/auto` to `RUBYOPT` and start the application as usual:

```sh
export RUBYOPT="${RUBYOPT:+$RUBYOPT }-rtraceai/auto"
bundle exec ruby app.rb
```

- Keep any existing `RUBYOPT` value and append to it.
- Set `RUBYOPT` only for the processes you want traced. Exported in a shell
  profile or an image, it turns the agent on in every Ruby process started there.
- In a directory with a `Gemfile`, use `bundle exec`, or make sure the script
  runs `require 'bundler/setup'`. In a directory without a `Gemfile`,
  `ruby app.rb` works as is.
- The agent waits for your application's own Bundler setup. It does not call
  `Bundler.setup` or require your gems for you.

To turn the agent on from code instead, use `require 'traceai/auto'`.
`OTEL_SDK_DISABLED=true` turns the agent off.

## Send traces to Future AGI

Set the Future AGI key pair before you start the application:

```sh
export FI_API_KEY=your-api-key
export FI_SECRET_KEY=your-secret-key
```

| Variable | Meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Future AGI credentials. Used only as a complete pair from one source. |
| `FI_BASE_URL` | Collector base URL. Default `https://api.futureagi.com`. Spans go to `${FI_BASE_URL}/tracer/v1/traces` as OTLP/HTTP protobuf. |
| `FI_PROJECT_NAME`, `FI_PROJECT_TYPE`, `FI_PROJECT_VERSION_NAME` | Project routing. The default name is `OTEL_SERVICE_NAME`, then the working directory name. |
| `FI_SAMPLE_RATE` | Parent-based ratio sampler for a provider the agent creates. |

Settings are read in this order: explicit `init` arguments, then environment
variables, then the config file (`FI_CONFIG_FILE`, default `./futureagi.toml`),
then defaults. A `base_url` in the config file is used only when the key pair
comes from the same file.

```toml
# futureagi.toml
project_name = "checkout"
instrumentations = ["wire", "rack"]
wire_max_body_bytes = 4194304
sample_rate = 1.0
```

Future AGI receives only AI spans. Your application's own OpenTelemetry provider
and its other spans stay with your own exporters.

**Your own OpenTelemetry destination.** The FI keys are optional when the
application already sets an OpenTelemetry SDK tracer provider; the agent uses it
automatically. You can also pass a destination in code, before the first AI
request:

```ruby
require 'opentelemetry/sdk'
require 'traceai/auto/agent'
TraceAI::Auto.init(provider: my_tracer_provider)
# or exporter: my_exporter, or processor: my_span_processor
```

To export to Future AGI as well as to explicit destinations, pass
`fi_export: true` and a complete FI key pair.

## Hide prompt and response content

Content capture is on by default. Each hide control replaces that content with
`__REDACTED__` in the copy the agent exports. Other exporters in your application
keep their original data.

Set a control as an environment variable, as an `init` option or as a
`futureagi.toml` key. The option and the file key are the variable's name in
lower case without `FI_`:

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

- `true`, `1`, `yes` or `on` hide; `false`, `0`, `no` or `off` show. Case and
  surrounding spaces are ignored, and a blank value counts as not set.
- Any other value hides that content and prints one warning that names the
  variable and the accepted values.
- An `init` option for the same control decides over the environment and the
  file, for example `init(hide_inputs: false)`.
- Before export, the agent also masks credential-named fields and
  credential-shaped values, and omits media such as data URLs and base64 images
  and audio. This is structural recognition, not general PII detection; use the
  hide controls when content must stay in the process.

## What gets traced

The agent recognizes the 25 wire formats of the shared catalog: chat, Responses,
completions, embeddings, rerank, moderation, image generation, speech and
transcription. Unknown hosts are captured when the path and body match a known
format.

| Transport | Coverage |
| --- | --- |
| `Net::HTTP` | Buffered and streamed requests and responses, multipart uploads, gzip and deflate. Covers Faraday's default adapter, `ruby-openai`, the official `openai` and `anthropic` gems, and RubyLLM. |
| `HTTPX` | Buffered and `:stream` responses, retries and `:follow_redirects`. |

**RubyLLM.** RubyLLM 2.x (`>= 2.0, < 3`; tested at 2.0.0, 2.1.1 and 2.2.3) is
traced without application code. Each model request made by `Chat#ask`,
`#complete` or `#generate` gives a `chat <model>` CLIENT span, and each tool call
gives an `execute_tool <name>` INTERNAL span. RubyLLM 1.x and 3.x are not hooked
and keep their wire spans.

**Rack.** Server spans need `opentelemetry-instrumentation-rack` in the
application's bundle and your own OpenTelemetry provider.

## Turn integrations off or choose them

| Variable | Meaning |
| --- | --- |
| `FI_INSTRUMENTATIONS` | Allowlist. It must name every integration you want, for example `wire,rack,ruby_llm`. |
| `FI_DISABLED_INSTRUMENTATIONS` | Denylist, for example `ruby_llm`. |
| `FI_WIRE_CAPTURE` | `false` turns off wire capture. |
| `FI_WIRE_MAX_BODY_BYTES` | Body capture limit, default 4 MiB, at most 64 MiB. |

Integration names: `wire` covers both transports; `net_http` or `httpx` alone
enables the wire layer for that transport only; `rack` controls server spans;
`ruby_llm` controls the RubyLLM spans. With `ruby_llm` turned off, RubyLLM's HTTP
calls are reported by the wire layer alone.

## Check that it works

Run these from your application's directory. They load your application's bundle
the way your application does, and need neither `RUBYLIB` nor a Gemfile entry:

```sh
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor              # report plus one test export
ruby /path/to/traceAI/ruby/bin/traceai-auto doctor --no-export  # report only
ruby /path/to/traceAI/ruby/bin/traceai-auto bootstrap           # integration inventory
```

The test export sends one diagnostic span to Future AGI and reports `collector`
as `accepted` or `failed`. When it could not run, `collector_detail` says why.
`bootstrap` gives advice only; it installs nothing. `TraceAI::Auto.report` gives
the same report from code. Set `FI_AUTO_DEBUG=true` to print a startup report.

## Troubleshooting

- **Nothing is traced in a Bundler project.** Start with `bundle exec`, or have
  the script run `require 'bundler/setup'`. When a key pair is set, the agent
  prints one warning at exit in this case.
- **The installed gem is not found.** Bare `-rtraceai/auto` depends on load-path
  lookup. For a gem outside the application's bundle, use the absolute-path
  `RUBYOPT` form from Install.
- **A client loaded after boot is not traced.** Call
  `TraceAI::Auto.auto_instrument` after loading it, for example
  `TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])`.
- **Settings passed to `init` have no effect.** Call `init` before the first AI
  request and before the first `auto_instrument` call that starts tracing. A
  later `init` changes nothing and prints an "already initialized" warning.
- **A hide line in `futureagi.toml` is ignored.** Write the lower-case key
  (`hide_inputs = true`). A line that uses the variable name
  (`FI_HIDE_INPUTS = true`) is not a hide control.
- **Export does not use your proxy.** The built-in exporter connects directly;
  `HTTPS_PROXY` and other proxy settings are not used.

## Limits

- Not covered: non-default Faraday adapters (Typhoeus, Excon, HTTP.rb), direct
  sockets, WebSockets, gRPC and custom HTTPX transport plugins.
- Apart from RubyLLM, calls are traced at the HTTP layer. The `traceai-roast` gem
  is not enabled automatically.
- Brotli and zstd bodies are not decoded. AWS Bedrock binary event streams keep
  request and status attributes only.
- Startup discovery runs for at most 60 seconds after the agent loads. After that,
  a client first loaded outside the Bundler boot callback needs
  `TraceAI::Auto.auto_instrument`.
- If your application replaces its global tracer provider a second time, the
  agent stays on the first application provider it attached to.
- A span in Future AGI can name a parent that Future AGI does not receive, such
  as an HTTP client or controller span with no AI marker. The trace still lines
  up in your own backend.
- The context helpers apply to the current Fiber. Other threads and Fibers must
  propagate their own OpenTelemetry context.

## More detail

- [traceai-auto README](../../ruby/README.md): full configuration, sampling,
  RubyLLM spans, context helpers and every limitation.
- [Auto-instrumentation contract](../../spec/auto-instrumentation/README.md): the
  shared wire catalog and the behaviour all languages implement.
