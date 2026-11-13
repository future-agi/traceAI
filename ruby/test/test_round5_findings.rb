# frozen_string_literal: true
require_relative 'test_round5'

class EndToEndTest
  def r5_verifier_header_echo_code(transport, header, credential, proxy: false)
    <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'httpx'
      unless OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
        OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new
      end
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      before = tracer.start_span('header-span-started-before-request', with_parent: OpenTelemetry::Context.empty)
      failure = nil
      tracer.in_span('application-records-header-error') do |application|
        begin
          tracer.in_span('covered-header-library-call', attributes: {'gen_ai.span.kind' => 'LLM'}) do
            tracer.in_span('header-native-intermediate') do
              if #{(transport == 'net_http').inspect}
                request = Net::HTTP::Post.new('/v1/chat/completions')
                request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[]}'
                if #{proxy.inspect}
                  client = Net::HTTP::Proxy('127.0.0.1', #{@provider.port}, 'r5-proxy-user', 'r5-proxy-password').new('127.0.0.1', 1)
                else
                  client = Net::HTTP.new('127.0.0.1', #{@provider.port}, nil)
                  request[#{header.inspect}] = #{credential.inspect}
                end
                response = client.request(request)
                output = response.body; status = response.code.to_i
              else
                response = HTTPX.post('#{@provider.url}/v1/chat/completions', headers: {#{header.inspect} => #{credential.inspect}}, json: {model: 'm', messages: []})
                output = response.to_s; status = response.status
              end
              abort 'status changed' unless status == 401
              raise RuntimeError, output
            end
          end
        rescue RuntimeError => e
          failure = e
          abort 'error changed' unless e.message.include?(#{credential.inspect})
          application.set_attribute('ordinary', e.message)
          application.status = OpenTelemetry::Trace::Status.error(e.message)
          application.record_exception(e)
          before.set_attribute('ordinary', e.message)
          before.record_exception(e)
        end
      end
      before.finish
      tracer.in_span('unrelated-header-error') do |span|
        span.set_attribute('ordinary', failure.message)
        span.record_exception(failure)
      end
      original = memory.finished_spans.find { |span| span.name == 'application-records-header-error' }
      if defined?(TraceAI::Auto)
        TraceAI::Auto.flush
        abort 'other exporter mutated' unless original.attributes['ordinary'].include?(#{credential.inspect})
        abort 'exception mutated' unless original.events.any? { |event| event.attributes['exception.message'].include?(#{credential.inspect}) }
      end
      print failure.class.name + '|' + failure.message
    RUBY
  end

  def assert_r5_verifier_header_echo(transport, header, credential, proxy: false)
    @provider.stop
    @provider = MockHTTP.new do |request|
      [401, {'Content-Type' => 'application/json'}, JSON.generate(error: {message: 'provider echoes ' + request[:headers].fetch(header)})]
    end
    code = r5_verifier_header_echo_code(transport, header, credential, proxy: proxy)
    baseline = nil
    [nil, {}, {'FI_WIRE_CAPTURE' => 'false'}].each do |flags|
      stdout, stderr, status = app(code, flags || {'RUBYOPT' => nil})
      assert status.success?, stderr
      assert_includes stdout, credential
      baseline ||= stdout
      assert_equal baseline, stdout
      assert_equal credential, @provider.requests.pop(true)[:headers][header]
      assert_empty @provider.requests
      spans = export_spans.map(&:first)
      unless flags
        assert_empty spans
        next
      end
      assert_equal 5, spans.length
      assert spans.none? { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
      refute_includes spans.map(&:to_h).to_s, credential
      %w[application-records-header-error header-span-started-before-request unrelated-header-error].each do |name|
        span = spans.find { |candidate| candidate.name == name }
        refute_nil span, name
        assert_includes attrs(span.attributes).fetch('ordinary'), '<redacted>'
        assert span.events.any? { |event| attrs(event.attributes)['exception.message'].include?('<redacted>') }
      end
      application = spans.find { |span| span.name == 'application-records-header-error' }
      assert_equal :STATUS_CODE_ERROR, application.status.code
      assert_includes application.status.message, '<redacted>'
    end
  end

  def test_r5_verifier_aws_session_token_headers_are_observed_by_both_covered_transports
    %w[net_http httpx].each do |transport|
      assert_r5_verifier_header_echo(transport, 'x-amz-security-token', "r5-#{transport}-aws-session-credential-12345678")
    end
  end

  def test_r5_verifier_native_net_http_proxy_authorization_is_observed_when_covered
    credential = 'Basic ' + ['r5-proxy-user:r5-proxy-password'].pack('m0')
    assert_r5_verifier_header_echo('net_http', 'proxy-authorization', credential, proxy: true)
  end

  def r5_verifier_audio_attributes
    samples = [1, 300].map do |frames|
      pcm = "\0\0" * frames
      wav = 'RIFF' + [36 + pcm.bytesize].pack('V') + 'WAVEfmt ' + [16].pack('V') + [1, 1, 8000, 16_000, 2, 16].pack('vvVVvv') + 'data' + [pcm.bytesize].pack('V') + pcm
      [wav].pack('m0')
    end
    short, long = samples
    {
      'input.audio.0' => short,
      'input.audio.1' => long,
      'input.value' => JSON.generate(input_audio: {data: short, format: 'wav'}),
      'gen_ai.input.messages' => JSON.generate([{role: 'user', content: [{type: 'input_audio', input_audio: {data: short, format: 'wav'}}]}]),
      'input.inlineData.data' => short,
      'input.inlineData.mimeType' => 'audio/wav',
      'input.audio.transcript' => 'transcript',
      'ordinary.data' => 'ordinary',
      'ordinary' => 'visible text'
    }
  end

  def test_r5_verifier_structural_short_and_indexed_audio_is_scrubbed_in_actual_otlp
    attributes = r5_verifier_audio_attributes
    code = <<~RUBY
      require 'opentelemetry/sdk'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider.tracer('app').in_span('structural-audio', attributes: #{attributes.inspect}) { |span| span.add_event('audio', attributes: #{attributes.inspect}) }
      original = memory.finished_spans.first
      TraceAI::Auto.flush
      abort 'other exporter mutated' unless original.attributes == #{attributes.inspect}
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert_equal 1, spans.length
    span = spans.first
    %w[input.audio.0 input.audio.1].each { |key| refute_includes span.to_h.to_s, attributes.fetch(key) }
    [attrs(span.attributes), attrs(span.events.first.attributes)].each do |safe|
      %w[input.audio.0 input.audio.1 input.value gen_ai.input.messages input.inlineData.data].each do |key|
        assert_includes safe.fetch(key), '<omitted:', key
      end
      assert_equal 'audio/wav', safe['input.inlineData.mimeType']
      assert_equal 'transcript', safe['input.audio.transcript']
      assert_equal 'ordinary', safe['ordinary.data']
      assert_equal 'visible text', safe['ordinary']
    end
  end
end

class Round4BoundaryTest
  def test_r5_verifier_structural_audio_scrubbing_covers_nested_arrays_and_all_attribute_surfaces
    audio = ['RIFF' + "\0" * 40].pack('m0')
    attributes = {
      'input.audio.0' => audio,
      'input_audio' => {'data' => audio},
      'audio' => [audio],
      'input.inlineData.data' => audio,
      'input.inlineData.mimeType' => 'audio/wav',
      'nested' => {'attachment' => {'mimeType' => 'audio/wav', 'data' => audio}},
      'ordinary' => 'still visible'
    }
    original = record
    original.attributes = attributes
    original.events = [OpenTelemetry::SDK::Trace::Event.new('audio', attributes, 123)]
    original.links = [OpenTelemetry::Trace::Link.new(OpenTelemetry::Trace::SpanContext.new, attributes)]
    original.resource = A::ExportResource.new(attributes)
    original.instrumentation_scope = A::ExportScope.new('audio', '1', attributes)
    copy = snapshot(original)
    [copy.attributes, copy.events.first.attributes, copy.links.first.attributes, copy.resource.attributes, copy.instrumentation_scope.attributes].each do |safe|
      refute_includes A::Codec.generate(safe), audio
      assert_equal '<omitted: 60 base64 chars>', safe['input.audio.0']
      assert_equal 'still visible', safe['ordinary']
      assert_equal 'audio/wav', safe['input.inlineData.mimeType']
      assert_equal '<omitted: 60 base64 chars>', safe.dig('nested', 'attachment', 'data')
    end
    assert_same attributes, original.attributes
    assert_equal audio, original.attributes['input.audio.0']
  end
end
