# frozen_string_literal: true
require_relative 'test_round4_contract'

class EndToEndTest
  def test_r4_verifier_partial_tool_json_fails_closed_without_changing_bytes_or_eof
    credential = 'TOOL_CREDENTIAL_12345678'
    arguments = '{"api_key":"' + credential + '"'
    event = "data: #{JSON.generate(id: 'partial', model: 'm', choices: [{index: 0, delta: {tool_calls: [{index: 0, id: 'call-1', function: {name: 'tool', arguments: arguments}}]}}])}\n\n"
    @provider.raw do |io, _|
      io.write "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n#{event.bytesize.to_s(16)}\r\n#{event}\r\n"
      io.flush
    end
    code = <<~RUBY
      require 'net/http'
      if defined?(TraceAI::Auto)
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      end
      request = Net::HTTP::Post.new('/v1/chat/completions')
      request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[],"stream":true}'
      begin
        Net::HTTP.start('127.0.0.1', #{@provider.port}) do |client|
          client.max_retries = 0
          client.request(request) { |response| response.read_body { |chunk| print chunk } }
        end
        abort 'missing native EOFError'
      rescue EOFError => e
        abort 'backtrace changed' unless e.backtrace.any? { |line| line.include?('net/protocol') }
        puts 'NATIVE_EOF'
      end
      if memory
        wire = memory.finished_spans.first
        abort 'wire missing' unless wire
        abort 'wire leaked content to other exporter' if wire.attributes.keys.any? { |key| TraceAI::Auto::AttributeProjection.content_group(key, 'chat') && !TraceAI::Auto::AttributeProjection.protected_family(key) }
        abort 'wire leaked credential' if wire.attributes.values.any? { |value| value.to_s.include?(#{credential.inspect}) }
        abort 'safe outcome erased' unless wire.attributes['error.type'] == 'EOFError'
        abort 'arrival erased' unless wire.attributes.key?('gen_ai.response.time_to_first_text')
      end
    RUBY
    [false, true].each do |injected|
      stdout, stderr, status = app(code, injected ? {} : {'RUBYOPT' => nil})
      assert status.success?, stderr
      assert_equal event + "NATIVE_EOF\n", stdout
      spans = export_spans
      unless injected
        assert_empty spans; next
      end
      assert_equal 1, spans.length
      span = spans.first.first
      assert_equal :STATUS_CODE_ERROR, span.status.code
      refute_includes span.to_h.to_s, credential
      attributes = attrs(span.attributes)
      assert_equal 'EOFError', attributes['error.type']
      assert attributes.key?('gen_ai.response.time_to_first_text')
      refute attributes.keys.any? { |key| TraceAI::Auto::AttributeProjection.content_group(key, 'chat') && !TraceAI::Auto::AttributeProjection.protected_family(key) }
      assert_operator span.end_time_unix_nano, :>=, span.start_time_unix_nano
    end
  end

  def test_r4_verifier_native_credentials_are_discovered_with_wire_disabled
    key = 'REQUEST_ONLY_API_KEY_12345678'
    @provider.stop
    payload = JSON.generate(id: 'native-private', type: 'message', role: 'assistant', model: 'claude-test', content: [{type: 'text', text: "echo #{key}"}], stop_reason: 'end_turn', usage: {input_tokens: 2, output_tokens: 1})
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, payload] }
    code = <<~RUBY
      require 'anthropic'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      client = Anthropic::Client.new(api_key: #{key.inspect}, base_url: '#{@provider.url}', max_retries: 0, open_telemetry: {traces: {content_mode: :content}})
      response = client.messages.create(model: 'claude-test', max_tokens: 16, messages: [{role: 'user', content: 'hi'}])
      abort 'response changed' unless response.content.first.text == #{("echo " + key).inspect}
      native = memory.finished_spans.find { |span| span.name == 'anthropic.messages.create' }
      abort 'native missing' unless native
      original = JSON.generate(native.attributes)
      abort 'native capture missing' unless original.include?(#{key.inspect})
      TraceAI::Auto.flush
      abort 'other exporter mutated' unless JSON.generate(native.attributes) == original
      puts 'APPLICATION_RAN'
    RUBY
    [{}, {'FI_WIRE_CAPTURE' => 'false'}, {'FI_DISABLED_INSTRUMENTATIONS' => 'wire'},
     {'FI_DISABLED_INSTRUMENTATIONS' => 'net_http'}, {'FI_INSTRUMENTATIONS' => 'anthropic'}].each do |flags|
      stdout, stderr, status = app(code, flags)
      assert status.success?, "#{flags}: #{stderr}"; assert_equal "APPLICATION_RAN\n", stdout
      assert_equal "Bearer #{key}", @provider.requests.pop(true)[:headers]['authorization']
      spans = export_spans.map(&:first)
      assert_equal(flags.empty? ? 2 : 1, spans.length, flags.inspect)
      native = spans.find { |span| span.name == 'anthropic.messages.create' }; refute_nil native
      refute_includes spans.map(&:to_h).to_s, key, flags.inspect
      assert_includes attrs(native.attributes)['gen_ai.output.messages'], '<redacted>'
    end
  end

  def test_r4_verifier_httpx_credentials_survive_header_first_fiber_with_wire_disabled
    key = 'HTTPX_REQUEST_ONLY_CREDENTIAL_12345678'
    @provider.stop
    payload = JSON.generate(choices: [{message: {role: 'assistant', content: "echo #{key}"}}])
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, payload] }
    code = <<~RUBY
      require 'httpx'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider.tracer('app').in_span('library-call') do |span|
        response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, headers: {'x-api-key' => #{key.inspect}}, stream: true)
        abort 'bad status' unless response.status == 200
        output = +''; response.each { |chunk| output << chunk }
        abort 'body changed' unless output == #{payload.inspect}
        span.set_attribute('output.value', output)
      end
      original = memory.finished_spans.find { |span| span.name == 'library-call' }
      abort 'other exporter mutated' unless original.attributes['output.value'].include?(#{key.inspect})
      TraceAI::Auto.flush
      abort 'export mutated original' unless original.attributes['output.value'].include?(#{key.inspect})
      puts 'APPLICATION_RAN'
    RUBY
    [{'FI_WIRE_CAPTURE' => 'false'}, {'FI_DISABLED_INSTRUMENTATIONS' => 'wire'}, {'FI_DISABLED_INSTRUMENTATIONS' => 'httpx'}].each do |flags|
      stdout, stderr, status = app(code, flags)
      assert status.success?, "#{flags}: #{stderr}"; assert_equal "APPLICATION_RAN\n", stdout
      spans = export_spans.map(&:first); assert_equal 1, spans.length
      refute_includes spans.first.to_h.to_s, key
      assert_includes attrs(spans.first.attributes)['output.value'], '<redacted>'
    end
  end

  def test_r4_verifier_native_invocation_aliases_follow_hiding_without_mutation
    @provider.stop
    payload = JSON.generate(id: 'native-params', type: 'message', role: 'assistant', model: 'claude-test', content: [{type: 'text', text: 'answer'}], stop_reason: 'end_turn', usage: {input_tokens: 2, output_tokens: 1})
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json', 'request-id' => 'safe-request-id'}, payload] }
    code = <<~RUBY
      require 'anthropic'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      client = Anthropic::Client.new(api_key: 'mock-key', base_url: '#{@provider.url}', max_retries: 0)
      response = client.messages.create(model: 'claude-test', max_tokens: 2048, output_config: {effort: 'high'}, thinking: {type: 'enabled', budget_tokens: 1024}, service_tier: 'standard_only', messages: [{role: 'user', content: 'hi'}])
      abort 'response changed' unless response.content.first.text == 'answer'
      native = memory.finished_spans.find { |span| span.name == 'anthropic.messages.create' }
      expected = {'gen_ai.request.reasoning.level' => 'high', 'anthropic.request.service_tier' => 'standard_only', 'anthropic.thinking.type' => 'enabled', 'anthropic.thinking.budget_tokens' => 1024}
      abort 'native alias missing' unless expected.all? { |key, value| native.attributes[key] == value }
      original = native.attributes.dup
      TraceAI::Auto.flush
      abort 'other exporter mutated' unless native.attributes == original
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code, {'FI_HIDE_LLM_INVOCATION_PARAMETERS' => 'true'})
    assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    native = spans.find { |span| span.name == 'anthropic.messages.create' }; refute_nil native
    attributes = attrs(native.attributes)
    assert_equal 'claude-test', attributes['gen_ai.request.model']
    assert_equal 'safe-request-id', attributes['anthropic.request.id']
    %w[gen_ai.request.max_tokens gen_ai.request.reasoning.level anthropic.request.service_tier anthropic.thinking.type anthropic.thinking.budget_tokens].each do |key|
      refute attributes.key?(key), key
    end
  end

  def test_r4_verifier_httpx_follow_redirects_captures_final_route_once_with_native_parity
    @provider.stop
    @provider = MockHTTP.new do |request|
      path = request[:line].split[1]
      if path == '/v1/chat/completions'
        [200, {'Content-Type' => 'application/json'}, CHAT]
      else
        [307, {'Location' => '/v1/chat/completions'}, '']
      end
    end
    %w[/redirect /redirect/chat/completions].each do |path|
      %w[buffered stream headers].each do |mode|
        code = <<~RUBY
          require 'httpx'
          session = HTTPX.plugin(:follow_redirects)#{mode == 'buffered' ? '' : '.plugin(:stream)'}
          module ObserveRedirectIdentity
            def build_request(...)
              request = super
              request.on(:response) do |result|
                if result.status == 200
                  result.define_singleton_method(:application_helper) { 'native-helper' }
                  Thread.current.thread_variable_set(:native_redirect_response, result)
                end
              end
              request
            end
          end
          session.singleton_class.prepend(ObserveRedirectIdentity)
          response = session.post('#{@provider.url}#{path}', json: {model: 'gpt-4o-mini', messages: []}#{mode == 'buffered' ? '' : ', stream: true'})
          if #{(mode == 'buffered').inspect}
            print response.to_s
          else
            abort 'bad status' unless response.status == 200 if #{(mode == 'headers').inspect}
            response.each { |chunk| print chunk }
          end
          abort 'response class changed' unless response.class.name == #{(mode == 'buffered' ? 'HTTPX::Response' : 'HTTPX::StreamResponse').inspect} || response.class.ancestors.any? { |klass| klass.name == #{(mode == 'buffered' ? 'HTTPX::Response' : 'HTTPX::StreamResponse').inspect} }
          native = #{mode == 'buffered' ? 'response' : 'response.request.response'}
          abort 'native identity changed' unless native.equal?(Thread.current.thread_variable_get(:native_redirect_response))
          abort 'native helper lost' unless native.application_helper == 'native-helper'
          abort 'context leaked' if defined?(TraceAI::Auto) && TraceAI::Auto.current_capture
          abort 'batch state leaked' if Thread.current[:traceai_httpx_batch]
        RUBY
        [false, true].each do |injected|
          stdout, stderr, status = app(code, injected ? {} : {'RUBYOPT' => nil})
          assert status.success?, "#{path}/#{mode}: #{stderr}"; assert_equal CHAT, stdout
          requests = 2.times.map { @provider.requests.pop(true) }
          assert_equal ["POST #{path} HTTP/1.1\r\n", "POST /v1/chat/completions HTTP/1.1\r\n"], requests.map { |request| request[:line] }
          requests.each { |request| assert_equal({'model' => 'gpt-4o-mini', 'messages' => []}, JSON.parse(request[:body])) }
          spans = export_spans
          unless injected
            assert_empty spans; next
          end
          attributes = assert_chat_span(spans)
          assert_equal 'Hello from Ruby', attributes['output.value']
          assert_equal 200, attributes['http.response.status_code']
          refute attributes['traceai.wire.incomplete']
        end
      end
    end
  end
  def test_r4_verifier_httpx_redirected_credentials_survive_stripped_auth_headers
    key = 'REDIRECT_REQUEST_ONLY_KEY_12345678'
    payload = JSON.generate(choices: [{message: {role: 'assistant', content: "echo #{key}"}}])
    final = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, payload] }
    @provider.stop
    @provider = MockHTTP.new { |_r| [307, {'Location' => "#{final.url}/v1/chat/completions"}, ''] }
    code = <<~RUBY
      require 'httpx'
      response = HTTPX.plugin(:follow_redirects).post('#{@provider.url}/redirect', json: {model: 'm', messages: []}, headers: {'authorization' => 'Bearer #{key}'})
      print response.to_s
    RUBY
    [false, true].each do |standalone|
      [false, true].each do |injected|
        flags = standalone ? standalone_env : {}
        flags = flags.merge('RUBYOPT' => nil) unless injected
        stdout, stderr, status = app(code, flags, chdir: standalone ? File.dirname(ROOT) : ROOT)
        assert status.success?, stderr; assert_equal payload, stdout
        assert_equal "Bearer #{key}", @provider.requests.pop(true)[:headers]['authorization']
        refute final.requests.pop(true)[:headers].key?('authorization')
        spans = export_spans
        unless injected
          assert_empty spans; next
        end
        assert_equal 1, spans.length
        refute_includes spans.first.first.to_h.to_s, key
        assert_equal 'echo <redacted>', attrs(spans.first.first.attributes)['output.value']
      end
    end
  ensure
    final&.stop
  end

  def test_r4_verifier_httpx_redirect_batch_pairing_ancestry_and_nested_guard
    @provider.stop
    @provider = MockHTTP.new do |request|
      path = request[:line].split[1]
      if path.end_with?('/redirect')
        [307, {'Location' => path.sub('/redirect', '/chat/completions')}, '']
      else
        model = JSON.parse(request[:body]).fetch('model')
        [200, {'Content-Type' => 'application/json'}, JSON.generate(model: model, choices: [{message: {role: 'assistant', content: model + '-answer'}}])]
      end
    end
    code = <<~RUBY
      require 'httpx'; require 'net/http'
      session = HTTPX.plugin(:follow_redirects)
      module NestedRedirectCallback
        def build_request(...)
          request = super
          if request.uri.path == '/one/chat/completions'
            request.on(:response) do
              nested = Net::HTTP::Post.new('/nested/chat/completions')
              nested['content-type'] = 'application/json'; nested.body = '{"model":"nested","messages":[]}'
              Net::HTTP.start('127.0.0.1', #{@provider.port}) { |http| http.request(nested) }
            end
          end
          request
        end
      end
      session.singleton_class.prepend(NestedRedirectCallback)
      requests = %w[one two].map { |model| session.build_request('POST', '#{@provider.url}/' + model + '/redirect', json: {model: model, messages: []}) }
      responses = session.request(*requests)
      abort 'batch response pairing changed' unless responses.map { |response| response.json['model'] } == %w[one two]
      requests.each do |request|
        [request, request.redirect_request].each do |part|
          %i[idle response_started response].each do |event|
            abort 'agent callback retained' if part.send(:callbacks, event).any? { |callback| callback.source_location&.first&.end_with?('/traceai/auto/transports.rb') }
          end
          body = part.response.body
          abort 'body capture retained' if body.instance_variable_defined?(:@traceai_capture)
        end
      end
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      tracer.in_span('library', attributes: {'gen_ai.span.kind' => 'LLM'}) do
        tracer.in_span('native') { session.post('#{@provider.url}/covered/redirect', json: {model: 'covered', messages: []}) }
      end
      OpenTelemetry::Common::Utilities.untraced { session.post('#{@provider.url}/suppressed/redirect', json: {model: 'suppressed', messages: []}) }
      tracer.in_span('framework', attributes: {'gen_ai.span.kind' => 'TOOL'}) { session.post('#{@provider.url}/framework/redirect', json: {model: 'framework', messages: []}) }
      abort 'context leaked' if TraceAI::Auto.current_capture || Thread.current[:traceai_httpx_batch]
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    wire = spans.select { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
    assert_equal %w[framework one two], wire.map { |span| attrs(span.attributes)['gen_ai.request.model'] }.sort
    wire.each do |span|
      attributes = attrs(span.attributes)
      assert_equal attributes['gen_ai.request.model'] + '-answer', attributes['output.value']
      assert_equal 200, attributes['http.response.status_code']
    end
    framework = spans.find { |span| span.name == 'framework' }
    assert_equal framework.span_id, wire.find { |span| attrs(span.attributes)['gen_ai.request.model'] == 'framework' }.parent_span_id
  end
end

class Round4BoundaryTest
  def test_r4_verifier_embedded_json_errors_fail_closed_per_snapshot_not_batch
    credential = 'TOOL_CREDENTIAL_12345678'
    complete = JSON.generate(api_key: credential)
    original = record; original.attributes = {'output.value' => complete}
    assert_equal '<redacted>', JSON.parse(snapshot(original).attributes['output.value'])['api_key']
    [complete[0...-1], '[' * 70 + complete + ']' * 70].each do |bad|
      original = record
      original.attributes = {'output.value' => bad, 'input.value' => 'PRIVATE_INPUT'}
      original.status = OpenTelemetry::Trace::Status.error('error')
      copy = snapshot(original)
      assert_empty copy.attributes
      assert_equal original.name, copy.name
      assert_equal original.span_id, copy.span_id
      assert_equal original.start_timestamp, copy.start_timestamp
      assert_equal original.end_timestamp, copy.end_timestamp
      assert_equal 2, copy.status.code
      encoded = A::Protobuf.encode([copy, snapshot(record)])
      refute_empty encoded
      refute_includes encoded, credential
      refute_includes encoded, 'PRIVATE_INPUT'
      assert_equal bad, original.attributes['output.value']
    end
  end

  def test_r4_verifier_invocation_families_cover_new_nested_aliases_on_every_surface
    attributes = {'gen_ai.request.model' => 'model', 'gen_ai.request.stream' => true, 'anthropic.request.id' => 'request-id',
                  'gen_ai.request.reasoning.level' => 'PRIVATE_SETTING', 'gen_ai.request.future.nested' => 'PRIVATE_SETTING',
                  'anthropic.request.future' => 'PRIVATE_SETTING', 'anthropic.thinking' => '{"type":"PRIVATE_SETTING"}',
                  'anthropic.thinking.budget_tokens' => 1024}
    original = record
    original.attributes = attributes
    original.events = [OpenTelemetry::SDK::Trace::Event.new('event', attributes, 123)]
    original.resource = A::ExportResource.new(attributes)
    original.instrumentation_scope = A::ExportScope.new('scope', 'version', attributes)
    copy = snapshot(original, hide_llm_invocation_parameters: true)
    [copy.attributes, copy.events.first.attributes, copy.resource.attributes, copy.instrumentation_scope.attributes].each do |masked|
      assert_equal({'gen_ai.request.model' => 'model', 'gen_ai.request.stream' => true, 'anthropic.request.id' => 'request-id'}, masked)
    end
    refute_includes A::Protobuf.encode([copy]), 'PRIVATE_SETTING'
    assert_equal 'PRIVATE_SETTING', original.attributes['gen_ai.request.reasoning.level']
  end
end
