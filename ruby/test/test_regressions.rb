# frozen_string_literal: true
require_relative 'test_e2e'
require 'tempfile'

# Reuse the real process/collector harness; these are transport regressions, not
# mocks of the hooks themselves.
class EndToEndTest
  def test_unsupported_ruby_injection_fails_open_before_loading_agent
    skip 'System Ruby unavailable on this platform' unless File.executable?('/usr/bin/ruby')
    code = 'puts "APPLICATION_RAN"; abort "Ruby-3 agent loaded" if $LOADED_FEATURES.any? { |f| f.end_with?("traceai/auto/agent.rb") }'
    stdout, stderr, status = Timeout.timeout(15) do
      Open3.capture3(child_env, '/usr/bin/ruby', '-I' + File.join(ROOT, 'lib'), '-rtraceai/auto', '-e', code, chdir: ROOT)
    end
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    assert_equal 1, stderr.scan('traceai-auto:').length
    assert_includes stderr, 'Ruby >= 3.1 required'
    assert_empty export_spans
  end

  def test_disabled_preload_does_not_activate_json_before_bundler_setup
    default = Dir[File.join(Gem.default_specifications_dir, 'json-*.gemspec')].first
    refute_nil default, 'A default JSON version is needed to exercise the activation conflict'
    version = File.basename(default, '.gemspec').delete_prefix('json-')
    gemfile = Tempfile.new(['pinned-json', '.Gemfile'], File.join(ROOT, '.bundle/tmp'))
    gemfile.write("source 'https://rubygems.org'\ngem 'json', '#{version}'\n"); gemfile.close
    config = Tempfile.new(['preload-config', '.toml'], File.join(ROOT, '.bundle/tmp'))
    config.write("project_name = \"before Bundler # preserved\"\ninstrumentations = [\"wire\"]\nsample_rate = 0.25\n"); config.close
    # Do not inherit bundle-exec's auto-setup machinery: the application must
    # perform its own Bundler.setup AFTER the injected agent for this regression.
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('FI_API_KEY' => '', 'FI_SECRET_KEY' => '', 'BUNDLE_GEMFILE' => gemfile.path)
    stdout, stderr, status = app("require 'bundler/setup'; require 'json'; puts JSON::VERSION", overrides.merge('RUBYOPT' => nil))
    assert status.success?, stderr
    assert_equal version + "\n", stdout
    [File.join(ROOT, '.bundle/nonexistent.toml'), config.path].each do |path|
      code = <<~RUBY
        abort 'JSON activated before Bundler' if defined?(JSON) || Gem.loaded_specs.key?('json')
        abort 'Transport loaded while disabled' if $LOADED_FEATURES.any? { |f| f.end_with?('traceai/auto/wire.rb') }
        require 'bundler/setup'; require 'json'; puts JSON::VERSION
      RUBY
      stdout, stderr, status = app(code, overrides.merge('FI_CONFIG_FILE' => path))
      assert status.success?, stderr
      assert_equal version + "\n", stdout
      assert_equal 1, stderr.scan('traceai-auto:').length
      assert_includes stderr, 'missing FI_API_KEY/FI_SECRET_KEY'
    end
    assert_empty export_spans
  ensure
    File.unlink(gemfile.path + '.lock') if gemfile && File.exist?(gemfile.path + '.lock')
    gemfile&.unlink; config&.unlink
  end

  def test_native_file_multipart_tee_covers_known_hosts_and_gateways
    body = "--native\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--native\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + ('x' * 5_242_880) + "\r\n--native--\r\n"
    file = Tempfile.new('native-multipart', File.join(ROOT, '.bundle/tmp'))
    file.binmode; file.write('PREFIX' + body); file.close
    ['api.openai.com', '127.0.0.1'].each do |host|
      code = <<~RUBY
        require 'net/http'
        File.open(#{file.path.inspect}, 'rb') do |source|
          source.seek(6)
          req = Net::HTTP::Post.new('/v1/audio/transcriptions')
          req['Content-Type'] = 'multipart/form-data; boundary=native'
          req.content_length = #{body.bytesize}
          req.body_stream = source
          http = Net::HTTP.new(#{host.inspect}, #{@provider.port}, nil)
          http.ipaddr = '127.0.0.1'
          abort 'read ahead' unless source.pos == 6
          http.start do |h|
            puts h.request(req) { abort 'body_stream identity changed' unless req.body_stream.equal?(source) }.code
          end
          abort 'stream identity changed' unless req.body_stream.equal?(source)
          abort 'upload reader retained' if req.instance_variable_defined?(:@traceai_upload_reader)
          abort 'stream not consumed normally' unless source.pos == source.size
        end
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_equal "200\n", stdout
      received = @provider.requests.pop(true)
      assert_equal body, received[:body]
      assert_equal 'multipart/form-data; boundary=native', received[:headers]['content-type']
      assert_equal body.bytesize.to_s, received[:headers]['content-length']
      spans = export_spans
      assert_equal 1, spans.length, host
      a = attrs(spans.first.first.attributes)
      assert_equal 'whisper-1', a['gen_ai.request.model']
      assert_equal 5_242_880, a['traceai.audio.input_bytes']
      assert_equal 'audio/wav', a['gen_ai.audio.mime_type']
      refute a['traceai.wire.body_unavailable']; refute a['traceai.wire.truncated']
      refute_includes JSON.generate(a), 'xxxxxxxxxxxxxxxxxxxxxxxx'
    end
  ensure
    file&.unlink
  end

  def test_net_http_protocol_failure_preserves_partial_output_and_exception
    @provider.raw do |io, _|
      io.write "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
      chunk = STREAM.first
      io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\nZZZ\r\n"; io.flush
    end
    code = "begin\n#{plain_code(stream: true)}\nrescue Net::HTTPBadResponse => e; puts e.class.name; end"
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal STREAM.first + "Net::HTTPBadResponse\n", stdout
    spans = export_spans; assert_equal 1, spans.length
    span = spans.first.first; a = attrs(span.attributes)
    assert_equal :STATUS_CODE_ERROR, span.status.code
    assert_equal 'Net::HTTPBadResponse', a['error.type']
    assert_equal 'one', a['output.value']
    refute a['traceai.wire.incomplete']
  end

  def test_httpx_retry_clears_previous_json_binary_and_truncated_responses
    failures = [
      ['application/json', JSON.generate(id: 'failed-id', model: 'failed-model', error: {message: 'retry me'})],
      ['application/octet-stream', 'BINARY-FAILED-ATTEMPT'],
      ['application/json', 'x' * 4096]
    ]
    failures.each do |type, body|
      @provider.stop
      attempts = 0
      @provider = MockHTTP.new do |_r|
        attempts += 1
        attempts == 1 ? [503, {'Content-Type' => type}, body] : [200, {'Content-Type' => 'application/json'}, CHAT]
      end
      code = <<~RUBY
        require 'httpx'
        session = HTTPX.plugin(:retries).with(max_retries: 1, retry_change_requests: true, retry_on: ->(r) { r.status == 503 })
        response = session.post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}]})
        abort 'wrong final status' unless response.status == 200
        print response.to_s
      RUBY
      stdout, stderr, status = app(code, {'FI_WIRE_MAX_BODY_BYTES' => '512'})
      assert status.success?, stderr
      assert_equal CHAT, stdout
      assert_equal 2, attempts
      a = assert_chat_span(export_spans)
      assert_equal 'Hello from Ruby', a['output.value']
      assert_equal 'gpt-4o-mini', a['gen_ai.response.model']
      assert_equal 'chat-1', a['gen_ai.response.id']
      assert_equal 200, a['http.response.status_code']
      refute a['traceai.wire.truncated']; refute a['traceai.wire.incomplete']; refute a['error.type']
      refute_includes JSON.generate(a), 'FAILED-ATTEMPT'
      refute_includes JSON.generate(a), 'failed-id'
    end
  end

  def test_httpx_retry_clears_midstream_error_but_no_retry_keeps_partial_output
    [0, 1].each do |retries|
      attempts = 0
      @provider.raw do |io, _|
        attempts += 1
        if attempts == 1
          io.write "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
          chunk = STREAM.first
          io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\nZZZ\r\n"; io.flush
        else
          io.write "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: #{CHAT.bytesize}\r\nConnection: close\r\n\r\n#{CHAT}"
        end
      end
      code = <<~RUBY
        require 'httpx'
        r = HTTPX.plugin(:retries).with(max_retries: #{retries}, retry_change_requests: true, retry_on: ->(response) { response.is_a?(HTTPX::ErrorResponse) }).post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: []})
        print(r.is_a?(HTTPX::ErrorResponse) ? r.error.class.name : r.to_s)
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_equal retries + 1, attempts
      spans = export_spans; assert_equal 1, spans.length
      span = spans.first.first; a = attrs(span.attributes)
      if retries.zero?
        assert_match(/HTTPX::.*Error/, stdout)
        assert_equal :STATUS_CODE_ERROR, span.status.code
        assert_equal stdout, a['error.type']
        assert_equal 'one', a['output.value']
      else
        assert_equal CHAT, stdout
        assert_equal :STATUS_CODE_OK, span.status.code
        assert_equal 'Hello from Ruby', a['output.value']
        assert_equal 'chat-1', a['gen_ai.response.id']
        refute a['error.type']; refute a['traceai.wire.incomplete']
      end
    end
  end

  def test_net_http_consumer_exception_is_not_a_transport_failure
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = plain_code(stream: true).sub('STDOUT.write(s)', "STDOUT.write(s); raise ArgumentError, 'consumer failure'")
    stdout, stderr, status = app("begin\n#{code}\nrescue ArgumentError => e; puts e.message; end")
    assert status.success?, stderr
    assert_includes stdout, 'consumer failure'
    spans = export_spans; assert_equal 1, spans.length
    span = spans.first.first; a = attrs(span.attributes)
    assert_equal :STATUS_CODE_OK, span.status.code
    assert_equal true, a['traceai.wire.incomplete']
    refute a['error.type']
  end

  def test_doctor_exports_with_zero_sampling_without_enabling_application_spans
    code = plain_code + "\nputs JSON.generate(TraceAI::Auto.report(test_export: true))"
    stdout, stderr, status = app(code, {'FI_SAMPLE_RATE' => '0'})
    assert status.success?, stderr
    report = JSON.parse(stdout.delete_prefix(CHAT))
    assert_equal 'accepted', report['collector']
    assert_equal 0.0, report.dig('config', 'sample_rate')
    spans = export_spans
    assert_equal ['traceai-auto doctor'], spans.map { |s, _| s.name }
    assert_equal true, attrs(spans.first.first.attributes)['traceai.diagnostic']
    assert_equal 'ruby-e2e', spans.first.last['project_name']
  end

  def test_doctor_uses_own_processor_not_existing_provider_sampling_or_exporters
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'json'
      sampler = OpenTelemetry::SDK::Trace::Samplers::ALWAYS_OFF
      provider = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: sampler)
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = provider
      require 'traceai/auto'
      provider.tracer('app').in_span('not-sampled') { puts JSON.generate(TraceAI::Auto.report(test_export: true)) }
      abort 'provider/sampler changed' unless OpenTelemetry.tracer_provider.equal?(provider) && provider.sampler.equal?(sampler)
      abort 'diagnostic leaked to other exporters' unless memory.finished_spans.empty?
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_SAMPLE_RATE' => '0'})
    assert status.success?, stderr
    assert_equal 'accepted', JSON.parse(stdout)['collector']
    assert_equal ['traceai-auto doctor'], export_spans.map { |s, _| s.name }
  end
end
