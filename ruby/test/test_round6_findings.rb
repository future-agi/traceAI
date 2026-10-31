# frozen_string_literal: true
require_relative 'test_round6_privacy'
require_relative '../lib/traceai/auto/projection'

class Round6FindingsTest < Minitest::Test
  A = TraceAI::Auto
  P = A::AttributeProjection

  def candidates
    values = {'gen_ai.span.kind' => 'LLM', 'gen_ai.operation.name' => 'chat', 'gen_ai.provider.name' => 'openai_compatible',
              'gen_ai.request.model' => 'm', 'gen_ai.response.model' => 'm', 'gen_ai.response.id' => 'response-id',
              'http.response.status_code' => 200, 'error.type' => 'IOError', 'gen_ai.response.finish_reasons' => ['stop'],
              'gen_ai.usage.input_tokens' => 80, 'gen_ai.usage.output_tokens' => 1, 'gen_ai.usage.total_tokens' => 81,
              'traceai.capture.layer' => 'wire', 'traceai.wire.endpoint' => 'chat', 'traceai.wire.incomplete' => true,
              'traceai.wire.truncated' => true, 'traceai.wire.body_unavailable' => true, 'traceai.wire.undecoded_encoding' => 'br',
              'server.address' => 'localhost', 'server.port' => 1234, 'http.request.method' => 'POST',
              'output.value' => 'answer', 'output.mime_type' => 'text/plain', 'input.value' => '{"messages":["old"]}', 'input.mime_type' => 'application/json'}
    80.times do |index|
      values["gen_ai.input.messages.#{index}.message.role"] = 'user'
      values["gen_ai.input.messages.#{index}.message.content"] = "question-#{index}"
    end
    values['gen_ai.output.messages.0.message.role'] = 'assistant'
    values['gen_ai.output.messages.0.message.content'] = 'answer'
    values
  end

  def test_a7_projection_reserves_protected_fields_output_and_newest_atomic_inputs
    original = candidates
    [128, 32, 16, 8, 1, 0].each do |limit|
      result = P.project(original, limit)
      selected = result.attributes
      assert_operator selected.size, :<=, limit
      assert_equal selected.to_a, P.project(original.to_a.reverse.to_h, limit).attributes.to_a
      assert_equal selected, P.project(selected, limit).attributes
      assert_equal original.keys.count { |key| !selected.key?(key) }, result.dropped
      content = original.keys.select { |key| !P.protected_family(key) && P.content_group(key, 'chat') }
      assert_equal content.count { |key| !selected.key?(key) }, selected[P::COUNT] if limit.positive?
      indices = selected.keys.filter_map { |key| /\Agen_ai\.input\.messages\.(\d+)\.message\.role\z/.match(key)&.[](1)&.to_i }.sort
      assert_equal((indices.empty? ? [] : (indices.first..79).to_a), indices)
      indices.each { |index| assert selected.key?("gen_ai.input.messages.#{index}.message.content") }
      refute selected.key?('input.value')
      if limit >= 32
        original.each_key { |key| assert_equal original[key], selected[key], key if P.protected_family(key) }
        assert_equal 'answer', selected['output.value']
        assert_equal 'text/plain', selected['output.mime_type']
      else
        refute selected.keys.any? { |key| content.include?(key) }
      end
    end
    assert_operator P.zero_limit_spans, :>, 0
    assert_equal 0, original.fetch('input.value').index('{') # Candidate map is unmodified.
  end

  def test_a7_projection_atomic_tool_groups_output_precedence_and_no_suffix_gaps
    values = {'gen_ai.input.messages.3.message.role' => 'user', 'gen_ai.input.messages.3.message.content' => 'newest',
              'gen_ai.input.messages.2.message.role' => 'user', 'gen_ai.input.messages.2.message.content' => 'middle',
              'gen_ai.input.messages.2.message.tool_calls.0.id' => 'tool-id',
              'gen_ai.input.messages.1.message.role' => 'user', 'gen_ai.input.messages.1.message.content' => 'oldest',
              'input.value' => 'aggregate', 'input.mime_type' => 'text/plain'}
    selected = P.project(values, 5).attributes
    assert_equal [P::COUNT, 'gen_ai.input.messages.3.message.content', 'gen_ai.input.messages.3.message.role'], selected.keys
    assert_equal 7, selected[P::COUNT]
    output = {'gen_ai.output.messages.10.message.role' => 'assistant', 'gen_ai.output.messages.10.message.content' => 'ten',
              'gen_ai.output.messages.2.message.role' => 'assistant', 'gen_ai.output.messages.2.message.content' => 'two',
              'gen_ai.output.messages.2.message.tool_calls.0.id' => 'call', 'output.value' => 'aggregate', 'output.mime_type' => 'text/plain'}
    selected = P.project(values.merge(output), 5).attributes
    assert_equal [P::COUNT] + output.keys.grep(/messages\.2\./).sort, selected.keys
    refute selected.keys.any? { |key| key.start_with?('gen_ai.input.', 'input.') }
    with_count = candidates.merge(P::COUNT => 9)
    assert_equal 9 + P.project(candidates, 32).attributes[P::COUNT], P.project(with_count, 32).attributes[P::COUNT]
    all = P.project(candidates, 1000).attributes
    refute all.key?(P::COUNT)
    assert_equal candidates, all
  end

  def test_a7_flattened_media_context_is_atomic_at_every_budget
    [
      {'output.parts.0.mime_type' => 'audio/pcm', 'output.parts.0.content.chunk' => 'AQIDBA=='},
      {'output.parts[0].mimeType.0' => 'image/raw', 'output.parts[0].data.source.0' => 'AQIDBA=='},
      {'input.parts.2.type' => 'input_audio', 'input.parts.2.content.chunk' => 'AQIDBA=='}
    ].each do |values|
      sanitized = A::Privacy.scrub_attributes(values.merge('input.zz.value' => 'lower priority'))
      (1..4).each do |limit|
        result = P.project(sanitized, limit)
        retained = values.keys.count { |key| result.attributes.key?(key) }
        assert_includes [0, 2], retained, "context separated at #{limit}: #{result.attributes}"
        assert_equal result.attributes, P.project(sanitized.to_a.reverse.to_h, limit).attributes
        assert_equal sanitized.keys.count { |key| !result.attributes.key?(key) }, result.attributes.fetch(P::COUNT, 0)
        refute_includes A::Codec.generate(result.attributes), 'AQIDBA=='
        assert_equal result.attributes, A::Privacy.scrub_attributes(result.attributes)
      end
    end
  end

  def test_a7_dropped_count_and_numeric_usage_survive_sanitizer_budget_exhaustion
    record = Round4BoundaryTest.new('unused').record
    record.attributes = {'gen_ai.response.id' => 'x' * (A::Privacy::CATALOG['limits']['max_text_chars'] + 1),
                         'output.value' => 'PRIVATE', P::COUNT => 17, 'gen_ai.usage.input_tokens' => 9}
    record.total_recorded_attributes = 40
    copy = A::Privacy.snapshot(record, A::ExportResource.new({}), A::Config.new({}, env: {}))
    assert_equal 17, copy.attributes[P::COUNT]
    assert_equal 9, copy.attributes['gen_ai.usage.input_tokens']
    assert_equal 40, copy.total_recorded_attributes
    refute copy.attributes.key?('output.value')
    assert_equal 'PRIVATE', record.attributes['output.value']
    proto = Opentelemetry::Proto::Collector::Trace::V1::ExportTraceServiceRequest.decode(A::Protobuf.encode([copy]))
    assert_equal 40 - copy.attributes.size, proto.resource_spans.first.scope_spans.first.spans.first.dropped_attributes_count
  end

  def test_a7_snapshot_preserves_all_recorded_counts_even_when_privacy_fails_closed
    record = Round4BoundaryTest.new('unused').record
    record.total_recorded_attributes = 19
    record.total_recorded_events = 7
    record.total_recorded_links = 5
    record.attributes = {'ordinary' => 'value'}
    [false, true].each do |fail_closed|
      record.attributes['cycle'] = record.attributes if fail_closed
      copy = A::Privacy.snapshot(record, A::ExportResource.new({}), A::Config.new({}, env: {}))
      assert_equal [19, 7, 5], [copy.total_recorded_attributes, copy.total_recorded_events, copy.total_recorded_links]
      proto = Opentelemetry::Proto::Collector::Trace::V1::ExportTraceServiceRequest.decode(A::Protobuf.encode([copy]))
      span = proto.resource_spans.first.scope_spans.first.spans.first
      assert_equal 19 - copy.attributes.size, span.dropped_attributes_count
      assert_equal 7, span.dropped_events_count
      assert_equal 5, span.dropped_links_count
    end
  end
