# frozen_string_literal: true
require_relative 'test_round2'

class EndToEndTest
  def test_standalone_transport_require_leaves_application_gem_selection_untouched
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
    Dir.mktmpdir('standalone-json-selection', File.join(ROOT, '.bundle/tmp')) do |directory|
      # No Gemfile exists at preload/transport-require time. The application can
      # select a gem directly, or choose a bundle later in its startup sequence.
      gemfile = File.join(directory, 'late.Gemfile')
      File.write(gemfile, "source 'https://rubygems.org'\ngem 'json', '2.6.3'\n")
      ["gem 'json', '2.6.3'", "ENV['BUNDLE_GEMFILE'] = #{gemfile.inspect}; require 'bundler/setup'"].each do |selection|
        before = "require 'net/http'; require 'stringio'; puts Gem.loaded_specs.keys.sort.join(',')"
        baseline, stderr, status = app(before + "; #{selection}; require 'json'; puts 'APPLICATION_RAN ' + JSON::VERSION", overrides.merge('RUBYOPT' => nil), chdir: File.dirname(ROOT))
        assert status.success?, stderr
        specs, output = baseline.split("\n", 2)
        assert_equal "APPLICATION_RAN 2.6.3\n", output
        code = <<~RUBY
          require 'net/http'; require 'stringio'
          abort 'agent selected gems on require' unless Gem.loaded_specs.keys.sort.join(',') == #{specs.inspect}
          abort 'JSON activated before selection' if defined?(JSON)
          #{selection}
          require 'json'; puts 'APPLICATION_RAN ' + JSON::VERSION
        RUBY
        stdout, stderr, status = app(code, overrides, chdir: File.dirname(ROOT))
        assert status.success?, stderr
        assert_equal output, stdout
        refute_includes stderr, 'already activated'
        assert_operator stderr.scan('traceai-auto:').length, :<=, 1
      end
    end
    assert_empty export_spans
  end

  def test_standalone_pinned_json_can_trace_first_request_without_bundler
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
    code = "require 'net/http'; gem 'json', '2.6.3'; require 'json'; puts JSON::VERSION\n" + plain_code
    stdout, stderr, status = app(code, overrides, chdir: File.dirname(ROOT))
    assert status.success?, stderr
    assert_equal "2.6.3\n" + CHAT, stdout
    assert_chat_span(export_spans)
  end

  def test_enabled_metadata_helper_is_a_noop_before_json_is_loaded
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
    code = <<~RUBY
      require 'net/http'
      abort 'JSON loaded before helper' if defined?(JSON)
      metadata = Object.new
      def metadata.to_json(*); abort 'serialized metadata before JSON selection'; end
      sentinel = Object.new
      result = TraceAI::Auto.using_metadata(metadata) { sentinel }
      abort 'result changed' unless result.equal?(sentinel)
      abort 'helper loaded JSON' if defined?(JSON)
      gem 'json', '2.6.3'; require 'json'; puts JSON::VERSION
    RUBY
    stdout, stderr, status = app(code, overrides, chdir: File.dirname(ROOT))
    assert status.success?, stderr
    assert_equal "2.6.3\n", stdout
    assert_empty export_spans
  end

  def test_httpx_header_first_stream_preserves_suppression_dedup_and_caller_context
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    [false, true].each do |header_first|
      code = <<~RUBY
        require 'httpx'
        def invoke
          response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
          abort 'wrong response status' unless response.status == 200 if #{header_first}
          response.each { |chunk| print chunk }
        end
        tracer = OpenTelemetry.tracer_provider.tracer('app')
        OpenTelemetry::Common::Utilities.untraced { invoke }
        %w[LLM EMBEDDING RERANKER GUARDRAIL].each do |kind|
          tracer.in_span('dedicated-' + kind, attributes: {'gen_ai.span.kind' => kind}) { invoke }
        end
        %w[CHAIN AGENT TOOL].each do |kind|
          tracer.in_span('framework-' + kind, attributes: {'gen_ai.span.kind' => kind}) do
            TraceAI::Auto.using_session('s-' + kind) do
              TraceAI::Auto.using_user('u-' + kind) do
                TraceAI::Auto.using_metadata(tenant: kind) { invoke }
              end
            end
          end
        end
        abort 'caller suppression leaked' if OpenTelemetry::Common::Utilities.untraced?(OpenTelemetry::Context.current)
        abort 'capture leaked' if TraceAI::Auto.current_capture
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_equal STREAM.join * 8, stdout
      spans = export_spans.map(&:first)
      wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
      assert_equal 3, wire.length, "header_first=#{header_first}"
      %w[CHAIN AGENT TOOL].each do |kind|
        parent = spans.find { |s| s.name == 'framework-' + kind }; refute_nil parent
        child = wire.find { |s| s.parent_span_id == parent.span_id }; refute_nil child
        a = attrs(child.attributes)
        assert_equal '<redacted>', a['session.id'] # S6 session-name rule
        assert_equal 'u-' + kind, a['user.id']
        assert_equal({'tenant' => kind}, JSON.parse(a['metadata']))
        assert_equal 'one two', a['output.value']
      end
    end
  end

  def test_httpx_header_first_stream_retains_request_parent_when_iteration_scope_changes
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = <<~RUBY
      require 'httpx'
      response = nil
      OpenTelemetry.tracer_provider.tracer('app').in_span('original') do
        TraceAI::Auto.using_session('original-session') do
          response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
          abort 'bad header' unless response.status == 200
        end
      end
      TraceAI::Auto.using_session('different-session') { response.each { |chunk| print chunk } }
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal STREAM.join, stdout
    spans = export_spans.map(&:first)
    wire = spans.find { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }; refute_nil wire
    assert_equal spans.find { |s| s.name == 'original' }.span_id, wire.parent_span_id
    assert_equal '<redacted>', attrs(wire.attributes)['session.id'] # S6 session-name rule
  end

  def test_httpx_header_first_consumer_exceptions_finalize_without_transport_error
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    %w[IOError Timeout::Error ArgumentError].each do |type|
      code = <<~RUBY
        require 'httpx'; require 'timeout'
        response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
        abort 'bad status' unless response.status == 200
        failure = #{type}.new('consumer failure')
        begin
          response.each { |chunk| print chunk; raise failure }
        rescue #{type} => e
          abort 'exception identity changed' unless e.equal?(failure)
          abort 'backtrace changed' unless e.backtrace.first.include?('-e:')
          puts 'CONSUMER_ABORT'
        end
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_equal STREAM.first + "CONSUMER_ABORT\n", stdout
      spans = export_spans; assert_equal 1, spans.length
      span = spans.first.first; a = attrs(span.attributes)
      assert_equal :STATUS_CODE_OK, span.status.code
      assert_equal true, a['traceai.wire.incomplete']
      refute a['error.type']
      assert_equal 'one', a['output.value']
    end
  end

  def test_native_anthropic_content_is_hidden_at_fi_export_without_mutating_application_spans
    @provider.stop
    payload = JSON.generate(id: 'msg-private', type: 'message', role: 'assistant', model: 'claude-test', content: [{type: 'text', text: 'PRIVATE_OUTPUT_MARKER'}], stop_reason: 'end_turn', usage: {input_tokens: 3, output_tokens: 2})
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, payload] }
    [
      {'FI_HIDE_INPUTS' => 'true', 'FI_HIDE_OUTPUTS' => 'true'},
      {'FI_HIDE_INPUT_MESSAGES' => 'true', 'FI_HIDE_OUTPUT_MESSAGES' => 'true'},
      {'FI_HIDE_INPUT_TEXT' => 'true', 'FI_HIDE_OUTPUT_TEXT' => 'true'}
    ].each do |flags|
      code = <<~RUBY
        require 'anthropic'
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
        client = Anthropic::Client.new(api_key: 'mock-key', base_url: '#{@provider.url}', max_retries: 0, open_telemetry: {traces: {content_mode: :content}})
        response = client.messages.create(model: 'claude-test', max_tokens: 16, system: 'PRIVATE_SYSTEM_MARKER', messages: [{role: 'user', content: 'PRIVATE_INPUT_MARKER'}])
        abort 'application response changed' unless response.content.first.text == 'PRIVATE_OUTPUT_MARKER'
        native = memory.finished_spans.find { |s| s.attributes.key?('gen_ai.input.messages') }
        abort 'native span missing' unless native
        original = JSON.generate(native.attributes)
        %w[PRIVATE_INPUT_MARKER PRIVATE_OUTPUT_MARKER PRIVATE_SYSTEM_MARKER].each do |marker|
          abort 'FI mutated another exporter' unless original.include?(marker)
        end
        puts 'APPLICATION_RAN'
      RUBY
      stdout, stderr, status = app(code, flags)
      assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
      spans = export_spans.map(&:first)
      assert_operator spans.length, :>=, 2
      native = spans.find { |s| attrs(s.attributes)['traceai.capture.layer'] != 'wire' }; refute_nil native
      %w[PRIVATE_INPUT_MARKER PRIVATE_OUTPUT_MARKER PRIVATE_SYSTEM_MARKER].each do |marker|
        refute_includes spans.map(&:to_h).to_s, marker
      end
    end
  end

  def test_content_length_completion_survives_consumer_nonlocal_exit_for_both_transports
    %w[net_http httpx httpx_headers].each do |transport|
      [true, false].each do |complete|
        # Exact Content-Length proves completion before callback unwinding.
        # A larger declared body proves the otherwise identical exit is partial.
        @provider.raw do |io, _|
          io.write "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: #{CHAT.bytesize + (complete ? 0 : 100)}\r\nConnection: close\r\n\r\n#{CHAT}"
          io.flush
        end
        code = if transport == 'net_http'
                 plain_code(stream: true).sub('STDOUT.write(s)', "STDOUT.write(s); received += s.bytesize; throw :finished if received == #{CHAT.bytesize}")
               else
                 <<~RUBY
                   require 'httpx'
                   response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: []}, stream: true)
                   abort 'bad status' unless response.status == 200 if #{transport == 'httpx_headers'}
                   response.each { |chunk| print chunk; received += chunk.bytesize; throw :finished if received == #{CHAT.bytesize} }
                 RUBY
               end
        [false, true].each do |injected|
          stdout, stderr, status = app("received = 0; catch(:finished) { #{code} }; puts 'EXITED'", injected ? {} : {'RUBYOPT' => nil})
          assert status.success?, "#{transport}/#{complete}: #{stderr}"
          assert_equal CHAT + "EXITED\n", stdout
          spans = export_spans
          unless injected
            assert_empty spans; next
          end
          a = assert_chat_span(spans)
          assert_equal 'Hello from Ruby', a['output.value']
          if complete
            assert_nil a['traceai.wire.incomplete'], transport
          else
            assert_equal true, a['traceai.wire.incomplete'], transport
          end
          refute a['error.type']
        end
      end
    end
  end
end

class SafetyTest
  def test_json_valued_native_message_aliases_follow_side_and_granular_hiding
    attributes = {
      'gen_ai.input.messages' => JSON.generate([{role: 'user', content: 'PRIVATE_INPUT_MARKER'}]),
      'gen_ai.output.messages' => JSON.generate([{role: 'assistant', content: 'PRIVATE_OUTPUT_MARKER'}]),
      'gen_ai.system_instructions' => JSON.generate([{type: 'text', text: 'PRIVATE_SYSTEM_MARKER'}]),
      'gen_ai.system_instructions.0.text' => 'PRIVATE_SYSTEM_MARKER',
      'gen_ai.response.id' => 'safe-id'
    }
    %w[hide_inputs hide_input_messages hide_input_text].each do |flag|
      masked = P.mask(attributes, TraceAI::Auto::Config.new({flag => true}, env: {}))
      refute_includes JSON.generate(masked), 'PRIVATE_INPUT_MARKER'
      refute_includes JSON.generate(masked), 'PRIVATE_SYSTEM_MARKER'
      assert_includes masked['gen_ai.output.messages'], 'PRIVATE_OUTPUT_MARKER'
      assert_equal 'safe-id', masked['gen_ai.response.id']
    end
    %w[hide_outputs hide_output_messages hide_output_text].each do |flag|
      masked = P.mask(attributes, TraceAI::Auto::Config.new({flag => true}, env: {}))
      refute_includes JSON.generate(masked), 'PRIVATE_OUTPUT_MARKER'
      assert_includes masked['gen_ai.input.messages'], 'PRIVATE_INPUT_MARKER'
    end
    image_hidden = P.mask(attributes, TraceAI::Auto::Config.new({hide_input_images: true}, env: {}))
    refute image_hidden.key?('gen_ai.input.messages')
  end

  def test_cohere_v2_streamed_tool_calls_accumulate_by_index_and_emit_tool_only_alias
    req = request('/v2/chat', 'api.cohere.com', {model: 'command-r', messages: [], stream: true})
    events = [
      {type: 'message-start', delta: {message: {id: 'tool-response', role: 'assistant'}}},
      {type: 'tool-call-start', index: 1, delta: {message: {tool_calls: {id: 'call-2', type: 'function', function: {name: 'time', arguments: ''}}}}},
      {type: 'tool-call-start', index: 0, delta: {message: {tool_calls: {id: 'call-1', type: 'function', function: {name: 'weather', arguments: '{"city":'}}}}},
      {type: 'tool-call-delta', index: 0, delta: {message: {tool_calls: {function: {arguments: '"Boston"}'}}}}},
      {type: 'tool-call-delta', index: 1, delta: {message: {tool_calls: {function: {arguments: '{}'}}}}},
      {type: 'message-end', delta: {finish_reason: 'TOOL_CALL', usage: {tokens: {input_tokens: 2, output_tokens: 4}}}}
    ]
    a = W.build(req, response(events.map { |e| "event: #{e[:type]}\ndata: #{JSON.generate(e)}\n\n" }.join, 'text/event-stream')).attributes
    prefix = 'gen_ai.output.messages.0.message.tool_calls'
    assert_equal 'call-1', a["#{prefix}.0.tool_call.id"]
    assert_equal 'weather', a["#{prefix}.0.tool_call.function.name"]
    assert_equal '{"city":"Boston"}', a["#{prefix}.0.tool_call.function.arguments"]
    assert_equal 'call-2', a["#{prefix}.1.tool_call.id"]
    assert_equal 'time', a["#{prefix}.1.tool_call.function.name"]
    assert_equal '{}', a["#{prefix}.1.tool_call.function.arguments"]
    assert_equal [{'id' => 'call-1', 'name' => 'weather', 'arguments' => '{"city":"Boston"}'}, {'id' => 'call-2', 'name' => 'time', 'arguments' => '{}'}], JSON.parse(a['output.value'])
    assert_equal 'application/json', a['output.mime_type']
    assert_equal ['TOOL_CALL'], a['gen_ai.response.finish_reasons']
    assert_equal 'tool-response', a['gen_ai.response.id']
    assert_equal 2, a['gen_ai.usage.input_tokens']; assert_equal 4, a['gen_ai.usage.output_tokens']
  end

  def test_cohere_v1_ndjson_and_sse_stream_content_id_finish_and_usage
    req = request('/v1/chat', 'api.cohere.com', {model: 'command-r', message: 'hi', stream: true})
    events = [
      {event_type: 'stream-start', generation_id: 'v1-id'},
      {event_type: 'text-generation', text: 'hello '},
      {event_type: 'text-generation', text: 'world'},
      {event_type: 'stream-end', finish_reason: 'COMPLETE', response: {generation_id: 'v1-id', text: 'hello world', meta: {billed_units: {input_tokens: 2, output_tokens: 4}}}}
    ]
    ['application/x-ndjson', 'text/event-stream'].each do |type|
      [false, true].each do |partial|
        selected = partial ? events.first(3) : events
        body = selected.map { |e| type == 'text/event-stream' ? "data: #{JSON.generate(e)}\n\n" : JSON.generate(e) + "\n" }.join
        a = W.build(req, response(body, type).merge(incomplete: partial)).attributes
        assert_equal 'hello world', a['output.value']
        assert_equal 'hello world', a['gen_ai.output.messages.0.message.content']
        assert_equal 'v1-id', a['gen_ai.response.id']
        assert_equal true, a['gen_ai.request.stream']
        if partial
          assert_equal true, a['traceai.wire.incomplete']; refute a['gen_ai.usage.total_tokens']
        else
          assert_equal ['COMPLETE'], a['gen_ai.response.finish_reasons']
          assert_equal 2, a['gen_ai.usage.input_tokens']; assert_equal 4, a['gen_ai.usage.output_tokens']; assert_equal 6, a['gen_ai.usage.total_tokens']
        end
      end
    end
  end

  def test_every_streaming_endpoint_variant_sets_stream_attribute_without_sse
    cases = [
      [request('/model/anthropic.claude-test/invoke-with-response-stream', 'bedrock-runtime.us-east-1.amazonaws.com', {messages: []}), response('BINARY', 'application/vnd.amazon.eventstream'), 'bedrock.invoke_model'],
      [request('/model/anthropic.claude-test/converse-stream', 'bedrock-runtime.us-east-1.amazonaws.com', {messages: []}), response('BINARY', 'application/vnd.amazon.eventstream'), 'bedrock.converse'],
      [request('/v1/projects/p/locations/us/publishers/anthropic/models/claude-test:streamRawPredict', 'us-aiplatform.googleapis.com', {messages: []}), response({}), 'vertex.anthropic_raw_predict'],
      [request('/v1beta/models/gemini-test:streamGenerateContent', 'generativelanguage.googleapis.com', {contents: [{parts: [{text: 'hi'}]}]}), response([]), 'gemini.generate_content']
    ]
    cases.each do |req, resp, endpoint|
      result = W.build(req, resp); refute_nil result, endpoint
      assert_equal endpoint, result.attributes['traceai.wire.endpoint']
      assert_equal true, result.attributes['gen_ai.request.stream'], endpoint
    end
  end

  def test_openai_responses_text_parts_use_newlines_for_buffered_and_final_stream_messages
    req = request('/v1/responses', 'api.openai.com', {model: 'm', input: 'hi'})
    final = {id: 'r-id', status: 'completed', output: [{type: 'message', content: [{type: 'output_text', text: 'one'}, {type: 'refusal', refusal: 'skip'}, {type: 'output_text', text: 'two'}]}]}
    [response(final), response("data: #{JSON.generate(type: 'response.completed', response: final)}\n\n", 'text/event-stream')].each do |resp|
      a = W.build(req, resp).attributes
      assert_equal "one\ntwo", a['output.value']
      assert_equal "one\ntwo", a['gen_ai.output.messages.0.message.content']
    end
    deltas = %w[one two].map { |text| "data: #{JSON.generate(type: 'response.output_text.delta', delta: text)}\n\n" }.join
    assert_equal 'onetwo', W.build(req, response(deltas, 'text/event-stream')).attributes['output.value']
  end
end
