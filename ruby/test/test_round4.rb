# frozen_string_literal: true
require_relative 'test_round3'

class EndToEndTest
  def test_round4_sdk_upload_callback_preserves_nested_suppression_and_context
    assert_round4_sdk_upload_callback('suppressed')
  end

  def test_round4_sdk_upload_callback_preserves_nested_capture_guard
    assert_round4_sdk_upload_callback('nested')
  end

  def test_round4_sdk_upload_callback_preserves_callback_parent_and_metadata
    assert_round4_sdk_upload_callback('metadata')
  end

  def assert_round4_sdk_upload_callback(mode)
    @provider.stop
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, '{"text":"answer"}'] }
    callback_server = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, CHAT] }
    file = Tempfile.new('sdk-callback', File.join(ROOT, '.bundle/tmp'))
    file.write('upload bytes'); file.close
    code = official_client_code + <<~RUBY
      file = File.open(#{file.path.inspect}, 'rb')
      file.define_singleton_method(:read) do |*args|
        unless @called
          @called = true
          TraceAI::Auto.using_session('callback-session') do
            TraceAI::Auto.using_user('callback-user') do
              TraceAI::Auto.using_metadata(tenant: 'callback') do
                OpenTelemetry.tracer_provider.tracer('app').in_span('callback-parent') do
                  scope = #{mode == 'suppressed' ? 'OpenTelemetry::Common::Utilities.method(:untraced)' : '->(&block) { block.call }'}
                  scope.call do
                    context = OpenTelemetry::Context.current
                    request = Net::HTTP::Post.new('/v1/chat/completions')
                    request['content-type'] = 'application/json'
                    request.body = '{"model":"callback","messages":[]}'
                    response = Net::HTTP.start('127.0.0.1', #{callback_server.port}) { |h| h.request(request) }
                    abort 'callback result changed' unless response.body == #{CHAT.inspect}
                    abort 'callback context replaced' unless OpenTelemetry::Context.current.equal?(context)
                  end
                end
              end
            end
          end
        end
        super(*args)
      end
      # A raw File body makes ReadIOAdapter call the application's #read callback
      # inside the real 0.75.0 network Enumerator Fiber.
      TraceAI::Auto.using_session('outer-sdk-session') do
        result = c.request(method: :post, path: #{(mode == 'metadata' ? 'uploads/callback' : 'audio/transcriptions').inspect}, headers: {'content-type' => 'application/octet-stream'}, body: file, model: OpenAI::Models::Audio::TranscriptionCreateResponse)
        print result.text
      end
      abort 'callback not called' unless file.instance_variable_get(:@called)
      abort 'SDK bridge leaked' if Thread.current.thread_variable_get(:traceai_sdk_context)
      abort 'capture leaked' if TraceAI::Auto.current_capture
      file.close
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal 'answer', stdout
    assert_equal 1, callback_server.requests.size
    spans = export_spans.map(&:first)
    wire = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    if mode == 'metadata'
      assert_equal 1, wire.length
      a = attrs(wire.first.attributes)
      assert_equal callback_server.port, a['server.port']
      assert_equal '<redacted>', a['session.id'] # S6 session-name rule
      assert_equal 'callback-user', a['user.id']
      assert_equal({'tenant' => 'callback'}, JSON.parse(a['metadata']))
      assert_equal spans.find { |s| s.name == 'callback-parent' }.span_id, wire.first.parent_span_id
    else
      assert_empty wire
    end
  ensure
    callback_server&.stop; file&.unlink
  end

  def test_round4_native_anthropic_echoed_request_key_is_scrubbed_only_for_fi
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
      abort 'application output changed' unless response.content.first.text == #{("echo " + key).inspect}
      native = memory.finished_spans.find { |s| s.name == 'anthropic.messages.create' }
      abort 'other exporter mutated' unless native && JSON.generate(native.attributes).include?(#{key.inspect})
      TraceAI::Auto.flush
      abort 'export mutated original span' unless JSON.generate(native.attributes).include?(#{key.inspect})
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert spans.any? { |s| s.name == 'anthropic.messages.create' }
    assert spans.any? { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
    refute_includes spans.map(&:to_h).to_s, key
    native = spans.find { |s| s.name == 'anthropic.messages.create' }
    assert_includes attrs(native.attributes)['gen_ai.output.messages'], '<redacted>'
  end

  def test_round4_export_scrubs_names_events_links_without_mutating_other_exporters
    secret = 'fixture-api-12345678'
    signed = 'SIGNED_PRIVATE_VALUE'
    code = <<~RUBY
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      name = 'echo #{secret} https://cdn.test/x?signature=#{signed}'
      linked = OpenTelemetry::Trace::SpanContext.new
      link = OpenTelemetry::Trace::Link.new(linked, {'nested' => '{"api_key":"link-private-value"}', 'ordinary' => name, 'output.value' => 'PRIVATE_LINK_OUTPUT'})
      span = TraceAI::Auto.tracer.start_span(name, links: [link], attributes: {'ordinary' => name})
      span.add_event(name, attributes: {'ordinary' => name, 'output.value' => 'PRIVATE_EVENT_OUTPUT'})
      span.finish
      TraceAI::Auto.flush
      original = memory.finished_spans.first
      abort 'original name changed' unless original.name == name
      abort 'original event changed' unless original.events.first.name == name && original.events.first.attributes['ordinary'] == name
      abort 'original link changed' unless original.links.first.equal?(link) && link.attributes['ordinary'] == name
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code, {'FI_HIDE_OUTPUTS' => 'true'})
    assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first); assert_equal 1, spans.length
    span = spans.first
    serialized = span.to_h.to_s
    [secret, signed, 'link-private-value', 'PRIVATE_LINK_OUTPUT', 'PRIVATE_EVENT_OUTPUT'].each { |value| refute_includes serialized, value }
    assert_equal 'echo <redacted> https://cdn.test/x?signature=<redacted>', span.name
    assert_equal span.name, span.events.first.name
    assert_equal '<redacted>', JSON.parse(attrs(span.links.first.attributes)['nested'])['api_key']
    assert_equal '__REDACTED__', attrs(span.links.first.attributes)['output.value']
    assert_equal 8, span.links.first.span_id.bytesize
  end

  def test_round4_utf8_resident_multipart_is_byte_identical_and_captured
    assert_round4_utf8_multipart('resident')
  end

  def test_round4_utf8_file_multipart_is_byte_identical_and_captured
    assert_round4_utf8_multipart('file')
  end

  def assert_round4_utf8_multipart(kind)
    body = "--utf8\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--utf8\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\ncafé世界\r\n--utf8\r\nContent-Disposition: form-data; name=\"file\"; filename=\"é.wav\"\r\nContent-Type: audio/wav\r\n\r\nAUDIO\x00BYTES\r\n--utf8--\r\n"
    file = Tempfile.new('utf8-multipart', File.join(ROOT, '.bundle/tmp'))
    file.binmode; file.write('PREFIX' + body); file.close
    code = <<~RUBY
      require 'net/http'
      original = #{body.inspect}
      source = File.open(#{file.path.inspect}, 'rb'); source.seek(6)
      req = Net::HTTP::Post.new('/v1/audio/transcriptions')
      req['content-type'] = 'multipart/form-data; boundary=utf8'
      req.content_length = original.bytesize
      #{kind == 'resident' ? 'req.body = original' : 'req.body_stream = source'}
      result = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |h| h.request(req) }
      abort 'resident body mutated' unless original == #{body.inspect} && original.encoding == Encoding::UTF_8
      abort 'File identity changed' unless req.body_stream.equal?(source) if #{kind == 'file'}
      abort 'File position changed' unless source.pos == source.size if #{kind == 'file'}
      print result.body
      source.close
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, "#{kind}: #{stderr}"; assert_equal CHAT, stdout
    assert_equal body.b, @provider.requests.pop(true)[:body].b
    spans = export_spans; assert_equal 1, spans.length, kind
    a = attrs(spans.first.first.attributes)
    assert_equal 'whisper-1', a['gen_ai.request.model']
    assert_equal 'audio/wav', a['gen_ai.audio.mime_type']
    assert_equal 11, a['traceai.audio.input_bytes']
    input = JSON.parse(a['input.value'])
    assert_equal 'café世界', input['prompt']
    assert_equal 'é.wav', input.dig('file', 'filename')
    assert_equal 'audio/wav', input.dig('file', 'content_type')
    refute a['traceai.wire.body_unavailable']
    refute_includes a.to_s, 'AUDIO'
  ensure
    file&.unlink
  end

  def test_round4_httpx_header_first_close_finalizes_exactly_once_without_draining
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    %w[close body.close].each do |close|
      [false, true].each do |injected|
        code = <<~RUBY
          require 'httpx'
          response = HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true)
          puts response.status
          first = response.to_s.dup
          result = response.#{close}
          abort 'close drained response' unless response.to_s == first
          response.#{close}
          puts 'closed'
        RUBY
        stdout, stderr, status = app(code, injected ? {} : {'RUBYOPT' => nil})
        assert status.success?, stderr; assert_equal "200\nclosed\n", stdout
        spans = export_spans
        unless injected
          assert_empty spans; next
        end
        assert_equal 1, spans.length
        span = spans.first.first; a = attrs(span.attributes)
        assert_equal :STATUS_CODE_OK, span.status.code
        assert_equal true, a['traceai.wire.incomplete']
        assert_equal 'one', a['output.value']
        refute a['error.type']
      end
    end
  end
end

class SafetyTest
  def test_round4_incremental_utf8_multipart_headers_survive_every_byte_boundary
    require_relative '../lib/traceai/auto/upload'
    body = "--b\r\nContent-Disposition: form-data; name=\"modèle\"\r\n\r\ncafé世界\r\n--b\r\nContent-Disposition: form-data; name=\"fichieré\"; filename=\"é世界.wav\"\r\nContent-Type: audio/wav\r\n\r\nBINARY\r\n--b--\r\n"
    resident = W.multipart(body, 'multipart/form-data; boundary=b')
    assert_equal 'café世界', resident[0]['modèle']
    assert_equal 'é世界.wav', resident[1].first[:filename]
    [1, 2, 7, 17, 16_384].each do |size|
      reader = TraceAI::Auto::MultipartReader.new('multipart/form-data; boundary=b')
      offset = 0
      while offset < body.bytesize
        reader.feed(body.byteslice(offset, size)); offset += size
      end
      assert reader.complete
      assert_equal resident, [reader.fields, reader.files]
      assert JSON.generate(reader.files).include?('é世界.wav')
      refute_includes JSON.generate(reader.files), 'BINARY'
    end
  end

  def test_round4_interrupted_responses_retain_function_calls_and_argument_fragments
    req = request('/v1/responses', 'api.openai.com', {model: 'm', input: 'hi', stream: true})
    events = [
      {type: 'response.created', response: {id: 'resp-partial', model: 'm'}},
      {type: 'response.output_item.added', output_index: 0, item: {id: 'fc-1', type: 'function_call', call_id: 'call-1', name: 'weather', arguments: ''}},
      {type: 'response.function_call_arguments.delta', output_index: 0, item_id: 'fc-1', delta: '{"city":'},
      {type: 'response.function_call_arguments.delta', item_id: 'fc-1', delta: '"London"}'}
    ]
    [false, true].each do |transport_error|
      resp = response(events.map { |e| "data: #{JSON.generate(e)}\n\n" }.join, 'text/event-stream')
      resp.merge!(transport_error ? {error: Timeout::Error.new('wire timeout')} : {incomplete: true})
      result = W.build(req, resp); a = result.attributes
      assert_equal !transport_error, result.ok
      prefix = 'gen_ai.output.messages.0.message.tool_calls.0.tool_call'
      assert_equal 'call-1', a["#{prefix}.id"]
      assert_equal 'weather', a["#{prefix}.function.name"]
      assert_equal '{"city":"London"}', a["#{prefix}.function.arguments"]
      assert_equal [{'id' => 'call-1', 'name' => 'weather', 'arguments' => '{"city":"London"}'}], JSON.parse(a['output.value'])
      assert_equal 'application/json', a['output.mime_type']
      assert_equal 'resp-partial', a['gen_ai.response.id']
      assert_equal 'Timeout::Error', a['error.type'] if transport_error
    end
    events << {type: 'response.function_call_arguments.done', output_index: 0, item_id: 'fc-1', arguments: '{"city":"London"}'}
    a = W.build(req, response(events.map { |e| "data: #{JSON.generate(e)}\n\n" }.join, 'text/event-stream')).attributes
    assert_equal '{"city":"London"}', a['gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments']
  end

  def test_round4_ollama_tool_only_json_and_ndjson_preserve_structured_arguments
    req = request('/api/chat', 'ollama.com', {model: 'llama3', messages: []})
    call = {function: {name: 'weather', arguments: {city: 'London'}}}
    final = {model: 'llama3', message: {role: 'assistant', content: '', tool_calls: [call]}, done: true, done_reason: 'stop', prompt_eval_count: 2, eval_count: 3}
    [response(final), response(JSON.generate(final) + "\n", 'application/x-ndjson')].each do |resp|
      a = W.build(req, resp).attributes
      prefix = 'gen_ai.output.messages.0.message.tool_calls.0.tool_call'
      assert_equal 'weather', a["#{prefix}.function.name"]
      assert_equal({'city' => 'London'}, JSON.parse(a["#{prefix}.function.arguments"]))
      output = JSON.parse(a['output.value'])
      assert_equal 'weather', output.first['name']
      assert_equal({'city' => 'London'}, JSON.parse(output.first['arguments']))
      assert_equal 'application/json', a['output.mime_type']
      assert_equal 5, a['gen_ai.usage.total_tokens']
    end
  end

  def test_round4_streamed_function_name_fragments_accumulate_per_choice_and_call
    req = request('/v1/chat/completions', 'api.openai.com', {model: 'm', messages: [], stream: true})
    events = [
      {choices: [{index: 0, delta: {tool_calls: [{index: 0, id: 'call-1', function: {name: 'get_', arguments: '{"city":'}}, {index: 1, id: 'call-2', function: {name: 'get_', arguments: ''}}]}}]},
      {choices: [{index: 0, delta: {tool_calls: [{index: 1, function: {name: 'time', arguments: '{}'}}, {index: 0, function: {name: 'weather', arguments: '"London"}'}}]}, finish_reason: 'tool_calls'}]}
    ]
    a = W.build(req, response(events.map { |e| "data: #{JSON.generate(e)}\n\n" }.join, 'text/event-stream')).attributes
    prefix = 'gen_ai.output.messages.0.message.tool_calls'
    assert_equal 'get_weather', a["#{prefix}.0.tool_call.function.name"]
    assert_equal 'get_time', a["#{prefix}.1.tool_call.function.name"]
    assert_equal '{"city":"London"}', a["#{prefix}.0.tool_call.function.arguments"]
    assert_equal '{}', a["#{prefix}.1.tool_call.function.arguments"]
    assert_equal %w[get_weather get_time], JSON.parse(a['output.value']).map { |c| c['name'] }
  end
end
