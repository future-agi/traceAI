# frozen_string_literal: true
require_relative 'test_e2e'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/export'
require_relative '../lib/traceai/auto/wire'

class Round6FollowupTest < Minitest::Test
  A = TraceAI::Auto
  ANTHROPIC_EVENTS = [
    {type: 'message_start', message: {id: 'msg-ndjson', model: 'm', usage: {input_tokens: 5, cache_read_input_tokens: 3}}},
    {type: 'content_block_start', index: 0, content_block: {type: 'text', text: ''}},
    {type: 'content_block_delta', index: 0, delta: {type: 'text_delta', text: 'héllo'}},
    {type: 'message_delta', usage: {output_tokens: 1}},
    {type: 'message_delta', delta: {stop_reason: 'end_turn'}, usage: {output_tokens: 2}},
    {type: 'message_delta', usage: {output_tokens: 2}}
  ].freeze

  def test_anthropic_ndjson_merges_usage_only_from_complete_observed_frames
    chunks = ANTHROPIC_EVENTS.map { |event| JSON.generate(event) + "\n" }
    chunks << JSON.generate(type: 'message_delta', usage: {input_tokens: 999, output_tokens: 999}) # No newline: unobserved frame.
    bytes = chunks.join.b
    request = {method: 'POST', url: 'http://127.0.0.1:1234/v1/messages', headers: {'content-type' => 'application/json'},
               body: JSON.generate(model: 'm', messages: [], stream: true)}
    size = 0
    arrivals = chunks.each_with_index.map { |chunk, index| [size += chunk.bytesize, (index + 1) / 100.0] }
    result = A::Wire.build(request, {status: 200, headers: {'content-type' => 'application/x-ndjson'}, body: bytes, size: bytes.bytesize,
                                    arrivals: arrivals, first_chunk_seconds: 0.01, error: IOError.new('interrupted line')})
    refute_nil result
    values = result.attributes
    assert_equal 'héllo', values['output.value']
    assert_equal 'msg-ndjson', values['gen_ai.response.id']
    assert_equal 'm', values['gen_ai.response.model']
    assert_equal ['end_turn'], values['gen_ai.response.finish_reasons']
    assert_equal [8, 2, 10, 3], %w[input_tokens output_tokens total_tokens cache_read_tokens].map { |key| values['gen_ai.usage.' + key] }
    assert_equal 0.01, values['gen_ai.response.time_to_first_chunk']
    assert_equal 0.03, values['gen_ai.response.time_to_first_text']
    refute result.ok
  end

  def test_sanitizer_failure_recovers_identity_independently_and_fails_closed_without_registry
    host = OpenTelemetry::SDK::Trace::TracerProvider.new
    span = host.tracer('app').start_span('safe-name SECRET_IDENTITY_12345678')
    span.finish
    original = span.to_span_data
    original.attributes = {'output.value' => '{partial model JSON', 'gen_ai.usage.input_tokens' => 7}
    original.resource = A::ExportResource.new({'service.name' => 'checkout', 'custom.identity' => 'keep-custom',
      'gen_ai.tool' => 'PRIVATE_TOOL', 'gen_ai.request' => 'PRIVATE_PARAMETERS', 'gen_ai.audio.transcript.text' => 'PRIVATE_TRANSCRIPT'})
    config = A::Config.new({}, env: {})
    routing = A::ExportResource.new({'project_name' => 'separate-project'})
    snapshot = -> { A::Privacy.snapshot(original, routing, config, ['SECRET_IDENTITY_12345678']) }
    copy = snapshot.call
    assert_equal 'safe-name <redacted>', copy.name
    assert_equal({'service.name' => 'checkout', 'custom.identity' => 'keep-custom', 'project_name' => 'separate-project'}, copy.resource.attributes)
    assert_equal({'gen_ai.usage.input_tokens' => 7}, copy.attributes)
    assert_equal original.span_id, copy.span_id
    assert_equal original.start_timestamp, copy.start_timestamp
    assert_equal original.end_timestamp, copy.end_timestamp

    original.name = '{malformed name'
    copy = snapshot.call
    assert_equal '', copy.name
    assert_equal 'checkout', copy.resource.attributes['service.name']
    original.name = 'x' * (A::Privacy::CATALOG['limits']['max_text_chars'] + 1)
    copy = snapshot.call
    assert_equal '', copy.name
    assert_equal 'checkout', copy.resource.attributes['service.name']
    original.name = 'safe-name'
    original.resource = A::ExportResource.new({'service.name' => '{malformed identity'})
    copy = snapshot.call
    assert_equal 'safe-name', copy.name
    assert_empty copy.resource.attributes

    original.resource = A::ExportResource.new({'service.name' => 'PRIVATE_IDENTITY'})
    A::SECRET_REGISTRY.stub(:snapshot, -> { raise A::SecretRegistry::Unavailable }) do
      copy = snapshot.call
      assert_equal '', copy.name
      assert_empty copy.resource.attributes
      assert_equal({'gen_ai.usage.input_tokens' => 7}, copy.attributes)
    end
    assert_equal 'PRIVATE_IDENTITY', original.resource.attributes['service.name']
    assert_equal '{partial model JSON', original.attributes['output.value']
  end

  def test_r6_sanitizer_fallback_scrubs_known_credentials_in_protected_keys
    secrets = %w[wireObserved wireObserved783421 fixture-api-12345678]
    values = {'output.value' => '{partial JSON', 'gen_ai.usage.input_tokens' => 7,
              'traceai.capture.dropped_content_count' => 3,
              'gen_ai.usage.wireObserved783421.numeric' => 8,
              'gen_ai.usage.fixture-api-12345678.boolean' => true,
              'gen_ai.usage.wireObserved783421.string' => 'ordinary metric',
              'gen_ai.usage.fixture-api-12345678.array' => ['ordinary metric'],
              'gen_ai.usage.wireObserved783421.float' => Float::INFINITY}
    expected = {'gen_ai.usage.input_tokens' => 7, 'traceai.capture.dropped_content_count' => 3,
                'gen_ai.usage.<redacted>.numeric' => 8, 'gen_ai.usage.<redacted>.boolean' => true,
                'gen_ai.usage.<redacted>.string' => 'ordinary metric',
                'gen_ai.usage.<redacted>.array' => ['ordinary metric'],
                'gen_ai.usage.<redacted>.float' => 'Infinity'}
    original = Marshal.dump(values)
    A.stub(:secret_values, secrets) do
      [values, values.to_a.reverse.to_h].each do |attributes|
        assert_equal expected, A::Privacy.scrub_attributes(attributes)
        assert_equal expected, A::Privacy.safe_metadata(attributes)
      end
    end
    A.stub(:secret_values, []) do
      assert_equal expected, A::Privacy.safe_metadata(values, secrets)
    end
    assert_equal original, Marshal.dump(values)
  end

  def test_r6_sanitizer_fallback_drops_unverifiable_protected_keys_without_registry
    values = {'output.value' => '{partial JSON', 'gen_ai.usage.input_tokens' => 7,
              'gen_ai.usage.output_tokens.reasoning' => 2, 'traceai.capture.dropped_content_count' => 3,
              'traceai.wire.incomplete' => true, 'gen_ai.usage.wireObserved783421' => 8,
              'gen_ai.usage.fixture-api-12345678' => false, 'gen_ai.usage.custom' => 9,
              'gen_ai.request.model' => 'PRIVATE_MODEL'}
    expected = {'gen_ai.usage.input_tokens' => 7, 'gen_ai.usage.output_tokens.reasoning' => 2,
                'traceai.capture.dropped_content_count' => 3, 'traceai.wire.incomplete' => true}
    A::SECRET_REGISTRY.stub(:snapshot, -> { raise A::SecretRegistry::Unavailable }) do
      assert_equal expected, A::Privacy.scrub_attributes(values)
      assert_equal expected, A::Privacy.safe_metadata(values, ['fixture-api-12345678'])
    end
    assert_equal 8, values['gen_ai.usage.wireObserved783421']
  end

  def test_transcript_family_side_hiding_includes_dotted_descendants
    values = {'gen_ai.audio.transcript' => 'PRIVATE_BASE_TRANSCRIPT',
              'gen_ai.audio.transcript.text' => 'PRIVATE_NESTED_TRANSCRIPT',
              'gen_ai.audio.transcript.0' => 'PRIVATE_INDEXED_TRANSCRIPT',
              'gen_ai.audio.transcript.parts.0.text' => ['PRIVATE_ARRAY_TRANSCRIPT'],
              'gen_ai.audio.transcript_id' => 'safe-id'}
    %w[speech_to_text text_to_speech].each do |operation|
      %w[hide_inputs hide_input_text hide_outputs hide_output_text].each do |flag|
        config = A::Config.new({flag => true}, env: {})
        attrs = values.merge('gen_ai.operation.name' => operation)
        result = A::Privacy.mask(attrs, config)
        hidden = operation == 'text_to_speech' ? flag.start_with?('hide_input') : flag.start_with?('hide_output')
        values.each do |key, value|
          expected = hidden && key != 'gen_ai.audio.transcript_id' ? '__REDACTED__' : value
          assert_equal expected, result[key], "#{operation}/#{flag}/#{key}"
        end
        assert_equal values, attrs.reject { |key, _| key == 'gen_ai.operation.name' }
      end
    end
  end
