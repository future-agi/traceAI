# frozen_string_literal: true
require_relative 'test_helper'
class EndToEndTest < Minitest::Test
  include ChildHelper
  CHAT = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: 'Hello from Ruby'}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5}).freeze
  STREAM = ["data: #{JSON.generate(id: 'c', model: 'gpt-4o-mini', choices: [{index: 0, delta: {role: 'assistant', content: 'one'}}])}\n\n",
            "data: #{JSON.generate(id: 'c', model: 'gpt-4o-mini', choices: [{index: 0, delta: {content: ' two'}, finish_reason: 'stop'}], usage: {prompt_tokens: 4, completion_tokens: 2})}\n\n", "data: [DONE]\n\n"].freeze
  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, CHAT] }
  end
  def teardown
    @provider.stop; @collector.stop
  end
  def plain_code(stream: false)
    <<~RUBY
      require 'net/http'; require 'json'
      uri = URI('#{@provider.url}/v1/chat/completions')
      req = Net::HTTP::Post.new(uri); req['Content-Type'] = 'application/json'
      req.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}], stream: #{stream})
      #{stream ? "Net::HTTP.start(uri.host, uri.port) { |http| http.request(req) { |r| r.read_body { |s| STDOUT.write(s) } } }" : 'STDOUT.write(Net::HTTP.start(uri.host, uri.port) { |http| http.request(req).body })'}
    RUBY
  end
  def assert_chat_span(spans)
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'chat gpt-4o-mini', span.name
    assert_equal :SPAN_KIND_CLIENT, span.kind
    assert_equal :STATUS_CODE_OK, span.status.code
    assert span.end_time_unix_nano >= span.start_time_unix_nano
    assert_equal 'ruby-e2e', resource['project_name']; assert_equal 'observe', resource['project_type']
    assert_equal '[]', resource['eval_tags']; assert_equal '{}', resource['metadata']; assert_equal 'fi', resource['semantic_convention']
    assert_equal 'DEFAULT_PROJECT_VERSION_NAME', resource['project_version_name']; assert_match(/\A[0-9a-f-]{36}\z/, resource['project_version_id'])
    attrs(span.attributes)
  end
  def test_plain_app_no_tracing_code
    stdout, stderr, status = app(plain_code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    a = assert_chat_span(export_spans)
    assert_equal 'wire', a['traceai.capture.layer']; assert_equal 'Hello from Ruby', a['output.value']; assert_equal 5, a['gen_ai.usage.total_tokens']
  end
  # A container started with LANG=C reads files and the environment as US-ASCII.
  # The agent must still load, read its UTF-8 config and export non-ASCII text.
  def test_posix_locale_loads_agent_and_exports_utf8_wire_span
    text = "Ol\u{e1} \u{1f600}"
    reply = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: text}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5})
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, reply] }
    config = File.join(ROOT, '.bundle/tmp', "posix-locale-#{Process.pid}.toml")
    File.binwrite(config, "project_name = \"caf\u{e9} \u{1f600}\"\n".b)
    # The child code stays ASCII: a non-ASCII literal in -e is invalid under LANG=C.
    code = plain_code + <<~RUBY
      name = TraceAI::Auto.config['project_name']
      abort 'config value is ' + name.encoding.name unless name.encoding == Encoding::UTF_8 && name.valid_encoding?
    RUBY
    locale = {'LANG' => 'C', 'LC_ALL' => 'C', 'LC_CTYPE' => nil, 'LANGUAGE' => nil}
    stdout, stderr, status = app(code, locale.merge('FI_PROJECT_NAME' => nil, 'FI_CONFIG_FILE' => config, 'FI_PROJECT_VERSION_NAME' => "v\u{e9}"))
    assert status.success?, stderr.b
    assert_equal reply.b, stdout.b
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    a = attrs(span.attributes)
    assert_equal 'wire', a['traceai.capture.layer']
    assert_equal text, a['output.value']
    assert_equal "caf\u{e9} \u{1f600}", resource['project_name']
    assert_equal "v\u{e9}", resource['project_version_name']
  ensure
    File.unlink(config) if config && File.exist?(config)
  end
  POSIX_LOCALE = {'LANG' => 'C', 'LC_ALL' => 'C', 'LC_CTYPE' => nil, 'LANGUAGE' => nil}.freeze
  # Under LANG=C a credential-named environment value with non-ASCII bytes is
  # tagged US-ASCII. It must still be removed from exported text.
  def test_posix_locale_redacts_a_non_ascii_environment_credential_echoed_by_the_provider
    secret = "sch\u{f6}n-pa\u{df}wort-12345678"
    reply = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: "echo #{secret}"}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5})
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, reply] }
    stdout, stderr, status = app(plain_code, POSIX_LOCALE.merge('TEST_UPSTREAM_PASSWORD' => secret))
    assert status.success?, stderr.b
    assert_equal reply.b, stdout.b
    spans = export_spans
    assert_equal 1, spans.length
    span = spans.first.first
    values = attrs(span.attributes)
    assert_equal 'echo <redacted>', values['output.value']
    # Compare bytes: inspect output escapes non-ASCII text under LANG=C.
    leaked = values.select { |_, value| value.is_a?(String) && value.b.include?(secret.b) }.keys
    assert_empty leaked
  end
  # The application's own SDK resource holds environment values as the SDK read
  # them. Under LANG=C a non-ASCII service name must not cost the wire span its
  # content or its resource. The SDK reads OTEL_SERVICE_NAME only into its
  # default resource, the one OpenTelemetry::SDK.configure uses; a bare
  # TracerProvider.new has an empty resource. So this application gives its
  # provider the default resource, as an application that names its service does.
  def test_posix_locale_keeps_span_content_with_a_non_ascii_service_name_on_the_application_provider
    service = "caf\u{e9}-service"
    sdk = "require 'opentelemetry/sdk'\n" \
          "OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new(resource: OpenTelemetry::SDK::Resources::Resource.default) unless OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)\n"
    stdout, stderr, status = app(sdk + plain_code, POSIX_LOCALE.merge('OTEL_SERVICE_NAME' => service))
    assert status.success?, stderr.b
    assert_equal CHAT, stdout
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'Hello from Ruby', attrs(span.attributes)['output.value']
    assert_equal 'ruby-e2e', resource['project_name']
    assert_equal service, resource['service.name']
  end
  def test_bundle_exec_with_rubyopt
    stdout, stderr, status = app(plain_code, {}, bundled: true)
    assert status.success?, stderr; assert_equal CHAT, stdout; assert_chat_span(export_spans)
  end
  def test_official_openai_gem
    code = <<~RUBY
      gem 'openai', '0.75.0'; require File.join(Gem::Specification.find_by_name('openai').full_gem_path, 'lib/openai.rb')
      c = OpenAI::Client.new(api_key: 'mock-openai-api', base_url: '#{@provider.url}/v1', max_retries: 0)
      r = c.chat.completions.create(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hello'}])
      puts r.choices.first.message.content
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "Hello from Ruby\n", stdout
    assert_chat_span(export_spans)
  end
  def test_net_http_stream_byte_passthrough
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    stdout, stderr, status = app(plain_code(stream: true))
    assert status.success?, stderr; assert_equal STREAM.join, stdout
    a = assert_chat_span(export_spans)
    assert_equal 'one two', a['output.value']; assert_equal true, a['gen_ai.request.stream']; assert a['gen_ai.response.time_to_first_chunk'] >= 0
  end
  def test_official_openai_stream
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = <<~RUBY
      gem 'openai', '0.75.0'; require File.join(Gem::Specification.find_by_name('openai').full_gem_path, 'lib/openai.rb')
      c = OpenAI::Client.new(api_key: 'mock-openai-api', base_url: '#{@provider.url}/v1', max_retries: 0)
      c.chat.completions.stream(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}]).each { |event| print event.chunk.choices.first&.delta&.content if event.type == :chunk }
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal 'one two', stdout
    assert_equal 'one two', assert_chat_span(export_spans)['output.value']
  end
  def test_httpx_buffered_and_streamed
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = <<~RUBY
      require 'httpx'
      opts = {json: {model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}], stream: true}}
      print HTTPX.post('#{@provider.url}/v1/chat/completions', **opts).to_s
      HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', **opts, stream: true).each { |s| print s }
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal STREAM.join * 2, stdout
    spans = export_spans; assert_equal 2, spans.length
    spans.each { |s, _| assert_equal 'one two', attrs(s.attributes)['output.value'], attrs(s.attributes).inspect }
  end
  def test_anthropic_official_client
    @provider.stop
    body = JSON.generate(id: 'msg-1', type: 'message', role: 'assistant', model: 'claude-test', content: [{type: 'text', text: 'anthropic local'}], stop_reason: 'end_turn', usage: {input_tokens: 3, output_tokens: 2})
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, body] }
    code = app_sdk + <<~RUBY
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      require 'anthropic'
      c = Anthropic::Client.new(api_key: 'mock-anthropic-api', base_url: '#{@provider.url}', max_retries: 0)
      r = c.messages.create(model: 'claude-test', max_tokens: 16, messages: [{role: 'user', content: 'hi'}])
      puts r.content.first.text
      native = memory.finished_spans.find { |s| s.name == 'anthropic.messages.create' }
      abort 'native span missing from the application exporter' unless native
      puts native.span_id.unpack1('H*')
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    output, native_id = stdout.split("\n")
    assert_equal 'anthropic local', output
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal 1, wire.length
    assert_equal 'chat claude-test', wire.first.name
    assert_equal 'anthropic local', attrs(wire.first.attributes)['output.value']
    # Anthropic 2.3 has a vendor-native span without traceAI's dedup marker;
    # preserve it and parent the wire child rather than modifying vendor tracing.
    assert_equal native_id, wire.first.parent_span_id.unpack1('H*')
    # The vendor span carries no gen_ai.span.kind, so it stays with the
    # application's own exporters and is not sent to Future AGI.
    assert_equal [wire.first.span_id], spans.map(&:span_id)
  end
  def test_ruby_llm_default_adapter
    code = <<~RUBY
      require 'ruby_llm'
      RubyLLM.configure { |c| c.openai_api_key = 'mock-key'; c.openai_api_base = '#{@provider.url}/v1' }
      c = RubyLLM.chat(model: 'gpt-4o-mini', provider: :openai, protocol: :chat_completions, assume_model_exists: true)
      puts c.ask('hi').content
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "Hello from Ruby\n", stdout
    assert_chat_span(export_spans)
  end
  def test_ruby_openai_default_adapter
    code = <<~RUBY
      gem 'ruby-openai'; require File.join(Gem::Specification.find_by_name('ruby-openai').full_gem_path, 'lib/openai.rb')
      c = OpenAI::Client.new(access_token: 'mock-key', uri_base: '#{@provider.url}/v1')
      r = c.chat(parameters: {model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}]})
      puts r.dig('choices', 0, 'message', 'content')
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "Hello from Ruby\n", stdout
    assert_chat_span(export_spans)
  end
  def test_gzip_stream_net_http_and_httpx_passthrough
    io = StringIO.new; gz = Zlib::GzipWriter.new(io); gz.write(STREAM.join); gz.close; compressed = io.string
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream', 'Content-Encoding' => 'gzip'}, compressed] }
    stdout, stderr, status = app(plain_code(stream: true))
    assert status.success?, stderr; assert_equal STREAM.join, stdout
    assert_equal 'one two', assert_chat_span(export_spans)['output.value']
    code = "require 'httpx'; print HTTPX.post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: []}).to_s"
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal STREAM.join, stdout
    assert_equal 'one two', assert_chat_span(export_spans)['output.value']
  end
  def test_transport_failure_after_stream_started
    @provider.raw do |io, _|
      io.write "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
      chunk = STREAM.first; io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\n"; io.flush
      # Close before the terminal chunk; Net::HTTP raises EOFError.
    end
    stdout, stderr, status = app("begin\n#{plain_code(stream: true)}\nrescue EOFError; puts 'EOF-PRESERVED'; end")
    assert status.success?, stderr; assert_includes stdout, 'EOF-PRESERVED'
    spans = export_spans; assert_equal 1, spans.length
    s = spans.first.first; assert_equal :STATUS_CODE_ERROR, s.status.code
    assert_equal 'one', attrs(s.attributes)['output.value']; assert_equal 'EOFError', attrs(s.attributes)['error.type']
  end
  def test_multipart_non_replayable_upload
    code = <<~RUBY
      require 'net/http'; require 'stringio'
      class OneShot
        def initialize(data); @io = StringIO.new(data); end
        def read(*args); @io.read(*args); end
        def rewind; abort 'instrumentation rewound request'; end
      end
      body = "--b\\r\\nContent-Disposition: form-data; name=\\\"model\\\"\\r\\n\\r\\nwhisper-1\\r\\n--b\\r\\nContent-Disposition: form-data; name=\\\"file\\\"; filename=\\\"audio.wav\\\"\\r\\nContent-Type: audio/wav\\r\\n\\r\\n" + ('x' * 5_242_880) + "\\r\\n--b--\\r\\n"
      uri = URI('#{@provider.url}/v1/audio/transcriptions'); req = Net::HTTP::Post.new(uri)
      req['Content-Type'] = 'multipart/form-data; boundary=b'; req['Content-Length'] = body.bytesize.to_s
      req.body_stream = OneShot.new(body)
      puts Net::HTTP.start(uri.host, uri.port) { |h| h.request(req).code }
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "200\n", stdout
    spans = export_spans; assert_equal 1, spans.length
    a = attrs(spans.first.first.attributes); assert_equal 5_242_880, a['traceai.audio.input_bytes']; assert_equal 'whisper-1', a['gen_ai.request.model']
    refute a['traceai.wire.body_unavailable']; refute_includes a.to_s, 'xxxxxxxxxxxxxxxxxxxxxxxx'
  end
  def test_flush_deadline_is_bounded_and_non_reentrant
    code = <<~RUBY
      p = Object.new
      def p.force_flush(timeout:); sleep 60; end
      TraceAI::Auto.instance_variable_set(:@processor, p)
      TraceAI::Auto.instance_variable_set(:@owned_processors, [p])
      t = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      a = TraceAI::Auto.flush(timeout: 0.05)
      b = TraceAI::Auto.flush(timeout: 0.05)
      elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - t
      abort 'flush exceeded deadline' if elapsed > 0.5 || a || b
      puts 'BOUNDED'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "BOUNDED\n", stdout
  end
  def test_retry_after_cannot_delay_exit
    @collector.stop; @collector = MockHTTP.new { |_r| [503, {'Retry-After' => '86400'}, 'unavailable'] }
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    stdout, stderr, status = app(plain_code)
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    assert status.success?, stderr; assert_equal CHAT, stdout; assert elapsed < 6
    assert_includes stderr, 'collector export failed'
  end
  # With the OpenTelemetry SDK in the bundle, a RUBYOPT preload starts nothing
  # until the application configures a provider, calls init or makes an AI
  # request. An explicit init after the preload therefore applies its settings.
  def test_explicit_init_after_preload_applies_its_settings
    code = <<~RUBY
      abort 'agent started before any trigger' if TraceAI::Auto.provider
      result = TraceAI::Auto.init(project_name: 'argument-project', sample_rate: 1.0)
      abort 'init returned another provider' unless result.equal?(TraceAI::Auto.provider)
      abort 'agent provider made global' if OpenTelemetry.tracer_provider.equal?(result)
      abort 'second init rewired the agent' unless TraceAI::Auto.init(project_name: 'second-project').equal?(result)
    RUBY
    _, stderr, status = app(code + plain_code, {'FI_PROJECT_NAME' => 'environment-project', 'FI_SAMPLE_RATE' => '0'})
    assert status.success?, stderr
    assert_equal 1, stderr.scan('already initialized').length
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'chat gpt-4o-mini', span.name
    assert_equal 'argument-project', resource['project_name']
  end

  # The application configures its own SDK after Bundler.setup, as a Rails
  # initializer does. The agent attaches to that provider: the wire span reaches
  # both the application's exporter and Future AGI, under the application span.
  def test_application_provider_configured_after_bundler_setup_gets_wire_spans
    code = <<~RUBY
      require 'bundler/setup'
      require 'opentelemetry/sdk'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)) }
      abort 'agent did not attach to the application provider' unless TraceAI::Auto.provider.equal?(OpenTelemetry.tracer_provider)
      OpenTelemetry.tracer_provider.tracer('app').in_span('app-request', attributes: {'gen_ai.span.kind' => 'AGENT'}) do
        #{plain_code}
      end
      parent = memory.finished_spans.find { |s| s.name == 'app-request' }
      wire = memory.finished_spans.select { |s| s.attributes['traceai.capture.layer'] == 'wire' }
      abort 'wire span missing from the application exporter' unless parent && wire.length == 1
      abort 'wire span not under the application span' unless wire.first.parent_span_id == parent.span_id
      puts
      puts parent.span_id.unpack1('H*')
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    body, parent_id = stdout.split("\n", 2)
    assert_equal CHAT, body
    spans = export_spans.map(&:first)
    wire = spans.find { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }; refute_nil wire
    assert_equal parent_id.strip, wire.parent_span_id.unpack1('H*')
    assert_includes spans.map(&:name), 'app-request'
  end

  # An explicit init(provider:) after the RUBYOPT preload still attaches to the
  # supplied provider instead of being refused, and makes no provider global.
  def test_explicit_init_with_provider_after_preload_attaches_to_it
    code = <<~RUBY
      require 'opentelemetry/sdk'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      abort 'supplied provider not used' unless TraceAI::Auto.init(provider: host, fi_export: true).equal?(host)
      abort 'a provider was made global' if OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
      host.tracer('app').in_span('app-parent', attributes: {'gen_ai.span.kind' => 'CHAIN'}) do
        #{plain_code}
      end
      abort 'wire span missing from the supplied provider' unless memory.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    refute_includes stderr, 'already initialized'
    spans = export_spans.map(&:first)
    wire = spans.find { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }; refute_nil wire
    assert_equal spans.find { |s| s.name == 'app-parent' }.span_id, wire.parent_span_id
  end

  # Only an AI request (one the wire catalog matches) or an explicit init
  # starts the agent. A request that is not an AI request, such as a secrets
  # fetch or a health check at boot, starts nothing, so a later explicit
  # init(provider:) is not refused and gets the wire spans.
  def test_non_ai_request_at_boot_does_not_use_up_the_start
    code = <<~RUBY
      require 'net/http'
      Net::HTTP.get(URI('#{@provider.url}/health'))
      abort 'a request that is not an AI request started the agent' if TraceAI::Auto.provider || TraceAI::Auto.active?
      require 'opentelemetry/sdk'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      abort 'supplied provider not used' unless TraceAI::Auto.init(provider: host, fi_export: true).equal?(host)
      #{plain_code}
      abort 'wire span missing from the supplied provider' unless memory.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    refute_includes stderr, 'already initialized'
    assert_chat_span(export_spans)
  end

  # The context helpers record their attributes without starting the agent. A
  # helper call alone starts nothing, a later explicit init applies its
  # settings, and a helper around an AI request still tags its wire span.
  def test_context_helpers_alone_start_nothing
    code = <<~RUBY
      result = TraceAI::Auto.using_session('s1') do
        TraceAI::Auto.using_user('u1') do
          TraceAI::Auto.using_metadata({'tier' => 'gold'}) { TraceAI::Auto.with_attributes({'tag.tenant' => 'acme'}) { :block_result } }
        end
      end
      abort 'helper changed the block result' unless result == :block_result
      abort 'a context helper started the agent' if TraceAI::Auto.provider || TraceAI::Auto.active?
      started = TraceAI::Auto.init(project_name: 'after-helpers')
      abort 'init did not start the agent' unless TraceAI::Auto.active? && started.equal?(TraceAI::Auto.provider)
      TraceAI::Auto.using_session('s2') do
        #{plain_code}
      end
    RUBY
    stdout, stderr, status = app(code, {'FI_PROJECT_NAME' => 'environment-project'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    refute_includes stderr, 'already initialized'
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'after-helpers', resource['project_name']
    assert_equal 's2', attrs(span.attributes)['session.id']
  end

  # A helper that wraps the very first AI request does not start the agent
  # itself; the request does, and its wire span still carries the context.
  def test_context_helpers_around_the_first_ai_request_tag_its_wire_span
    code = <<~RUBY
      TraceAI::Auto.using_session('first-session') do
        TraceAI::Auto.using_user('first-user') do
          TraceAI::Auto.using_metadata({'tier' => 'gold'}) do
            abort 'a context helper started the agent' if TraceAI::Auto.active?
            #{plain_code}
          end
        end
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    a = assert_chat_span(export_spans)
    assert_equal 'first-session', a['session.id']
    assert_equal 'first-user', a['user.id']
    assert_equal({'tier' => 'gold'}, JSON.parse(a['metadata']))
  end

  # The usual order in a web application: its initializer configures the
  # OpenTelemetry SDK, which is when the agent attaches, and only then calls
  # TraceAI::Auto.init. Before the first AI request that init still applies its
  # settings and its supplied exporter.
  def test_explicit_init_after_the_application_set_its_provider_applies_its_settings
    code = <<~RUBY
      require 'opentelemetry/sdk'
      own = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(own)) }
      abort 'agent did not attach to the application provider' unless TraceAI::Auto.provider.equal?(OpenTelemetry.tracer_provider)
      supplied = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      result = TraceAI::Auto.init(exporter: supplied, project_name: 'explicit-project', fi_export: true)
      abort 'init left the application provider' unless result.equal?(OpenTelemetry.tracer_provider)
      #{plain_code}
      abort 'flush failed' unless TraceAI::Auto.flush
      abort 'wire span missing from the supplied exporter' unless supplied.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
      abort 'wire span missing from the application exporter' unless own.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
    RUBY
    stdout, stderr, status = app(code, {'FI_PROJECT_NAME' => 'environment-project'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    refute_includes stderr, 'already initialized'
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'chat gpt-4o-mini', span.name
    assert_equal 'explicit-project', resource['project_name']
  end

  # A credential sent on a request made before the agent started is still
  # learned, so a later AI span cannot echo it.
  def test_credentials_on_a_request_made_before_the_agent_started_are_still_learned
    key = 'test-earlier-request-credential-12345678'
    reply = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: "echo #{key}"}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5})
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, reply] }
    code = <<~RUBY
      require 'net/http'
      health = Net::HTTP::Get.new('/health'); health['x-vault-token'] = '#{key}'
      Net::HTTP.start('127.0.0.1', #{@provider.port}) { |http| http.request(health) }
      abort 'a request that is not an AI request started the agent' if TraceAI::Auto.active?
    RUBY
    stdout, stderr, status = app(code + plain_code)
    assert status.success?, stderr; assert_equal reply, stdout
    a = assert_chat_span(export_spans)
    assert_equal 'echo <redacted>', a['output.value']
  end

  # Future AGI receives only AI spans. An application span without
  # gen_ai.span.kind, such as a database query, stays with the application.
  def test_application_span_with_db_statement_never_reaches_future_agi
    code = app_sdk + <<~RUBY
      t = OpenTelemetry.tracer_provider.tracer('app')
      t.in_span('db-query', attributes: {'db.statement' => 'SELECT private_column FROM accounts'}) {}
      t.in_span('framework', attributes: {'gen_ai.span.kind' => 'CHAIN'}) do
        t.in_span('db-inside-ai-trace', attributes: {'db.statement' => 'SELECT private_column FROM sessions'}) {}
        #{plain_code}
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    spans = export_spans.map(&:first)
    assert_equal %w[chat\ gpt-4o-mini framework], spans.map(&:name)
    refute_includes spans.map(&:to_h).to_s, 'private_column'
  end

  # A server span goes to Future AGI only when its trace contained an AI span.
  def test_server_span_reaches_future_agi_only_with_an_ai_span_in_its_trace
    code = app_sdk + <<~RUBY
      t = OpenTelemetry.tracer_provider.tracer('app')
      t.in_span('GET /health', kind: :server) {}
      t.in_span('POST /chat', kind: :server) do
        #{plain_code}
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    spans = export_spans.map(&:first)
    assert_equal ['chat gpt-4o-mini', 'POST /chat'], spans.map(&:name)
    assert_equal spans.last.span_id, spans.first.parent_span_id
  end
  def test_doctor_masks_and_exports_diagnostic
    code = "require 'json'; puts JSON.generate(TraceAI::Auto.report(test_export: true))"
    stdout, stderr, status = app(code)
    assert status.success?, stderr; report = JSON.parse(stdout)
    assert_equal 'accepted', report['collector']; assert_equal '<redacted>', report['config']['api_key']
    assert_equal 'instrumented', report.dig('integrations', 'net_http', 'status')
    assert_equal 'missing_instrumentor', report.dig('integrations', 'openai', 'status')
    assert_equal 'pending', report.dig('integrations', 'httpx', 'status')
    assert_equal 'traceai-auto doctor', export_spans.first.first.name
  end
  # The documented command, run without bundle exec: in a plain directory the
  # dependency-free exporter performs the test export; in an application
  # directory the command loads the application's bundle first.
  def test_doctor_command_tests_the_collector_with_and_without_a_gemfile
    standalone = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('RUBYOPT' => nil)
    code = "ARGV.replace(%w[doctor]); load #{File.join(ROOT, 'bin/traceai-auto').inspect}"
    Dir.mktmpdir('doctor-app', File.join(ROOT, '.bundle/tmp')) do |directory|
      plain = File.join(directory, 'plain'); bundled = File.join(directory, 'bundled')
      [plain, bundled].each { |path| Dir.mkdir(path) }
      File.write(File.join(bundled, 'Gemfile'), "source 'https://rubygems.org'\n")
      [plain, bundled].each do |chdir|
        stdout, stderr, status = app(code, standalone, chdir: chdir)
        assert status.success?, stderr
        report = JSON.parse(stdout)
        assert_equal true, report['active'], chdir
        assert_equal 'accepted', report['collector'], chdir
        assert_equal ['traceai-auto doctor'], export_spans.map { |span, _| span.name }, chdir
      end
    end
  end
  # The executable finds the agent next to itself. It needs neither RUBYLIB nor
  # a traceai-auto entry in the application's Gemfile, so it also works as an
  # installed command in an application whose bundle does not list the gem.
  def test_doctor_executable_loads_its_own_agent_without_rubylib_or_a_gemfile_entry
    standalone = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('RUBYOPT' => nil, 'RUBYLIB' => nil)
    Dir.mktmpdir('doctor-executable', File.join(ROOT, '.bundle/tmp')) do |directory|
      plain = File.join(directory, 'plain'); bundled = File.join(directory, 'bundled')
      [plain, bundled].each { |path| Dir.mkdir(path) }
      File.write(File.join(bundled, 'Gemfile'), "source 'https://rubygems.org'\n")
      [plain, bundled].each do |chdir|
        stdout, stderr, status = Timeout.timeout(15) do
          Open3.capture3(child_env(standalone), RbConfig.ruby, File.join(ROOT, 'bin/traceai-auto'), 'doctor', chdir: chdir)
        end
        assert status.success?, stderr
        report = JSON.parse(stdout)
        assert_equal true, report['active'], chdir
        assert_equal 'accepted', report['collector'], chdir
        assert_equal ['traceai-auto doctor'], export_spans.map { |span, _| span.name }, chdir
      end
    end
  end
  # Rack server spans are installed only after the application's own
  # OpenTelemetry configuration and once Rack is loaded. Until then the report
  # says pending, never failed. (The Rack instrumentation gem is not in this
  # bundle, so the child registers a stand-in specification for it.)
  def test_rack_instrumentation_is_pending_until_rack_and_the_application_provider_exist
    code = <<~RUBY
      require 'json'
      Gem.loaded_specs['opentelemetry-instrumentation-rack'] = Gem::Specification.new { |s| s.name = 'opentelemetry-instrumentation-rack'; s.version = '0.0.0' }
      puts JSON.generate(TraceAI::Auto.report[:integrations]['rack'])
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal({'status' => 'pending'}, JSON.parse(stdout))
  end
  # net_http and httpx in the allowlist enable the wire layer for that
  # transport only; they no longer switch all wire capture off.
  def test_transport_names_in_the_allowlist_enable_that_wire_transport
    stdout, stderr, status = app(plain_code, {'FI_INSTRUMENTATIONS' => 'net_http'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert_chat_span(export_spans)
    stdout, stderr, status = app(plain_code, {'FI_INSTRUMENTATIONS' => 'httpx,rack'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert_empty export_spans
  end
  def test_incomplete_key_pair_warns_once
    _, stderr, status = app(plain_code, {'FI_SECRET_KEY' => nil})
    assert status.success?, stderr
    assert_equal 1, stderr.scan('environment credential pair ignored').length, stderr
  end
  # Ruby's default SIGTERM handling already runs at_exit hooks, where the agent
  # flushes. The agent must not replace the application's signal handler.
  def test_agent_installs_no_sigterm_handler
    stdout, stderr, status = app(plain_code + "\nputs Signal.trap('TERM', 'DEFAULT').is_a?(Proc)\n")
    assert status.success?, stderr; assert_equal CHAT + "false\n", stdout
    assert_chat_span(export_spans)
  end
  # A live application SDK provider is enough for the SDK path, even when the
  # bundle did not select every default gem the SDK uses.
  def test_live_application_sdk_is_enough_without_every_default_gem_selected
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'json'
      %w[uri timeout logger].each { |name| Gem.loaded_specs.delete(name) }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      require 'traceai/auto/agent'
      TraceAI::Auto.init
      abort 'agent inactive' unless TraceAI::Auto.active? && TraceAI::Auto.provider.equal?(host)
      response = Net::HTTP.post(URI('#{@provider.url}/v1/chat/completions'), '{"model":"gpt-4o-mini","messages":[]}', {'content-type' => 'application/json'})
      abort 'missing wire span' unless memory.finished_spans.size == 1
      print response.body
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil})
    assert status.success?, stderr; assert_equal CHAT, stdout
    refute_includes stderr, 'application destinations require'
  end
  def test_hide_settings_in_the_config_file_reach_exported_spans
    file = File.join(ROOT, '.bundle/tmp', "hide-flags-#{Process.pid}.toml")
    File.write(file, "hide_inputs = true\nhide_outputs = true\n")
    stdout, stderr, status = app(plain_code, {'FI_CONFIG_FILE' => file})
    assert status.success?, stderr; assert_equal CHAT, stdout
    a = attrs(export_spans.first.first.attributes)
    assert_equal '__REDACTED__', a['input.value']
    assert_equal '__REDACTED__', a['output.value']
    refute_includes JSON.generate(a), 'Hello from Ruby'
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_missing_credentials_no_patch_one_warning
    stdout, stderr, status = app(plain_code + "\nputs Net::HTTP.ancestors.any? { |m| m.name == 'TraceAI::Auto::NetHTTP' }", {'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil})
    assert status.success?, stderr; assert_equal CHAT + "false\n", stdout
    assert_equal 1, stderr.scan('traceai-auto:').length; assert_empty export_spans
  end
  def test_disabled_wire_and_sdk
    [{'FI_WIRE_CAPTURE' => 'false'}, {'FI_DISABLED_INSTRUMENTATIONS' => 'wire'}, {'OTEL_SDK_DISABLED' => 'true'}].each do |settings|
      stdout, stderr, status = app(plain_code, settings)
      assert status.success?, stderr; assert_equal CHAT, stdout; assert_empty export_spans
    end
  end
  def test_parent_dedup_suppression_and_context
    prefix = app_sdk + <<~RUBY
      t = OpenTelemetry.tracer_provider.tracer('application')
      def invoke; #{plain_code}; end
      t.in_span('framework', attributes: {'gen_ai.span.kind' => 'CHAIN'}) do
        TraceAI::Auto.with_attributes('session.id' => 's123') { invoke }
      end
      t.in_span('dedicated', attributes: {'gen_ai.span.kind' => 'LLM'}) { invoke }
      OpenTelemetry::Common::Utilities.untraced { invoke }
      t.in_span('application-only') {}
    RUBY
    _, stderr, status = app(prefix)
    assert status.success?, stderr
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal 1, wire.length
    parent = spans.find { |s| s.name == 'framework' }; assert_equal parent.span_id, wire.first.parent_span_id
    # The session id set through the agent's API is exported as set.
    assert_equal 's123', attrs(wire.first.attributes)['session.id']
    # Only AI spans reach Future AGI: the wire span and the two tagged spans.
    assert_equal %w[chat\ gpt-4o-mini dedicated framework], spans.map(&:name).sort
  end
  # Applications often send their session id to the provider as well, under a
  # credential-like body key. The session id set through the agent is still
  # exported as set; the body copy keeps the name rule.
  def test_session_id_repeated_in_the_request_body_is_still_exported_as_set
    code = <<~RUBY
      require 'net/http'; require 'json'
      uri = URI('#{@provider.url}/v1/chat/completions')
      req = Net::HTTP::Post.new(uri); req['Content-Type'] = 'application/json'
      req.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}], metadata: {session_id: 'checkout-session-42'})
      TraceAI::Auto.using_session('checkout-session-42') do
        STDOUT.write(Net::HTTP.start(uri.host, uri.port) { |http| http.request(req).body })
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    a = assert_chat_span(export_spans)
    assert_equal 'checkout-session-42', a['session.id']
    refute_includes a['input.value'], 'checkout-session-42'
  end
  def test_coexists_with_existing_provider
    # Preload manual agent API only; app sets its provider before one-line init.
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'
      p = OpenTelemetry::SDK::Trace::TracerProvider.new
      e = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      p.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(e))
      OpenTelemetry.tracer_provider = p; TraceAI::Auto.init
      #{plain_code}
      abort 'provider replaced' unless OpenTelemetry.tracer_provider.equal?(p)
      abort 'existing exporter lost span' unless e.finished_spans.length == 1
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr; assert_equal CHAT, stdout; assert_chat_span(export_spans)
  end
  def test_early_close_partial_ok
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = plain_code(stream: true).sub('r.read_body { |s| STDOUT.write(s) }', 'catch(:stop) { r.read_body { |s| STDOUT.write(s); throw :stop } }; throw :outer').sub('Net::HTTP.start(uri.host', 'catch(:outer) { Net::HTTP.start(uri.host') + ' }'
    stdout, stderr, status = app(code)
    assert status.success?, stderr; refute_empty stdout
    s = export_spans.map(&:first).first; refute_nil s
    assert_equal :STATUS_CODE_OK, s.status.code; assert_equal true, attrs(s.attributes)['traceai.wire.incomplete']
  end
  def test_privacy_hide_flags
    stdout, stderr, status = app(plain_code, {'FI_HIDE_INPUTS' => 'true', 'FI_HIDE_OUTPUTS' => 'true'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    s = export_spans.map(&:first).first; a = attrs(s.attributes)
    assert_equal '__REDACTED__', a['input.value']; assert_equal '__REDACTED__', a['output.value']
    refute_includes JSON.generate(a), 'Hello from Ruby'
  end
  # A hide switch with a value the agent does not recognise hides that content
  # and says so once per process, by name and never by value.
  def test_unrecognised_hide_switch_hides_content_and_warns_once
    stdout, stderr, status = app(plain_code, {'FI_HIDE_INPUTS' => 'maybe'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    s = export_spans.map(&:first).first; a = attrs(s.attributes)
    assert_equal '__REDACTED__', a['input.value']; assert_equal 'Hello from Ruby', a['output.value']
    assert_equal 1, stderr.scan('FI_HIDE_INPUTS').length, stderr
    refute_includes stderr, 'maybe'
  end
  def test_sigterm_flush_preserves_signal_exit
    code = "STDOUT.sync = true\n" + plain_code + "\nputs 'READY'; loop { sleep 1 }"
    stdin, stdout, stderr, waiter = Open3.popen3(child_env, RbConfig.ruby, '-e', code, chdir: ROOT)
    begin
      Timeout.timeout(10) { loop { break if stdout.gets&.include?('READY') } }
      Process.kill('TERM', waiter.pid)
      status = Timeout.timeout(8) { waiter.value }
      assert status.signaled?, stderr.read; assert_equal Signal.list['TERM'], status.termsig
      assert_chat_span(export_spans)
    ensure
      Process.kill('KILL', waiter.pid) if waiter.alive?
      [stdin, stdout, stderr].each(&:close)
    end
  end
  def test_sigterm_chains_existing_handler
    code = <<~RUBY
      STDOUT.sync = true
      Signal.trap('TERM') { puts 'PREVIOUS'; exit 17 }
      require 'traceai/auto'
      #{plain_code}
      puts 'READY'; loop { sleep 1 }
    RUBY
    stdin, stdout, stderr, waiter = Open3.popen3(child_env('RUBYOPT' => nil), RbConfig.ruby, '-e', code, chdir: ROOT)
    begin
      Timeout.timeout(10) { loop { break if stdout.gets&.include?('READY') } }
      Process.kill('TERM', waiter.pid)
      status = Timeout.timeout(8) { waiter.value }
      assert_equal 17, status.exitstatus, stderr.read; assert_includes stdout.read, 'PREVIOUS'; assert_chat_span(export_spans)
    ensure
      Process.kill('KILL', waiter.pid) if waiter.alive?
      [stdin, stdout, stderr].each(&:close)
    end
  end
end
