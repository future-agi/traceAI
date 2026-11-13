# frozen_string_literal: true
require_relative 'test_round4'
require_relative '../lib/traceai/auto/export'
require_relative '../lib/traceai/auto/agent'

class EndToEndTest
  def standalone_env
    ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
  end

  def test_r4_https_export_uses_loaded_tls_with_certificate_and_hostname_verification
    require 'openssl'
    key = OpenSSL::PKey::RSA.new(2048)
    certificate = OpenSSL::X509::Certificate.new
    certificate.version = 2; certificate.serial = 1
    certificate.subject = certificate.issuer = OpenSSL::X509::Name.parse('/CN=localhost')
    certificate.public_key = key.public_key
    certificate.not_before = Time.now - 60; certificate.not_after = Time.now + 3600
    factory = OpenSSL::X509::ExtensionFactory.new
    factory.subject_certificate = factory.issuer_certificate = certificate
    certificate.add_extension(factory.create_extension('basicConstraints', 'CA:TRUE', true))
    certificate.add_extension(factory.create_extension('subjectAltName', 'DNS:localhost,IP:127.0.0.1'))
    certificate.sign(key, OpenSSL::Digest.new('SHA256'))
    file = Tempfile.new('r4-ca', File.join(ROOT, '.bundle/tmp'))
    file.write(certificate.to_pem); file.close
    context = OpenSSL::SSL::SSLContext.new
    context.cert = certificate; context.key = key
    tcp = TCPServer.new('127.0.0.1', 0)
    port = tcp.addr[1]; requests = Queue.new
    server = Thread.new do
      socket = tcp.accept
      ssl = OpenSSL::SSL::SSLSocket.new(socket, context); ssl.sync_close = true; ssl.accept
      line = ssl.gets
      headers = {}
      while (header = ssl.gets) && header != "\r\n"
        name, value = header.split(':', 2); headers[name.downcase] = value.strip
      end
      body = ssl.read(headers.fetch('content-length').to_i)
      requests << {line: line, headers: headers, body: body}
      ssl.write "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    ensure
      ssl&.close; socket&.close unless socket&.closed?
    end
    server.report_on_exception = false
    stdout, stderr, status = app("require 'openssl'; " + plain_code, {'FI_BASE_URL' => "https://127.0.0.1:#{port}", 'SSL_CERT_FILE' => file.path})
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert server.join(2), 'TLS export did not finish'
    request = requests.pop(true)
    assert_equal 'fixture-api-12345678', request[:headers]['x-api-key']
    decoded = Opentelemetry::Proto::Collector::Trace::V1::ExportTraceServiceRequest.decode(request[:body])
    assert_equal 'chat gpt-4o-mini', decoded.resource_spans.first.scope_spans.first.spans.first.name
  ensure
    tcp&.close; server&.kill; server&.join; file&.unlink
  end

  def test_r4_health_and_ai_requests_never_activate_gems_or_preselect_json
    code = <<~RUBY
      require 'net/http'
      before = Gem.loaded_specs.transform_values(&:version)
      Net::HTTP.get(URI('#{@provider.url}/health'))
      request = Net::HTTP::Post.new('/v1/chat/completions')
      request['content-type'] = 'application/json'; request.body = '{"model":"gpt-4o-mini","messages":[]}'
      print Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(request).body }
      TraceAI::Auto.flush if defined?(TraceAI::Auto)
      abort 'agent activated gems' unless Gem.loaded_specs.transform_values(&:version) == before
      abort 'agent loaded JSON' if defined?(JSON)
      gem 'json', '2.6.3'; require 'json'
      puts 'APPLICATION_RAN ' + JSON::VERSION
    RUBY
    [false, true].each do |injected|
      stdout, stderr, status = app(code, standalone_env.merge(injected ? {} : {'RUBYOPT' => nil}), chdir: File.dirname(ROOT))
      assert status.success?, stderr
      assert_equal CHAT + "APPLICATION_RAN 2.6.3\n", stdout
      spans = export_spans
      injected ? assert_chat_span(spans) : assert_empty(spans)
    end
  end

  def test_r4_httpx_only_standalone_export_never_loads_net_http_or_selects_gems
    code = <<~RUBY
      require 'httpx'
      before = Gem.loaded_specs.transform_values(&:version)
      response = HTTPX.post('#{@provider.url}/v1/chat/completions', headers: {'content-type' => 'application/json'}, body: '{"model":"gpt-4o-mini","messages":[]}')
      print response.to_s
      TraceAI::Auto.flush if defined?(TraceAI::Auto)
      abort 'export selected gems' unless Gem.loaded_specs.transform_values(&:version) == before
      abort 'export required net/http' if $LOADED_FEATURES.any? { |path| path.end_with?('/net/http.rb') }
    RUBY
    [false, true].each do |injected|
      stdout, stderr, status = app(code, standalone_env.merge(injected ? {} : {'RUBYOPT' => nil}), chdir: File.dirname(ROOT))
      assert status.success?, stderr; assert_equal CHAT, stdout
      spans = export_spans
      injected ? assert_chat_span(spans) : assert_empty(spans)
    end
  end

  def test_r4_require_observation_never_triggers_zeitwerk_namespace_autoloads
    Dir.mktmpdir('r4-zeitwerk', File.join(ROOT, '.bundle/tmp')) do |directory|
      File.write(File.join(directory, 'httpx.rb'), "module HTTPX; BOOT_HOST = ENV.fetch('BOOT_HOST', 'unconfigured'); end")
      File.write(File.join(directory, 'open_ai.rb'), "module OpenAI; BOOT_HOST = ENV.fetch('BOOT_HOST', 'unconfigured'); end")
      code = <<~RUBY
        require 'zeitwerk'
        loader = Zeitwerk::Loader.new; loader.inflector.inflect('open_ai' => 'OpenAI', 'httpx' => 'HTTPX')
        loader.push_dir(#{directory.inspect}); loader.setup
        require 'set'
        abort 'HTTPX autoload triggered' unless Object.autoload?(:HTTPX)
        abort 'OpenAI autoload triggered' unless Object.autoload?(:OpenAI)
        ENV['BOOT_HOST'] = 'configured-host'
        puts HTTPX::BOOT_HOST; puts OpenAI::BOOT_HOST
      RUBY
      [false, true].each do |injected|
        stdout, stderr, status = app(code, standalone_env.merge(injected ? {} : {'RUBYOPT' => nil}), chdir: File.dirname(ROOT))
        assert status.success?, stderr
        assert_equal "configured-host\nconfigured-host\n", stdout
      end
    end
    assert_empty export_spans
  end

  def test_r4_init_is_once_with_concurrent_callers_and_immutable_export_settings
    code = <<~RUBY
      original = [TraceAI::Auto.provider, TraceAI::Auto.processor, TraceAI::Auto.exporter, TraceAI::Auto.config]
      results = 12.times.map { Thread.new { TraceAI::Auto.init(project_name: 'CHANGED', sample_rate: 0, api_key: 'CHANGED') } }.map(&:value)
      abort 'init result changed' unless results.all? { |value| value.equal?(original.first) }
      abort 'init rewired state' unless original == [TraceAI::Auto.provider, TraceAI::Auto.processor, TraceAI::Auto.exporter, TraceAI::Auto.config]
      #{plain_code}
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert_equal 1, stderr.scan('already initialized').length
    spans = export_spans; assert_chat_span(spans)
    assert_equal 'ruby-e2e', spans.first.last['project_name']
  end

  def test_r4_auto_instrument_is_additive_and_never_disables_or_rewires
    code = <<~RUBY
      require 'httpx'
      provider = TraceAI::Auto.provider; processor = TraceAI::Auto.processor
      abort 'wire enabled too soon' if TraceAI::Auto.wire_enabled?
      TraceAI::Auto.auto_instrument(instrumentations: ['wire'])
      TraceAI::Auto.auto_instrument(instrumentations: [])
      TraceAI::Auto.init(wire_capture: false)
      abort 'provider rewired' unless TraceAI::Auto.provider.equal?(provider) && TraceAI::Auto.processor.equal?(processor)
      response = HTTPX.post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: []})
      print response.to_s
    RUBY
    stdout, stderr, status = app(code, {'FI_WIRE_CAPTURE' => 'false'})
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert_chat_span(export_spans)
  end

  def test_r4_real_anthropic_native_span_cannot_defeat_covered_ancestry
    @provider.stop
    body = JSON.generate(id: 'msg-1', type: 'message', role: 'assistant', model: 'claude-test', content: [{type: 'text', text: 'answer'}], stop_reason: 'end_turn', usage: {input_tokens: 3, output_tokens: 2})
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, body] }
    code = <<~RUBY
      require 'anthropic'
      client = Anthropic::Client.new(api_key: 'mock-key', base_url: '#{@provider.url}', max_retries: 0)
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      def invoke(client)
        response = client.messages.create(model: 'claude-test', max_tokens: 16, messages: [{role: 'user', content: 'hi'}])
        abort 'response changed' unless response.content.first.text == 'answer'
      end
      %w[LLM EMBEDDING RERANKER GUARDRAIL].each do |kind|
        tracer.in_span('library-' + kind, attributes: {'gen_ai.span.kind' => kind}) do
          tracer.in_span('intermediate') { invoke(client) }
        end
      end
      %w[CHAIN AGENT TOOL].each do |kind|
        tracer.in_span('framework-' + kind, attributes: {'gen_ai.span.kind' => kind}) { invoke(client) }
      end
      OpenTelemetry::Common::Utilities.untraced { invoke(client) }
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
    assert_equal 8, @provider.requests.length
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal 3, wire.length
    native = spans.select { |s| s.name == 'anthropic.messages.create' }
    assert_equal 7, native.length
    wire.each { |s| assert native.any? { |n| n.span_id == s.parent_span_id } }
  end

  def test_r4_httpx_unconsumed_stream_is_untouched_and_response_identity_survives
    code = <<~RUBY
      require 'httpx'
      response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
      marker = Object.new
      response.define_singleton_method(:application_helper) { marker }
      abort 'helper lost' unless response.application_helper.equal?(marker)
      abort 'agent consumed stream' if response.instance_variable_get(:@response_enum)
      puts 'UNCONSUMED'
    RUBY
    [false, true].each do |injected|
      stdout, stderr, status = app(code, injected ? {} : {'RUBYOPT' => nil})
      assert status.success?, stderr; assert_equal "UNCONSUMED\n", stdout
      assert_empty export_spans
      assert_equal 0, @provider.requests.length
    end
  end

  def test_r4_native_response_identity_helpers_and_cached_bodies_match_baseline
    code = <<~RUBY
      require 'net/http'; require 'httpx'
      module ObserveNativeIdentity
        def transport_request(...)
          result = super
          result.define_singleton_method(:application_helper) { 'native-helper' }
          Thread.current[:native_response] = result
          result
        end
        private :transport_request
      end
      Net::HTTP.prepend(ObserveNativeIdentity)
      request = Net::HTTP::Post.new('/v1/chat/completions')
      request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[]}'
      response = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(request) }
      abort 'Net::HTTP identity changed' unless response.equal?(Thread.current[:native_response])
      abort 'Net::HTTP helper lost' unless response.application_helper == 'native-helper'
      abort 'Net::HTTP cached body changed' unless response.body.equal?(response.read_body)
      print response.body
      session = HTTPX.with({})
      request = session.build_request('POST', '#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []})
      native = nil
      request.on(:response) { |result| native = result; result.define_singleton_method(:application_helper) { 'native-helper' } }
      response = session.request(request)
      abort 'HTTPX identity changed' unless response.equal?(native)
      abort 'HTTPX helper lost' unless response.application_helper == 'native-helper'
      print response.to_s
    RUBY
    [false, true].each do |injected|
      stdout, stderr, status = app(code, injected ? {} : {'RUBYOPT' => nil})
      assert status.success?, stderr; assert_equal CHAT * 2, stdout
      spans = export_spans
      assert_equal(injected ? 2 : 0, spans.length)
    end
  end

  def test_r4_transport_timestamps_exclude_consumer_and_parser_time
    %w[net_http httpx].each do |transport|
      code = <<~RUBY
        require 'net/http'; require 'httpx'
        module SlowParser
          def match(...)
            Thread.current[:first_match_at] ||= (Time.now.to_r * 1_000_000_000).to_i
            sleep 0.10
            super
          end
          def build(*args, **kwargs)
            sleep 0.20
            super
          end
        end
        TraceAI::Auto::Wire.singleton_class.prepend(SlowParser)
        before = Time.now.to_r * 1_000_000_000
        if #{transport.inspect} == 'net_http'
          request = Net::HTTP::Post.new('/v1/chat/completions')
          request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[]}'
          Net::HTTP.start('127.0.0.1', #{@provider.port}) do |client|
            client.request(request) { |response| response.read_body { |chunk| print chunk; sleep 0.20 } }
          end
        else
          response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
          response.each { |chunk| print chunk; sleep 0.20 }
        end
        after = Time.now.to_r * 1_000_000_000
        puts "TIMES " + before.to_i.to_s + ' ' + after.to_i.to_s + ' ' + Thread.current[:first_match_at].to_s
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert stdout.start_with?(CHAT)
      before, after, first_match = stdout.split('TIMES ').last.split.map(&:to_i)
      spans = export_spans; assert_equal 1, spans.length
      span = spans.first.first
      assert_operator span.start_time_unix_nano, :>=, before
      assert_operator span.start_time_unix_nano, :<=, first_match, transport
      assert_operator after - span.end_time_unix_nano, :>=, 350_000_000, transport
      assert_operator span.end_time_unix_nano, :>=, span.start_time_unix_nano
    end
  end
end

class Round4BoundaryTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  C = TraceAI::Auto::Codec
  A = TraceAI::Auto
  def record
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    span = provider.tracer('scope', 'version').start_span('span')
    span.finish
    span.to_span_data
  end
  def snapshot(value, flags = {})
    P.snapshot(value, A::ExportResource.new({}), A::Config.new(flags, env: {}), ['SECRET_VALUE_12345678'])
  end
  def test_r4_all_export_surfaces_are_sanitized_without_mutation_or_unknown_getters
    secret = 'SECRET_VALUE_12345678'
    marker = "#{secret} https://host/path?signature=PRIVATE_SIGNATURE data:image/png;base64,QUJD"
    value = record
    hostile = Object.new
    def hostile.to_s = raise('user to_s called')
    def hostile.to_json(*) = raise('user to_json called')
    def hostile.class = raise('user class called')
    value.name = marker
    value.attributes = {'ordinary' => marker, 'array' => [Float::NAN, Float::INFINITY, -Float::INFINITY, marker, hostile], 'nested' => {'api_key' => 'CREDENTIAL'}, 'output.extra' => 'PRIVATE_OUTPUT'}
    value.status = OpenTelemetry::Trace::Status.error(marker)
    value.events = [OpenTelemetry::SDK::Trace::Event.new(marker, {'output.extra' => 'PRIVATE_EVENT', 'ordinary' => marker}, 123)]
    state = OpenTelemetry::Trace::Tracestate.from_string('vendor=' + secret)
    value.tracestate = state
    link_context = OpenTelemetry::Trace::SpanContext.new(tracestate: state)
    value.links = [OpenTelemetry::Trace::Link.new(link_context, {'ordinary' => marker, 'output.extra' => 'PRIVATE_LINK'})]
    value.resource = OpenTelemetry::SDK::Resources::Resource.create('ordinary' => marker, 'output.extra' => 'PRIVATE_RESOURCE')
    value.instrumentation_scope = A::ExportScope.new(marker, marker, {'ordinary' => marker, 'output.extra' => 'PRIVATE_SCOPE'})
    copy = snapshot(value, hide_outputs: true)
    encoded = A::Protobuf.encode([copy])
    [secret, 'PRIVATE_SIGNATURE', 'PRIVATE_OUTPUT', 'PRIVATE_EVENT', 'PRIVATE_LINK', 'PRIVATE_RESOURCE', 'PRIVATE_SCOPE', 'CREDENTIAL', 'QUJD'].each { |s| refute_includes encoded, s }
    assert_equal ['NaN', 'Infinity', '-Infinity'], copy.attributes['array'].first(3)
    assert_equal 'Object', copy.attributes['array'].last
    assert_equal '__REDACTED__', copy.attributes['output.extra']
    assert_equal marker, value.name
    assert_equal marker, value.events.first.name
    assert_same hostile, value.attributes['array'].last
    assert_equal state, value.tracestate
    assert_equal 'vendor=<redacted>', copy.tracestate
    assert_equal 'vendor=<redacted>', copy.links.first.span_context.tracestate
    assert_equal value.start_timestamp, copy.start_timestamp
    assert_equal value.span_id, copy.span_id
  end

  def test_r4_sanitizer_cycles_and_branching_fail_closed_per_span_not_batch
    cycle = []; cycle << cycle
    [cycle, Array.new(20_000, 'content')].each do |bad|
      original = record; original.attributes = {'input.value' => 'PRIVATE', 'cycle' => bad}
      copy = snapshot(original)
      assert_equal({}, copy.attributes)
      assert_equal original.name, copy.name
      assert_equal '', copy.tracestate
      assert_equal original.span_id, copy.span_id
      assert_equal original.start_timestamp, copy.start_timestamp
      good = snapshot(record)
      encoded = A::Protobuf.encode([copy, good])
      refute_empty encoded
      refute_includes encoded, 'PRIVATE'
    end
  end

  def test_r4_hide_flags_cover_flattened_parameters_and_attribute_families
    original = record
    original.attributes = {'gen_ai.request.model' => 'model', 'gen_ai.request.stream' => true, 'gen_ai.request.temperature' => 0.731,
                           'gen_ai.request.top_p' => 0.819, 'gen_ai.request.max_tokens' => 1237, 'gen_ai.request.parameters.extra' => 'PARAM',
                           'input.extra' => 'INPUT', 'gen_ai.input.extra' => 'INPUT', 'output.extra' => 'OUTPUT', 'gen_ai.output.extra' => 'OUTPUT',
                           'gen_ai.tool.extra' => 'TOOL', 'gen_ai.system_instructions' => 'SYSTEM', 'file.uri' => 'PRIVATE_FILE', 'image.url' => 'PRIVATE_IMAGE'}
    copy = snapshot(original, hide_llm_invocation_parameters: true, hide_inputs: true, hide_outputs: true, hide_input_images: true)
    %w[gen_ai.request.temperature gen_ai.request.top_p gen_ai.request.max_tokens gen_ai.request.parameters.extra file.uri image.url].each { |key| refute copy.attributes.key?(key), key }
    %w[input.extra gen_ai.input.extra output.extra gen_ai.output.extra gen_ai.tool.extra].each { |key| assert_equal '__REDACTED__', copy.attributes[key], key }
    refute copy.attributes.key?('gen_ai.system_instructions')
    assert_equal 'model', copy.attributes['gen_ai.request.model']
    assert_equal true, copy.attributes['gen_ai.request.stream']
  end

  def test_r4_packaged_preload_cannot_activate_dependencies_before_the_shim_runs
    spec = Gem::Specification.load(File.join(ROOT, 'traceai-auto.gemspec'))
    assert_empty spec.runtime_dependencies
  end

  def test_r4_configuration_is_an_immutable_snapshot_not_an_alias_of_application_options
    name = +'original'; integration = +'wire'; list = [integration]
    config = A::Config.new({project_name: name, instrumentations: list}, env: {})
    name.replace('changed'); integration.replace('changed'); list.clear
    assert_equal 'original', config['project_name']
    assert_equal ['wire'], config.list('instrumentations')
    assert_raises(FrozenError) { config.values['project_name'] = 'changed' }
    assert_raises(FrozenError) { config['project_name'].replace('changed') }
    assert_raises(FrozenError) { config['instrumentations'].clear }
  end

  def test_r4_internal_codec_matches_json_without_activating_or_calling_hooks
    samples = [nil, true, false, 0, -12, 0.123, 1e30, "café世界\n\"\\", {'array' => [1, nil, false, 'é'], 'emoji' => "\u{1f680}"}]
    samples.each do |sample|
      if sample.nil?
        assert_nil C.parse(C.generate(sample))
        assert_nil C.parse(JSON.generate(sample))
      else
        assert_equal JSON.parse(JSON.generate(sample)), C.parse(C.generate(sample))
        assert_equal JSON.parse(JSON.generate(sample)), C.parse(JSON.generate(sample))
      end
    end
    assert_equal "\u{1f680}", C.parse('"\\ud83d\\ude80"')
    ['', '{', 'NaN', 'true false', '[1,]', '"\\ud800"', '"\\x"', '1e999'].each { |bad| assert_raises(C::ParserError) { C.parse(bad) } }
  end
end