end

class EndToEndTest
  def test_r6_anthropic_ndjson_and_sse_capture_match_without_changing_application_bytes
    %w[application/x-ndjson text/event-stream].each do |content_type|
      chunks = Round6FollowupTest::ANTHROPIC_EVENTS.map do |event|
        content_type == 'text/event-stream' ? "data: #{JSON.generate(event)}\n\n" : JSON.generate(event) + "\n"
      end
      @provider.stop
      @provider = MockHTTP.new { |_request| [200, {'content-type' => content_type}, chunks] }
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'net/http'; require 'json'
        if ENV['TEST_AGENT'] == 'true'
          require 'traceai/auto/agent'
          TraceAI::Auto.init
        end
        request = Net::HTTP::Post.new('/v1/messages')
        request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[],"stream":true}'
        body = +''
        response = Net::HTTP.start('127.0.0.1', #{@provider.port}) do |client|
          client.request(request) { |result| result.read_body { |chunk| body << chunk } }
        end
        abort 'flush failed' if ENV['TEST_AGENT'] == 'true' && !TraceAI::Auto.flush
        puts JSON.generate([response.class.name, response.code, body.unpack1('H*')])
      RUBY
      original = nil
      [false, true].each do |enabled|
        stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_AGENT' => enabled.to_s})
        assert status.success?, stderr
        original ||= stdout
        assert_equal original, stdout
        assert_equal chunks.join.unpack1('H*'), JSON.parse(stdout).last
        spans = export_spans
        unless enabled
          assert_empty spans
          next
        end
        assert_equal 1, spans.size
        values = attrs(spans.first.first.attributes)
        assert_equal 'héllo', values['output.value']
        assert_equal 'msg-ndjson', values['gen_ai.response.id']
        assert_equal 'm', values['gen_ai.response.model']
        assert_equal ['end_turn'], values['gen_ai.response.finish_reasons']
        assert_equal [8, 2, 10, 3], %w[input_tokens output_tokens total_tokens cache_read_tokens].map { |key| values['gen_ai.usage.' + key] }
        assert_equal true, values['gen_ai.request.stream']
        assert_operator values.fetch('gen_ai.response.time_to_first_text'), :>=, values.fetch('gen_ai.response.time_to_first_chunk')
      end
    end
  end

  def test_r6_real_gzip_arrivals_use_discovered_native_inflater_before_decode
    payload = "data: #{JSON.generate(choices: [{delta: {content: 'native gzip output'}}])}\n\n"
    io = StringIO.new; gzip = Zlib::GzipWriter.new(io); gzip.write(payload); gzip.close
    chunks = [io.string.byteslice(0, 10), io.string.byteslice(10..)]
    @provider.stop
    @provider = MockHTTP.new { |_request| [200, {'content-type' => 'text/event-stream', 'content-encoding' => 'gzip'}, chunks] }
    %w[preloaded lazy].each do |load_order|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'json'
        require 'net/http' if #{load_order.inspect} == 'preloaded'
        if ENV['TEST_AGENT'] == 'true'
          require 'traceai/auto/agent'
          TraceAI::Auto.init
        end
        require 'net/http'
        # Control real socket-delivery callbacks, not the agent's hook. The gzip
        # header emits no text; inflation advances the clock by five seconds.
        Process.singleton_class.prepend(Module.new do
          def clock_gettime(clock, *args)
            controlled = Thread.current[:test_arrival_clock]
            controlled && clock == Process::CLOCK_MONOTONIC ? controlled[:now] : super
          end
        end)
        Net::BufferedIO.prepend(Module.new do
          def read(length, destination = ''.b, ignore_eof = false)
            controlled = Thread.current[:test_arrival_clock]
            if controlled && (destination.is_a?(Net::ReadAdapter) || destination.class.name == 'TraceAI::Auto::NetInflateArrival')
              forward = Net::ReadAdapter.new(->(bytes) do
                unless bytes.empty?
                  controlled[:now] = [100.01, 100.07].fetch(controlled[:raw].length)
                  controlled[:raw] << (controlled[:now] - 100.0).round(6)
                end
                destination << bytes
              end)
              super(length, forward, ignore_eof)
            else
              super
            end
          end
        end)
        Zlib::Inflate.prepend(Module.new do
          def inflate(*args, &block)
            controlled = Thread.current[:test_arrival_clock]
            controlled[:now] += 5.0 if controlled
            super
          end
        end)
        controlled = {now: 100.0, raw: []}
        Thread.current[:test_arrival_clock] = controlled
        request = Net::HTTP::Post.new('/v1/chat/completions')
        request['content-type'] = 'application/json'; request.body = '{"model":"m","messages":[],"stream":true}'
        response = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(request) }
        Thread.current[:test_arrival_clock] = nil
        abort 'flush failed' if ENV['TEST_AGENT'] == 'true' && !TraceAI::Auto.flush
        installed = defined?(TraceAI::Auto::NetInflater) && Net::HTTPResponse::Inflater.ancestors.include?(TraceAI::Auto::NetInflater)
        puts JSON.generate([response.class.name, response.code, response.body, controlled[:raw], installed])
      RUBY
      original = nil
      [false, true].each do |enabled|
        stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_AGENT' => enabled.to_s})
        assert status.success?, stderr
        result = JSON.parse(stdout)
        original ||= result.first(4)
        assert_equal original, result.first(4), "#{load_order}: application bytes changed"
        assert_equal payload, result[2]
        assert_equal [0.01, 0.07], result[3]
        spans = export_spans
        unless enabled
          assert_empty spans
          next
        end
        assert_equal 1, spans.size
        values = attrs(spans.first.first.attributes)
        assert_equal 0.01, values['gen_ai.response.time_to_first_chunk'], load_order
        assert_equal 0.07, values['gen_ai.response.time_to_first_text'], load_order
        assert_equal true, result.last, 'native hook was not discovered'
      end
    end
  end

  def test_r6_sanitizer_failure_preserves_safe_resource_and_name_in_actual_otlp
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'json'
      resource = OpenTelemetry::SDK::Resources::Resource.create(
        'service.name' => 'checkout-verifier', 'service.namespace' => 'commerce',
        'custom.identity' => 'keep-custom', 'ordinary' => 'echo resource-secret-12345678',
        'parts.0.mime_type' => 'audio/pcm', 'parts.0.content.chunk' => 'AQIDBA==',
        'output.value' => 'PRIVATE_RESOURCE_CONTENT')
      host = OpenTelemetry::SDK::Trace::TracerProvider.new(resource: resource)
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      if ENV['TEST_AGENT'] == 'true'
        require 'traceai/auto/agent'
        TraceAI::Auto.init(hide_outputs: true)
      end
      ['safe text', '{partial model JSON'].each do |text|
        host.tracer('app').in_span('safe-name resource-secret-12345678', attributes: {'output.value' => text, 'gen_ai.usage.input_tokens' => 7}) {}
      end
      abort 'flush failed' if ENV['TEST_AGENT'] == 'true' && !TraceAI::Auto.flush
      puts JSON.generate(memory.finished_spans.map { |span| [span.name, span.attributes, span.resource.instance_variable_get(:@attributes)] })
    RUBY
    original = nil
    [false, true].each do |enabled|
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_AGENT' => enabled.to_s, 'RESOURCE_TOKEN' => 'resource-secret-12345678'})
      assert status.success?, stderr
      original ||= stdout
      assert_equal original, stdout
      spans = export_spans
      unless enabled
        assert_empty spans
        next
      end
      assert_equal 2, spans.size
      spans.each do |span, resource|
        assert_equal 'safe-name <redacted>', span.name
        assert_equal 'checkout-verifier', resource['service.name']
        assert_equal 'commerce', resource['service.namespace']
        assert_equal 'keep-custom', resource['custom.identity']
        assert_equal 'ruby-e2e', resource['project_name']
        assert_equal 'echo <redacted>', resource['ordinary']
        assert_equal '<omitted: 8 base64 chars>', resource['parts.0.content.chunk']
        assert_equal 7, attrs(span.attributes)['gen_ai.usage.input_tokens']
        refute_includes resource.to_s, 'PRIVATE_RESOURCE_CONTENT'
        refute_includes span.to_h.to_s, 'resource-secret-12345678'
      end
      refute attrs(spans.last.first.attributes).key?('output.value')
      refute spans.last.last.key?('output.value')
    end
  end

  def test_r6_known_credential_in_protected_keys_is_scrubbed_in_fallback_otlp_without_host_mutation
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'json'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      if ENV['TEST_AGENT'] == 'true'
        require 'traceai/auto/agent'
        TraceAI::Auto.init
      end
      request = Net::HTTP::Get.new('/credential-observation')
      request['x-access-token'] = 'wireObserved783421'
      response = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(request) }
      ['ordinary output', '{partial JSON'].each_with_index do |text, index|
        host.tracer('app').in_span('credential-key-' + index.to_s, attributes: {
          'gen_ai.usage.wireObserved783421' => 7,
          'gen_ai.usage.fixture-api-12345678' => 8,
          'gen_ai.usage.input_tokens' => 9,
          'output.value' => text}) {}
      end
      abort 'flush failed' if ENV['TEST_AGENT'] == 'true' && !TraceAI::Auto.flush
      puts JSON.generate([response.class.name, response.code, response.body,
        memory.finished_spans.map { |span| [span.name, span.attributes] }])
    RUBY
    original = nil
    [false, true].each do |enabled|
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_AGENT' => enabled.to_s})
      assert status.success?, stderr
      original ||= stdout
      assert_equal original, stdout, 'application response or host exporter records changed'
      assert_equal 1, @provider.requests.size
      request = @provider.requests.pop(true)
      assert_equal 'wireObserved783421', request[:headers]['x-access-token']
      spans = export_spans.map(&:first)
      unless enabled
        assert_empty spans
        next
      end
      assert_equal %w[credential-key-0 credential-key-1], spans.map(&:name)
      spans.each do |span|
        values = attrs(span.attributes)
        assert_equal 8, values['gen_ai.usage.<redacted>']
        assert_equal 9, values['gen_ai.usage.input_tokens']
        refute_includes span.to_h.to_s, 'wireObserved783421'
        refute_includes span.to_h.to_s, 'fixture-api-12345678'
      end
      assert_equal 'ordinary output', attrs(spans.first.attributes)['output.value']
      refute attrs(spans.last.attributes).key?('output.value')
    end
  end

  def test_r6_transcript_descendants_are_hidden_in_actual_otlp_without_mutating_host
    %w[speech_to_text text_to_speech].each do |operation|
      flags = operation == 'text_to_speech' ? %w[hide_inputs hide_input_text] : %w[hide_outputs hide_output_text]
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'json'
        host = OpenTelemetry::SDK::Trace::TracerProvider.new
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
        OpenTelemetry.tracer_provider = host
        if ENV['TEST_HIDE_FLAG']
          require 'traceai/auto/agent'
          TraceAI::Auto.init(**{ENV.fetch('TEST_HIDE_FLAG').to_sym => true})
        end
        content = {'gen_ai.audio.transcript' => 'PRIVATE_BASE_TRANSCRIPT',
                   'gen_ai.audio.transcript.text' => 'PRIVATE_NESTED_TRANSCRIPT',
                   'gen_ai.audio.transcript.0' => 'PRIVATE_INDEXED_TRANSCRIPT',
                   'gen_ai.audio.transcript.parts.0.text' => ['PRIVATE_ARRAY_TRANSCRIPT']}
        host.tracer('app').in_span('transcript-family', attributes: content.merge('gen_ai.operation.name' => #{operation.inspect})) do |span|
          span.add_event('transcript-event', attributes: content)
        end
        abort 'flush failed' if ENV['TEST_HIDE_FLAG'] && !TraceAI::Auto.flush
        record = memory.finished_spans.first
        puts JSON.generate([record.name, record.attributes, record.events.map { |event| [event.name, event.attributes] }])
      RUBY
      baseline, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_HIDE_FLAG' => nil})
      assert status.success?, stderr
      assert_empty export_spans
      flags.each do |flag|
        stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'TEST_HIDE_FLAG' => flag})
        assert status.success?, stderr
        assert_equal baseline, stdout, "host data changed with #{flag}"
        spans = export_spans
        assert_equal 1, spans.size
        span = spans.first.first
        [attrs(span.attributes), attrs(span.events.first.attributes)].each do |values|
          %w[gen_ai.audio.transcript gen_ai.audio.transcript.text gen_ai.audio.transcript.0 gen_ai.audio.transcript.parts.0.text].each do |key|
            assert_equal '__REDACTED__', values[key], "#{operation}/#{flag}/#{key}"
          end
        end
        refute_includes span.to_h.to_s, 'PRIVATE_'
      end
    end
  end
end
