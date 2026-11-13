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
    code = <<~RUBY
      require 'anthropic'
      c = Anthropic::Client.new(api_key: 'mock-anthropic-api', base_url: '#{@provider.url}', max_retries: 0)
      r = c.messages.create(model: 'claude-test', max_tokens: 16, messages: [{role: 'user', content: 'hi'}])
      puts r.content.first.text
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "anthropic local\n", stdout
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal 1, wire.length
    assert_equal 'chat claude-test', wire.first.name
    assert_equal 'anthropic local', attrs(wire.first.attributes)['output.value']
    # Anthropic 2.3 has a vendor-native span without traceAI's dedup marker;
    # preserve it and parent the wire child rather than modifying vendor tracing.
    native = spans.find { |s| s.span_id == wire.first.parent_span_id }; refute_nil native
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
  def test_one_line_configuration_does_not_reconfigure_initialized_preload
    code = "original = TraceAI::Auto.provider; result = TraceAI::Auto.init(project_name: 'argument-project', sample_rate: 1.0); abort 'provider changed' unless result.equal?(original)\n" + plain_code
    _, stderr, status = app(code, {'FI_PROJECT_NAME' => 'environment-project', 'FI_SAMPLE_RATE' => '0'})
    assert status.success?, stderr
    assert_includes stderr, 'already initialized'
    assert_empty export_spans
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
    prefix = <<~RUBY
      require 'opentelemetry/sdk'
      t = OpenTelemetry.tracer_provider.tracer('application')
      def invoke; #{plain_code}; end
      t.in_span('framework', attributes: {'gen_ai.span.kind' => 'CHAIN'}) do
        TraceAI::Auto.with_attributes('session.id' => 's123') { invoke }
      end
      t.in_span('dedicated', attributes: {'gen_ai.span.kind' => 'LLM'}) { invoke }
      OpenTelemetry::Common::Utilities.untraced { invoke }
    RUBY
    _, stderr, status = app(prefix)
    assert status.success?, stderr
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal 1, wire.length
    parent = spans.find { |s| s.name == 'framework' }; assert_equal parent.span_id, wire.first.parent_span_id
    assert_equal '<redacted>', attrs(wire.first.attributes)['session.id'] # S6 session-name rule
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
