# frozen_string_literal: true
require_relative 'test_round5_findings'

class EndToEndTest
  def r5_followup_proxy_code(transport, credential, explicit_start: false)
    <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'httpx'
      unless OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
        OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new
      end
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      before = tracer.start_span('proxy-before-request', with_parent: OpenTelemetry::Context.empty)
      failure = nil
      tracer.in_span('proxy-application') do |application|
        begin
          tracer.in_span('proxy-covered', attributes: {'gen_ai.span.kind' => 'LLM'}) do
            tracer.in_span('proxy-intermediate') do
              if #{(transport == 'net_http_https').inspect}
                client = Net::HTTP::Proxy('127.0.0.1', #{@provider.port}, 'r5-connect-user', 'r5-connect-password').new('127.0.0.1', 1)
                client.use_ssl = true
                client.start if #{explicit_start.inspect}
                request = Net::HTTP::Post.new('/v1/chat/completions')
                request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[]}'
                client.request(request)
                abort 'CONNECT rejection lost'
              else
                session = HTTPX.plugin(:proxy).with(proxy: {uri: '#{@provider.url}', username: 'r5-connect-user', password: 'r5-connect-password'})
                response = session.post('#{transport == 'httpx_https_proxy' ? 'https' : 'http'}://127.0.0.1:1/v1/chat/completions', json: {model: 'm', messages: []})
                abort 'proxy status changed' unless response.status == 401
                raise RuntimeError, response.to_s
              end
            end
          end
        rescue StandardError => e
          failure = e
          abort 'proxy error changed' unless e.message.include?(#{credential.inspect})
          application.set_attribute('ordinary', e.message)
          application.status = OpenTelemetry::Trace::Status.error(e.message)
          application.record_exception(e)
          before.set_attribute('ordinary', e.message)
          before.status = OpenTelemetry::Trace::Status.error(e.message)
          before.record_exception(e)
        end
      end
      before.finish
      tracer.in_span('proxy-unrelated') do |span|
        span.set_attribute('ordinary', failure.message)
        span.status = OpenTelemetry::Trace::Status.error(failure.message)
        span.record_exception(failure)
      end
      original = memory.finished_spans.find { |span| span.name == 'proxy-application' }
      if defined?(TraceAI::Auto)
        TraceAI::Auto.flush
        abort 'other exporter attributes mutated' unless original.attributes['ordinary'].include?(#{credential.inspect})
        abort 'other exporter status mutated' unless original.status.description.include?(#{credential.inspect})
        abort 'other exporter events mutated' unless original.events.any? { |event| event.attributes['exception.message'].include?(#{credential.inspect}) }
      end
      print failure.class.name + '|' + failure.message
    RUBY
  end

  def assert_r5_followup_native_proxy(transport, explicit_start: false)
    credential = 'Basic ' + ['r5-connect-user:r5-connect-password'].pack('m0')
    @provider.raw do |io, request|
      echo = request[:headers].fetch('proxy-authorization')
      body = 'proxy echoes ' + echo
      io.write "HTTP/1.1 401 #{body}\r\nContent-Type: text/plain\r\nContent-Length: #{body.bytesize}\r\nConnection: close\r\n\r\n#{body}"
    end
    code = r5_followup_proxy_code(transport, credential, explicit_start: explicit_start)
    baseline = nil
    [nil, {}, {'FI_WIRE_CAPTURE' => 'false'}].each do |flags|
      stdout, stderr, status = app(code, flags || {'RUBYOPT' => nil})
      assert status.success?, stderr
      assert_includes stdout, credential
      baseline ||= stdout
      assert_equal baseline, stdout
      request = @provider.requests.pop(true)
      assert_equal credential, request[:headers]['proxy-authorization']
      assert_match(transport == 'httpx_proxy' ? /\APOST http:\/\/127\.0\.0\.1:1\// : /\ACONNECT 127\.0\.0\.1:1 /, request[:line])
      assert_empty @provider.requests
      spans = export_spans.map(&:first)
      unless flags
        assert_empty spans
        next
      end
      assert_equal 5, spans.length
      assert spans.none? { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
      refute spans.any? { |span| span.to_h.to_s.include?(credential.delete_prefix('Basic ')) }, 'proxy credential leaked into OTLP'
      %w[proxy-intermediate proxy-covered proxy-application proxy-before-request proxy-unrelated].each do |name|
        span = spans.find { |candidate| candidate.name == name }
        refute_nil span, name
        assert_equal :STATUS_CODE_ERROR, span.status.code
        assert_includes span.status.message, '<redacted>' unless %w[proxy-intermediate proxy-covered].include?(name)
        assert span.events.any? { |event| attrs(event.attributes)['exception.message'].include?('<redacted>') }
      end
    end
  end

  def test_r5_followup_net_http_https_connect_authentication_is_observed_when_covered
    [false, true].each { |explicit_start| assert_r5_followup_native_proxy('net_http_https', explicit_start: explicit_start) }
  end

  def test_r5_followup_httpx_serializer_proxy_authentication_is_observed_when_covered
    %w[httpx_proxy httpx_https_proxy].each { |transport| assert_r5_followup_native_proxy(transport) }
  end

  def assert_r5_followup_media_export(attributes, payloads, media_keys, visible)
    code = <<~RUBY
      require 'opentelemetry/sdk'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      attributes = #{attributes.inspect}
      OpenTelemetry.tracer_provider.tracer('app').in_span('followup-media', attributes: attributes) { |span| span.add_event('media', attributes: attributes) }
      original = memory.finished_spans.first
      TraceAI::Auto.flush
      abort 'other exporter attributes mutated' unless original.attributes == attributes
      abort 'other exporter events mutated' unless original.events.first.attributes == attributes
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert_equal 1, spans.length
    span = spans.first
    payloads.each { |payload| refute_includes span.to_h.to_s, payload }
    [attrs(span.attributes), attrs(span.events.first.attributes)].each do |safe|
      media_keys.each { |key| assert_includes safe.fetch(key), '<omitted:', key }
      visible.each { |key, value| assert_equal value, safe.fetch(key), key }
    end
  end

  def test_r5_followup_flattened_and_indexed_mime_siblings_scrub_short_audio_in_otlp
    audio = r5_verifier_audio_attributes.fetch('input.audio.0')
    attributes = {
      'input.source.data' => audio, 'input.source.media_type' => 'audio/wav',
      'input.attachment.0.data' => audio, 'input.attachment.0.mimeType' => 'audio/wav',
      'input.attachment.2.bytes.0' => audio, 'input.attachment.2.content_type' => 'audio/wav',
      'input.value' => JSON.generate(attachment: {mimeType: 'audio/wav', data: audio}),
      'input.audio.0' => audio,
      'input.attachment.1.data' => 'ordinary', 'input.attachment.0.transcript' => 'spoken words',
      'ordinary' => 'visible'
    }
    media_keys = %w[input.source.data input.attachment.0.data input.attachment.2.bytes.0 input.value input.audio.0]
    visible = attributes.reject { |key, _| media_keys.include?(key) }
    assert_r5_followup_media_export(attributes, [audio], media_keys, visible)
  end

  def test_r5_followup_nonbase64_audio_data_uri_is_scrubbed_in_otlp
    audio = r5_verifier_audio_attributes.fetch('input.audio.0').unpack1('m0')
    payload = audio.bytes.map { |byte| format('%%%02X', byte) }.join
    uri = 'data:audio/wav,' + payload
    literal = "data:audio/basic,synthetic'audio"
    attributes = {'input.value' => 'sound: ' + uri, 'output.value' => JSON.generate(audio: uri), 'input.literal' => literal, 'ordinary' => 'visible'}
    assert_r5_followup_media_export(attributes, [uri, payload, "synthetic'audio"], %w[input.value output.value input.literal], {'ordinary' => 'visible'})
  end
end

class Round5SecretsTest
  def test_r5_followup_connect_observer_preserves_native_writes_and_ignores_nonhandshake_data
    base = Class.new do
      attr_reader :parts
      def initialize(result) = (@result = result)
      def write(*parts)
        @parts = parts
        @result
      end
    end
    base.prepend(A::NetConnectHeaders)
    result = Object.new; writer = base.new(result)
    header = "CONNECT 127.0.0.1:1 HTTP/1.1\r\nProxy-Authorization: Basic synthetic-credential\r\n\r\nAuthorization: must-not-be-observed\r\n"
    observations = []
    previous = Thread.current[:traceai_net_connect]
    P.stub(:observe_request, ->(headers, url) { observations << [headers, url] }) do
      Thread.current[:traceai_net_connect] = nil
      assert_same result, writer.write(header)
      assert_empty observations
      Thread.current[:traceai_net_connect] = true
      assert_same result, writer.write('ordinary body bytes')
      assert_empty observations
      assert_same result, writer.write(header)
      assert_same header, writer.parts.first
      assert_equal [[{'Proxy-Authorization' => 'Basic synthetic-credential'}, '']], observations
    end
    P.stub(:observe_request, ->(*) { raise 'observer failure' }) { assert_same result, writer.write(header) }
    assert_same header, writer.parts.first
  ensure
    Thread.current[:traceai_net_connect] = previous
  end

  def test_r5_followup_connect_scope_restores_fiber_local_marker_after_native_errors
    base = Class.new do
      def initialize(error) = (@error = error)
      def connect = raise(@error)
    end
    base.prepend(A::NetHTTP)
    error = IOError.new('native connect failure')
    client = base.new(error)
    previous = Thread.current[:traceai_net_connect]
    Thread.current[:traceai_net_connect] = :outer
    A.stub(:active?, true) do
      caught = assert_raises(IOError) { client.send(:connect) }
      assert_same error, caught
      assert_equal :outer, Thread.current[:traceai_net_connect]
      Fiber.new do
        assert_nil Thread.current[:traceai_net_connect]
        assert_raises(IOError) { client.send(:connect) }
        assert_nil Thread.current[:traceai_net_connect]
      end.resume
      assert_equal :outer, Thread.current[:traceai_net_connect]
    end
  ensure
    Thread.current[:traceai_net_connect] = previous
  end

  def test_r5_followup_httpx_final_header_observation_is_single_pass_and_fail_open
    base = Class.new do
      attr_reader :serialized
      def initialize(result) = (@result = result)
      def join_headers2(request, headers)
        @serialized = []
        headers.each { |name, value| @serialized << [name, value] }
        @result
      end
    end
    base.prepend(A::HTTPXProtocolHeaders)
    result = Object.new; parser = base.new(result)
    pairs = [['proxy-authorization', 'Basic final-proxy-credential'], ['host', '127.0.0.1']]
    passes = 0
    headers = Object.new
    headers.define_singleton_method(:each) do |&block|
      passes += 1
      raise 'native enumeration repeated' unless passes == 1
      pairs.each(&block)
    end
    A.stub(:active?, true) do
      P.stub(:observe_request, ->(*) { raise 'observer failure' }) do
        assert_same result, parser.send(:join_headers2, Object.new, headers)
      end
    end
    assert_equal 1, passes
    assert_equal pairs, parser.serialized
    native_error = IOError.new('native header enumeration failure')
    failing_headers = Object.new
    failing_headers.define_singleton_method(:each) { |&block| raise native_error }
    A.stub(:active?, true) do
      caught = assert_raises(IOError) { parser.send(:join_headers2, Object.new, failing_headers) }
      assert_same native_error, caught
    end
  end
end

class Round4BoundaryTest
  def test_r5_followup_flattened_mime_index_is_bounded_and_fails_closed
    original = record
    original.attributes = {('attachment.' * 17_000) + 'mimeType' => 'audio/wav', 'input.value' => 'PRIVATE_AUDIO'}
    copy = snapshot(original)
    assert_empty copy.attributes
    assert_equal original.span_id, copy.span_id
    assert_equal original.start_timestamp, copy.start_timestamp
    assert_equal original.end_timestamp, copy.end_timestamp
    refute_includes A::Protobuf.encode([copy, snapshot(record)]), 'PRIVATE_AUDIO'
    assert_equal 'PRIVATE_AUDIO', original.attributes['input.value']
  end

  def test_r5_followup_mime_siblings_remain_scoped_and_cover_every_attribute_surface
    audio = ['RIFF' + "\0" * 40].pack('m0')
    attributes = {
      'input.source.data' => audio, 'input.source.media_type' => 'audio/wav',
      'input.attachment.0.data' => audio, 'input.attachment.0.mimeType' => 'audio/wav',
      'input.attachment.0.extra.data.0' => audio,
      'nested' => {'attachment.0.bytes' => audio, 'attachment.0.contentType' => 'audio/wav'},
      'input.attachment.1.data' => audio, 'ordinary.data' => audio,
      'input.attachment.0.transcript' => 'spoken words'
    }
    original = record
    original.attributes = attributes
    original.events = [OpenTelemetry::SDK::Trace::Event.new('media', attributes, 123)]
    original.links = [OpenTelemetry::Trace::Link.new(OpenTelemetry::Trace::SpanContext.new, attributes)]
    original.resource = A::ExportResource.new(attributes)
    original.instrumentation_scope = A::ExportScope.new('media', '1', attributes)
    copy = snapshot(original)
    [copy.attributes, copy.events.first.attributes, copy.links.first.attributes, copy.resource.attributes, copy.instrumentation_scope.attributes].each do |safe|
      %w[input.source.data input.attachment.0.data input.attachment.0.extra.data.0].each { |key| assert_equal '<omitted: 60 base64 chars>', safe.fetch(key) }
      assert_equal '<omitted: 60 base64 chars>', safe.dig('nested', 'attachment.0.bytes')
      %w[input.attachment.1.data ordinary.data].each { |key| assert_equal audio, safe.fetch(key) }
      assert_equal 'spoken words', safe['input.attachment.0.transcript']
      assert_equal 'audio/wav', safe['input.source.media_type']
    end
    assert_same attributes, original.attributes
    assert_equal audio, original.attributes['input.source.data']
  end

  def test_r5_followup_nonbase64_data_uris_cover_every_exported_string_surface
    payload = '%52%49%46%46%00%00%00%00%57%41%56%45'
    uri = 'data:audio/wav,' + payload
    marker = 'sound ' + uri
    original = record
    original.name = marker
    original.attributes = {'ordinary' => marker, 'array' => [marker], 'nested' => {'plain' => marker}}
    original.status = OpenTelemetry::Trace::Status.error(marker)
    original.events = [OpenTelemetry::SDK::Trace::Event.new(marker, {'ordinary' => marker}, 123)]
    original.links = [OpenTelemetry::Trace::Link.new(OpenTelemetry::Trace::SpanContext.new, {'ordinary' => marker})]
    original.resource = A::ExportResource.new({'ordinary' => marker})
    original.instrumentation_scope = A::ExportScope.new(marker, marker, {'ordinary' => marker})
    original.tracestate = 'vendor=' + uri
    copy = snapshot(original)
    refute_includes A::Protobuf.encode([copy]), payload
    assert_includes copy.name, '<omitted:'
    assert_includes copy.status.description, '<omitted:'
    assert_includes copy.events.first.name, '<omitted:'
    assert_includes copy.instrumentation_scope.name, '<omitted:'
    assert_includes copy.instrumentation_scope.version, '<omitted:'
    assert_includes copy.tracestate, '<omitted:'
    [copy.attributes, copy.events.first.attributes, copy.links.first.attributes, copy.resource.attributes, copy.instrumentation_scope.attributes].each do |safe|
      assert_includes safe.fetch('ordinary'), '<omitted:'
      refute_includes A::Codec.generate(safe), payload
    end
    assert_equal marker, original.name
    assert_equal marker, original.events.first.name
    assert_equal marker, original.attributes['ordinary']
  end
end