end

class EndToEndTest
  def test_s6_ignored_fi_credentials_from_every_config_source_are_redacted_in_actual_otlp
    file = Tempfile.new(['s6-unused-pairs', '.toml'], File.join(ROOT, '.bundle/tmp'))
    ignored = ['ignored-explicit-fi-api-12345678', 'unused-file-fi-api-12345678', 'unused-file-fi-secret-12345678']
    file.write("api_key = #{ignored[1].inspect}\nsecret_key = #{ignored[2].inspect}\n"); file.close
    values = ignored + %w[fixture-api-12345678 fixture-secret-12345678]
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init(config_file: #{file.path.inspect}, api_key: #{ignored.first.inspect})
      values = #{values.inspect}.join(' ')
      host.tracer('app').in_span('ignored-fi-pair') do |span|
        span.set_attribute('ordinary', values)
        span.add_event('echo', attributes: {'ordinary' => values})
      end
      abort 'flush failed' unless TraceAI::Auto.flush
      original = memory.finished_spans.first
      abort 'host data mutated' unless original.attributes['ordinary'] == values && original.events.first.attributes['ordinary'] == values
      abort 'diagnostics leaked' if #{ignored.inspect}.any? { |value| TraceAI::Auto::Codec.generate(TraceAI::Auto.report).include?(value) }
      puts 'ORIGINAL_PRESERVED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "ORIGINAL_PRESERVED\n", stdout
    assert_includes stderr, 'explicit arguments credential pair ignored: missing FI_SECRET_KEY'
    values.each { |value| refute_includes stderr, value }
    spans = export_spans.map(&:first)
    assert_equal 1, spans.length
    values.each { |value| refute_includes spans.map(&:to_h).to_s, value }
    assert_equal Array.new(values.length, '<redacted>').join(' '), attrs(spans.first.attributes)['ordinary']
  ensure
    file&.unlink
  end

  def test_a7_real_sdk_admission_and_serialized_drop_counts_use_actual_host_limits
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new(resource: OpenTelemetry::SDK::Resources::Resource.create('service.name' => 'checkout-api', 'service.namespace' => 'commerce'))
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init
      abort 'host replaced' unless TraceAI::Auto.provider.equal?(host)
      req = {method: 'POST', url: 'http://127.0.0.1:1234/v1/chat/completions', headers: {'content-type' => 'application/json'},
             body: JSON.generate(model: 'm', messages: 80.times.map { |i| {role: 'user', content: 'question-' + i.to_s} })}
      body = JSON.generate(id: 'chat-1', choices: [{message: {role: 'assistant', content: 'answer'}, finish_reason: 'stop'}], usage: {prompt_tokens: 80, completion_tokens: 1, total_tokens: 81})
      resp = {status: 200, headers: {'content-type' => 'application/json'}, body: body, size: body.bytesize, incomplete: true, error: IOError.new('read interrupted')}
      summaries = [128, 16, 8].map do |limit|
        host.span_limits = OpenTelemetry::SDK::Trace::SpanLimits.new(attribute_count_limit: limit)
        candidates = TraceAI::Auto::Wire.build(req, resp).attributes
        expected = TraceAI::Auto::AttributeProjection.project(candidates, limit)
        started = Time.now
        TraceAI::Auto.emit(req, resp, TraceAI::Auto.current_context, started, started + 0.01)
        record = memory.finished_spans.last
        abort 'projection not admitted' unless record.attributes == expected.attributes
        abort 'host limit changed' unless host.span_limits.attribute_count_limit == limit
        abort 'recorded count lost' unless record.total_recorded_attributes == record.attributes.size + expected.dropped
        copy = TraceAI::Auto::Privacy.snapshot(record, TraceAI::Auto::ExportResource.new({}), TraceAI::Auto.config)
        abort 'snapshot count lost' unless copy.total_recorded_attributes == record.total_recorded_attributes
        {limit: limit, before: candidates.size, dropped: expected.dropped, attributes: record.attributes}
      end
      abort 'flush failed' unless TraceAI::Auto.flush
      puts JSON.generate(summaries)
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    summaries = JSON.parse(stdout)
    large = summaries.first.fetch('attributes')
    assert_equal 'answer', large['output.value']
    assert_equal true, large['traceai.wire.incomplete']
    assert_equal 'IOError', large['error.type']
    assert_equal 80, large['gen_ai.usage.input_tokens']
    spans = export_spans
    assert_equal 3, spans.length
    summaries.zip(spans).each do |summary, (span, resource)|
      assert_equal summary['attributes'], attrs(span.attributes)
      assert_equal summary['dropped'], span.dropped_attributes_count
      assert_operator summary['dropped'], :>, 0
      assert_equal :STATUS_CODE_ERROR, span.status.code
      assert_equal 'checkout-api', resource['service.name']
      assert_equal 'commerce', resource['service.namespace']
      assert_equal 'ruby-e2e', resource['project_name']
    end
  end

  def test_a11_neutral_destinations_activate_without_fi_and_do_not_duplicate_or_shutdown_host_routes
    %w[provider global exporter processor all exporter_with_keys all_with_keys].each do |variant|
      mode = variant.delete_suffix('_with_keys')
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'net/http'; require 'json'; require 'traceai/auto/agent'; require 'traceai/auto/export'
        class << TraceAI::Auto::WireExporter
          def new(*) = raise('FI exporter constructed in neutral mode')
        end
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
        host = OpenTelemetry::SDK::Trace::TracerProvider.new
        mode = #{mode.inspect}
        if %w[provider global all].include?(mode)
          host.add_span_processor(processor)
          OpenTelemetry.tracer_provider = host
        end
        options = case mode
                  when 'provider' then {provider: host}
                  when 'global' then {}
                  when 'exporter' then {exporter: memory}
                  when 'processor' then {processor: processor}
                  else {provider: host, processor: processor, exporter: memory}
                  end
        provider = TraceAI::Auto.init(**options)
        abort 'inactive' unless TraceAI::Auto.active?
        abort 'host replaced' if %w[provider global all].include?(mode) && !provider.equal?(host)
        provider.tracer('app').in_span('neutral-parent', attributes: {'ordinary' => 'neutral-test-token-12345678'}) do
          response = Net::HTTP.post(URI('#{@provider.url}/v1/chat/completions'), '{"model":"m","messages":[{"role":"user","content":"question"}]}', {'content-type' => 'application/json'})
          abort 'response changed' unless response.body == #{CHAT.inspect}
        end
        abort 'flush failed' unless TraceAI::Auto.flush
        records = memory.finished_spans
        abort 'duplicate spans' unless records.size == 2 && records.map(&:name).uniq.size == 2
        native = records.find { |span| span.name == 'neutral-parent' }
        expected = mode == 'exporter' ? '<redacted>' : 'neutral-test-token-12345678'
        abort 'incorrect destination privacy' unless native.attributes['ordinary'] == expected
        wire = records.find { |span| span.name != 'neutral-parent' }
        abort 'default capture disabled' unless wire.attributes['output.value'] == 'Hello from Ruby'
        unless mode == 'exporter'
          abort 'shutdown failed' unless TraceAI::Auto.shutdown
          provider.tracer('app').in_span('host-after-shutdown') {}
          abort 'host stopped' unless memory.finished_spans.any? { |span| span.name == 'host-after-shutdown' }
        end
        puts 'NEUTRAL_ACTIVE'
      RUBY
      env = {'RUBYOPT' => nil, 'CUSTOM_TOKEN' => 'neutral-test-token-12345678'}
      env.merge!('FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil) unless variant.end_with?('_with_keys')
      stdout, stderr, status = app(code, env)
      assert status.success?, variant + ' ' + stderr
      assert_equal "NEUTRAL_ACTIVE\n", stdout, mode
      refute_includes stderr, 'missing FI_API_KEY', mode
      assert_empty export_spans
    end
    assert_equal 7, @provider.requests.size
  end

  def test_a11_zero_code_defers_until_host_destination_exists_without_fi
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      response = Net::HTTP.post(URI('#{@provider.url}/v1/chat/completions'), '{"model":"m","messages":[]}', {'content-type' => 'application/json'})
      abort 'inactive' unless TraceAI::Auto.active? && TraceAI::Auto.provider.equal?(host)
      abort 'missing wire span' unless memory.finished_spans.size == 1
      abort 'FI exporter created' if TraceAI::Auto.exporter
      print response.body
    RUBY
    stdout, stderr, status = app(code, {'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil})
    assert status.success?, stderr
    assert_equal CHAT, stdout
    refute_includes stderr, 'missing FI_API_KEY'
    assert_empty export_spans
  end

  def test_a11_supplied_otlp_exporter_gets_detached_privacy_and_sdk_status_codes
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'opentelemetry/exporter/otlp'; require 'traceai/auto/agent'
      sink = OpenTelemetry::Exporter::OTLP::Exporter.new(endpoint: '#{@collector.url}/tracer/v1/traces', compression: 'none')
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init(provider: host, exporter: sink, hide_outputs: true)
      context = OpenTelemetry::Trace::SpanContext.new(trace_id: ['01' * 16].pack('H*'), span_id: ['02' * 8].pack('H*'), remote: true,
        trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED, tracestate: OpenTelemetry::Trace::Tracestate.from_string('vendor=neutral-test-token-12345678'))
      link = OpenTelemetry::Trace::Link.new(context, {'ordinary' => 'neutral-test-token-12345678'})
      [:ok, :error, :unset].each do |outcome|
        span = host.tracer('app').start_span(outcome.to_s, links: [link], attributes: {'ordinary' => 'neutral-test-token-12345678', 'output.value' => 'PRIVATE_OUTPUT'})
        span.add_event('echo', attributes: {'ordinary' => 'neutral-test-token-12345678'})
        span.status = OpenTelemetry::Trace::Status.public_send(outcome, *(:error == outcome ? ['failure neutral-test-token-12345678'] : []))
        span.finish
      end
      abort 'flush failed' unless TraceAI::Auto.flush
      abort 'host mutated' unless memory.finished_spans.all? { |span| span.attributes['ordinary'] == 'neutral-test-token-12345678' && span.attributes['output.value'] == 'PRIVATE_OUTPUT' }
      puts 'HOST_UNCHANGED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil, 'CUSTOM_TOKEN' => 'neutral-test-token-12345678'})
    assert status.success?, stderr
    assert_equal "HOST_UNCHANGED\n", stdout
    refute_includes stderr, 'missing FI_API_KEY'
    request = @collector.requests.pop(true)
    refute request[:headers].key?('x-api-key')
    refute request[:headers].key?('x-secret-key')
    refute_includes request[:body], 'neutral-test-token-12345678'
    refute_includes request[:body], 'PRIVATE_OUTPUT'
    proto = Opentelemetry::Proto::Collector::Trace::V1::ExportTraceServiceRequest.decode(request[:body])
    spans = proto.resource_spans.flat_map { |rs| rs.scope_spans.flat_map(&:spans) }
    assert_equal %i[STATUS_CODE_OK STATUS_CODE_ERROR STATUS_CODE_UNSET], spans.map { |span| span.status.code }
    assert_equal 3, spans.length
    assert_equal '<redacted>', attrs(spans.first.attributes)['ordinary']
    assert_equal '__REDACTED__', attrs(spans.first.attributes)['output.value']
    assert_equal '<redacted>', attrs(spans.first.links.first.attributes)['ordinary']
    assert_equal 'vendor=<redacted>', spans.first.links.first.trace_state
    assert_equal 1 | 256 | 512, spans.first.links.first.flags
    assert_equal '<redacted>', attrs(spans.first.events.first.attributes)['ordinary']
  end

  def test_a11_explicit_fi_route_coexists_with_host_without_duplication
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
      host.add_span_processor(processor)
      TraceAI::Auto.init(provider: host, processor: processor, exporter: memory, fi_export: true)
      host.tracer('app').in_span('both-destinations') {}
      abort 'flush failed' unless TraceAI::Auto.flush
      abort 'host duplicated' unless memory.finished_spans.size == 1
      puts 'BOTH_ACTIVE'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "BOTH_ACTIVE\n", stdout
    assert_equal ['both-destinations'], export_spans.map { |span, _| span.name }
  end

  def test_a11_no_destination_and_explicit_disable_leave_the_host_untouched
    %w[missing disabled].each do |mode|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'net/http'; require 'traceai/auto/agent'
        original = OpenTelemetry.tracer_provider
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        host = OpenTelemetry::SDK::Trace::TracerProvider.new
        options = #{mode.inspect} == 'disabled' ? {provider: host, exporter: memory} : {}
        TraceAI::Auto.init(**options)
        abort 'activated' if TraceAI::Auto.active?
        abort 'provider replaced' unless original.equal?(OpenTelemetry.tracer_provider)
        abort 'destination added' unless host.instance_variable_get(:@span_processors).empty?
        puts 'UNCHANGED'
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil, 'OTEL_SDK_DISABLED' => mode == 'disabled' ? 'true' : nil})
      assert status.success?, stderr
      assert_equal "UNCHANGED\n", stdout
      assert_empty export_spans
    end
  end

  def test_a7_standalone_projection_preserves_usage_and_degradation_under_128_attributes
    code = <<~RUBY
      require 'net/http'; require 'traceai/auto/agent'
      TraceAI::Auto.init
      abort 'unexpected SDK' if TraceAI::Auto.instance_variable_get(:@sdk)
      body = TraceAI::Auto::Codec.generate(model: 'm', messages: 80.times.map { |i| {role: 'user', content: 'question-' + i.to_s} })
      req = {method: 'POST', url: 'http://127.0.0.1:1234/v1/chat/completions', headers: {'content-type' => 'application/json'}, body: body}
      response = #{CHAT.inspect}
      resp = {status: 200, headers: {'content-type' => 'application/json'}, body: response, size: response.bytesize, incomplete: true}
      TraceAI::Auto.emit(req, resp, TraceAI::Auto.current_context, Time.now)
      abort 'flush failed' unless TraceAI::Auto.flush
      puts 'STANDALONE_PROJECTED'
    RUBY
    stdout, stderr, status = app(code, standalone_env.merge('RUBYOPT' => nil), chdir: File.dirname(ROOT))
    assert status.success?, stderr
    assert_equal "STANDALONE_PROJECTED\n", stdout
    spans = export_spans
    assert_equal 1, spans.length
    span = spans.first.first
    values = attrs(span.attributes)
    assert_operator values.size, :<=, 128
    assert_equal 3, values['gen_ai.usage.input_tokens']
    assert_equal true, values['traceai.wire.incomplete']
    assert_equal 'Hello from Ruby', values['output.value']
    assert_operator values['traceai.capture.dropped_content_count'], :>, 0
    assert_operator span.dropped_attributes_count, :>, 0
  end
end
