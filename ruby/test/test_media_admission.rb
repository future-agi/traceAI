# frozen_string_literal: true
require_relative 'test_e2e'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/export'
require_relative '../lib/traceai/auto/media_admission'

class MediaAdmissionTest < Minitest::Test
  A = TraceAI::Auto
  P = A::Privacy
  PAYLOAD = 'AQIDBA=='
  CONFIG = A::Config.new({}, env: {})

  def self.limits(count, length)
    limits = OpenTelemetry::SDK::Trace::SpanLimits.new(**(count ? {
      attribute_count_limit: count, event_attribute_count_limit: count, link_attribute_count_limit: count
    } : {}))
    # SDK 1.10 rejects configured lengths below 32. Exercise its actual truncator
    # with custom host limits at 8/16/24 as well as its supported default/32.
    limits.instance_variable_set(:@attribute_length_limit, length)
    limits.instance_variable_set(:@event_attribute_length_limit, length)
    limits
  end

  def self.values(hint = 'mimeType', value = 'application/octet-stream; codec=pcm')
    root = 'gen_ai.input.messages.199.audio.transcript.id.parts[0]'
    payloads = {root + '.content.chunk' => PAYLOAD, root + '.data.samples' => [1, 2, 3, 4],
                'arbitrary.magic' => ['iVBORw0KGgoAAAAA'], 'ordinary' => 'safe sentence'}
    filler = 200.times.to_h { |i| ["gen_ai.input.messages.#{i}.message.role", 'user'] }
    payloads.merge(filler).merge(root + '.' + hint => value)
  end

  # These tests pin the admission model on SDKs whose hooks the probe proves. On any other SDK
  # exports omit media instead (test_media_sdk_verification.rb).
  def setup
    skip 'media admission hooks are unproven on this SDK' unless A::MediaAdmission.verified?
  end

  def provider(count = nil, length = nil)
    OpenTelemetry::SDK::Trace::Span.prepend(A::MediaAdmission) unless OpenTelemetry::SDK::Trace::Span.ancestors.include?(A::MediaAdmission)
    host = OpenTelemetry::SDK::Trace::TracerProvider.new
    host.span_limits = self.class.limits(count, length)
    host.add_span_processor(A::CoveredAncestry.new)
    host
  end

  def context
    OpenTelemetry::Trace::SpanContext.new(trace_id: "\1" * 16, span_id: "\2" * 8)
  end

  def snapshot(record)
    P.snapshot(record, A::ExportResource.new({}), CONFIG)
  end

  def assert_no_media(attributes)
    text = A::Codec.generate(attributes)
    refute_includes text, PAYLOAD
    refute_includes text, 'iVBOR'
    refute attributes.values.include?([1, 2, 3, 4])
  end

  def test_default_and_32_128_140_limits_precede_media_context_eviction
    [nil, 32, 128, 140].product([nil, 8, 16, 24, 32]).each do |count, length|
      host = provider(count, length)
      values = self.class.values
      span = host.tracer('app').start_span('limited-media', attributes: values.dup,
        links: [OpenTelemetry::Trace::Link.new(context, values.to_a.reverse.to_h)])
      span.add_event('media', attributes: values.to_a.reverse.to_h)
      span.finish
      original = span.to_span_data
      assert_equal PAYLOAD, original.attributes.values.first, [count, length].inspect
      assert_equal PAYLOAD, original.events.first.attributes.values.last
      assert_equal PAYLOAD, original.links.first.attributes.values.last
      copy = snapshot(original)
      assert_no_media(copy.attributes)
      assert_no_media(copy.events.first.attributes)
      assert_no_media(copy.links.first.attributes)
      assert_equal original.total_recorded_attributes, copy.total_recorded_attributes
      assert_equal PAYLOAD, original.attributes.values.first
      refute_includes Marshal.dump(original.instance_variable_get(:@traceai_media)), PAYLOAD
      assert_operator copy.attributes.size, :<=, count || 128
    end
  end

  def test_short_truncated_mime_and_type_hints_are_not_safe
    [['mimeType', 'application/octet-stream'], ['mime_type', 'audio/pcm'], ['media_type', 'image/raw'], ['type', 'input_audio']].each do |name, value|
      [8, 16, 24].each do |length|
        host = provider(140, length)
        values = {'blob.id.content.chunk' => PAYLOAD, 'blob.id.' + name => value,
                  'blob.id.format' => 'pcm16', 'ordinary' => 'x' * 40}
        span = host.tracer('app').start_span('length-media', attributes: values.dup)
        span.add_event('media', attributes: values.dup)
        span.add_link(OpenTelemetry::Trace::Link.new(context, values.dup))
        span.finish
        record = span.to_span_data
        assert_equal PAYLOAD, record.attributes['blob.id.content.chunk']
        copy = snapshot(record)
        assert_no_media(copy.attributes)
        assert_no_media(copy.events.first.attributes)
        assert_no_media(copy.links.first.attributes)
      end
    end
    refute P.safe_mime?('mime_type', 'text/...')
    refute P.safe_mime?('mime_type', 'application/octet-str...')
    assert P.safe_mime?('mime_type', 'text/plain')
  end

  def test_sequential_setters_and_late_evicted_hints_fail_closed
    host = provider(32, 8)
    span = host.tracer('app').start_span('setters')
    assert_same span, span.set_attribute('blob.id.content.chunk', PAYLOAD)
    assert_same span, span.add_attributes(40.times.to_h { |i| ["ordinary.#{i}", 'safe sentence'] })
    span['blob.id.mime_type'] = 'audio/pcm'
    span.add_event('media', attributes: self.class.values.to_a.reverse.to_h)
    2.times { span.add_link(OpenTelemetry::Trace::Link.new(context, self.class.values.to_a.reverse.to_h)) }
    span.finish
    original = span.to_span_data
    assert_equal PAYLOAD, original.attributes['blob.id.content.chunk']
    refute original.attributes.key?('blob.id.mime_type')
    copy = snapshot(original)
    assert_no_media(copy.attributes)
    copy.events.each { |event| assert_no_media(event.attributes) }
    copy.links.each { |link| assert_no_media(link.attributes) }
  end

  def test_projection_scrubs_complete_candidates_before_any_selection
    ['transcript', 'id'].each do |boundary|
      root = 'gen_ai.usage.' + boundary + '.parts.199'
      values = {root + '.content.chunk' => PAYLOAD, root + '.mime_type' => 'audio/pcm', 'output.value' => '{unfinished JSON'}
      [1, 32, 128, 140].each do |limit|
        selected = A::AttributeProjection.project(values, limit).attributes
        assert_no_media(selected)
      end
    end
  end

  def test_fallback_preserves_sibling_hints_for_protected_media
    values = {'gen_ai.usage.parts.0.mime_type' => 'audio/pcm', 'gen_ai.usage.parts.0.content.chunk' => PAYLOAD,
              'gen_ai.usage.parts.1.mime_type' => 'image/raw', 'gen_ai.usage.parts.1.content.chunk' => [1, 2, 3, 4],
              'gen_ai.usage.input_tokens' => 7, 'output.value' => '{unfinished JSON'}
    [values, values.to_a.reverse.to_h].each do |attributes|
      result = P.safe_metadata(attributes)
      assert_equal '<omitted: 8 base64 chars>', result['gen_ai.usage.parts.0.content.chunk']
      assert_equal '<omitted: media>', result['gen_ai.usage.parts.1.content.chunk']
      assert_equal 7, result['gen_ai.usage.input_tokens']
      assert_no_media(P.scrub_attributes(attributes))
    end
  end

  def test_export_without_sidecars_fails_closed_after_loss_or_truncation
    span = provider.tracer('app').start_span('forwarded'); span.finish
    record = span.to_span_data
    record.remove_instance_variable(:@traceai_media)
    ['audio.transcript', 'blob.id', 'ordinary'].each do |root|
      [true, false].each do |dropped|
        record.attributes = {root + '.content.chunk' => PAYLOAD, root + '.data' => [1, 2, 3, 4]}
        record.attributes[root + '.mime_type'] = 'applicati...' unless dropped
        record.total_recorded_attributes = record.attributes.size + (dropped ? 1 : 0)
        assert_no_media(snapshot(record).attributes)
      end
    end
  end

  def test_event_and_link_loss_alone_trigger_export_fallback
    host = provider(32)
    values = 40.times.to_h { |i| ["ordinary.#{i}", 'safe sentence'] }.merge('blob.data' => PAYLOAD)
    span = host.tracer('app').start_span('event-link-only')
    span.add_event('media', attributes: values.dup)
    span.add_link(OpenTelemetry::Trace::Link.new(context, values.dup))
    span.finish
    original = span.to_span_data
    assert_equal 0, original.total_recorded_attributes
    assert_equal PAYLOAD, original.events.first.attributes['blob.data']
    assert_equal PAYLOAD, original.links.first.attributes['blob.data']
    copy = snapshot(original)
    assert_no_media(copy.events.first.attributes)
    assert_no_media(copy.links.first.attributes)
  end

  def test_truncated_mime_arrays_and_unknown_sibling_base64_fail_closed
    values = {'blob.mime_type.0' => 'text/plain', 'blob.mime_type.1' => 'application/octet-str...',
              'blob.opaque' => PAYLOAD, 'missing.opaque' => PAYLOAD, 'unknown' => 'iVBOR...'}
    result = P.scrub(values, [], lossy: true)
    assert_equal '<omitted: media>', result['blob.opaque']
    assert_equal '<omitted: media>', result['missing.opaque']
    assert_equal '<omitted: media>', result['unknown']
  end

  def test_empty_payload_admission_remembers_media_context_without_omitting_empty_values
    [[], nil].each do |empty|
      values = {'input.mime_type' => 'application/json', 'input.parts.199.mime_type' => 'audio/pcm',
                'input.parts.199.content.chunk' => empty}
      evidence = P.media_admission(values, count_limit: 2)
      assert evidence.omitted['input.parts.199.content.chunk']
      assert_equal values, P.admitted_attributes(values, evidence)
      [PAYLOAD, [1, 2, 3, 4]].each do |payload|
        retained = {'input.mime_type' => 'application/json', 'input.parts.199.content.chunk' => payload}
        later = P.media_admission(retained, previous: evidence)
        assert_equal '<omitted: media>', P.admitted_attributes(retained, later)['input.parts.199.content.chunk']
        refute_includes Marshal.dump(later), PAYLOAD
      end
    end
  end

  def test_outer_mime_cannot_exempt_late_payloads_or_detached_lossy_records
    [nil, 32, 128, 140].product([nil, 8, 16, 24], ['', '.transcript', '.id'], [PAYLOAD, [1, 2, 3, 4]]).each do |count, length, boundary, payload|
      root = 'input.messages.199.parts.0' + boundary
      key = root + '.content.arbitrary[0]'
      values = {key => [], 'input.mime_type' => 'application/json'}
      values.merge!(200.times.to_h { |i| ["filler.#{i}", 'ordinary words'] })
      values[root + '.mime_type'] = 'audio/pcm'
      span = provider(count, length).tracer('app').start_span('late-media', attributes: values)
      assert_same span, span.set_attribute(key, payload)
      retained = span.to_span_data.attributes.dup
      refute retained.key?(root + '.mime_type')
      span.add_event('media', attributes: retained.dup)
      span.add_link(OpenTelemetry::Trace::Link.new(context, retained.dup))
      span.finish
      record = span.to_span_data
      assert_equal payload, record.attributes[key]
      [true, false].each do |sidecar|
        record.remove_instance_variable(:@traceai_media) unless sidecar
        copy = snapshot(record)
        [copy.attributes, copy.events.first.attributes, copy.links.first.attributes].each do |attributes|
          assert_equal '<omitted: media>', attributes[key], [count, length, boundary, sidecar].inspect
        end
      end
    end
  end

  def test_lossy_outer_mime_does_not_protect_untyped_descendant_base64
    values = {'input.mime_type' => 'application/json', 'input.parts.199.opaque' => PAYLOAD,
              'input.parts[198].opaque' => PAYLOAD, 'input.opaque' => 'ordinary words'}
    assert_equal values, P.scrub(values)
    result = P.scrub(values, [], lossy: true)
    assert_equal '<omitted: media>', result['input.parts.199.opaque']
    assert_equal '<omitted: media>', result['input.parts[198].opaque']
    assert_equal 'ordinary words', result['input.opaque']
    assert_equal 'application/json', result['input.mime_type']
    result = P.scrub({'input.mime_type' => 'text/plain', 'input.content.arbitrary' => [1, 2, 3, 4]}, [], lossy: true)
    assert_equal '<omitted: media>', result['input.content.arbitrary']
  end

  # Admission bookkeeping runs inside the application's own span calls. If it
  # fails, the application's call still completes and the export fails closed.
  def test_admission_failure_never_reaches_application_span_calls
    span = provider.tracer('app').start_span('bookkeeping-failure')
    P.stub(:media_admission, ->(*_args, **_kwargs) { raise NoMethodError, 'bookkeeping' }) do
      assert_same span, span.set_attribute('blob.data', PAYLOAD)
      assert_same span, span.add_event('media', attributes: {'blob.data' => PAYLOAD})
    end
    span.finish
    record = span.to_span_data
    assert_equal 1, record.events.length
    assert record.instance_variable_get(:@traceai_media)[:failed]
    assert_no_media(snapshot(record).attributes)
  end

  def test_complete_metadata_boundaries_and_text_mime_are_preserved
    values = {'audio.transcript.content' => PAYLOAD, 'audio.id.value' => 'public id',
              'blob.mime_type' => 'text/plain', 'blob.content' => 'ordinary words'}
    span = provider.tracer('app').start_span('intact', attributes: values.dup); span.finish
    assert_equal values, snapshot(span.to_span_data).attributes
    # Failed admission/unsupported structures cannot restore media via fallback.
    evidence = P.media_admission({'output.value' => '{bad json', 'blob.data' => PAYLOAD})
    assert evidence.failed
  end
end

class EndToEndTest
  def test_prior_fallback_finding_is_closed_on_actual_fi_export
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init
      ['ordinary output', '{unfinished JSON'].each do |output|
        host.tracer('app').in_span('fallback-media', attributes: {
          'gen_ai.span.kind' => 'CHAIN',
          'gen_ai.usage.parts.0.mime_type' => 'audio/pcm', 'gen_ai.usage.parts.0.content.chunk' => #{MediaAdmissionTest::PAYLOAD.inspect},
          'gen_ai.usage.parts.1.mime_type' => 'image/raw', 'gen_ai.usage.parts.1.content.chunk' => [1, 2, 3, 4], 'output.value' => output
        }) {}
      end
      abort 'host changed' unless memory.finished_spans.all? { |span| span.attributes['gen_ai.usage.parts.0.content.chunk'] == #{MediaAdmissionTest::PAYLOAD.inspect} && span.attributes['gen_ai.usage.parts.1.content.chunk'] == [1, 2, 3, 4] }
      abort 'flush failed' unless TraceAI::Auto.flush
      puts 'HOST_UNCHANGED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "HOST_UNCHANGED\n", stdout
    spans = export_spans.map(&:first)
    assert_equal 2, spans.size
    spans.each do |span|
      values = attrs(span.attributes)
      assert_equal '<omitted: media>', values['gen_ai.usage.parts.0.content.chunk']
      assert_equal '<omitted: media>', values['gen_ai.usage.parts.1.content.chunk']
      refute_includes span.to_h.to_s, MediaAdmissionTest::PAYLOAD
    end
  end

  def test_wire_projection_scrubs_media_in_long_messages_before_sdk_limits
    %w[owned host].each do |ownership|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'net/http'; require 'json'; require 'traceai/auto/agent'
        if #{ownership.inspect} == 'host'
          OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new
        end
        TraceAI::Auto.init
        host = TraceAI::Auto.provider
        messages = 200.times.map { |i| {role: 'user', content: 'message number ' + i.to_s} }
        messages.last[:content] = [{type: 'input_audio', input_audio: {data: #{MediaAdmissionTest::PAYLOAD.inspect}, format: 'pcm16'}}]
        request_body = JSON.generate(model: 'm', messages: messages)
        [[nil, nil], [32, 8], [128, 16], [140, 24]].each do |count, length|
          limits = OpenTelemetry::SDK::Trace::SpanLimits.new(**(count ? {attribute_count_limit: count} : {}))
          limits.instance_variable_set(:@attribute_length_limit, length)
          host.span_limits = limits
          response = Net::HTTP.post(URI('#{@provider.url}/v1/chat/completions'), request_body, {'content-type' => 'application/json'})
          abort 'application response changed' unless response.body == #{CHAT.inspect}
        end
        abort 'flush failed' unless TraceAI::Auto.flush
        puts request_body
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil})
      assert status.success?, stderr
      4.times do
        assert_equal stdout.strip, @provider.requests.pop(true).fetch(:body)
      end
      spans = export_spans.map(&:first)
      assert_equal 4, spans.size
      spans.each do |span|
        refute_includes span.to_h.to_s, MediaAdmissionTest::PAYLOAD
        assert_operator span.dropped_attributes_count, :>, 0
      end
    end
  end

  def test_owned_and_host_providers_zero_code_and_init_preserve_application_and_omit_media
    skip 'media admission hooks are unproven on this SDK' unless TraceAI::Auto::MediaAdmission.verified?
    # Without an application provider, a zero-code start waits for one (or for
    # the first AI request), so application spans need 'host' or an explicit init.
    [%w[owned init], %w[host zero], %w[host init]].each do |ownership, boot|
      baseline = nil
      [false, true].each do |enabled|
        code = <<~RUBY
          require 'opentelemetry/sdk'; require 'json'
          ENV['FI_API_KEY'] = 'fixture-api-12345678'; ENV['FI_SECRET_KEY'] = 'fixture-secret-12345678'
          host = OpenTelemetry::SDK::Trace::TracerProvider.new if #{ownership.inspect} == 'host' || !#{enabled}
          OpenTelemetry.tracer_provider = host if host
          require 'net/http'
          if #{enabled} && #{boot.inspect} == 'init'
            require 'traceai/auto/agent'
            TraceAI::Auto.init
          end
          host ||= TraceAI::Auto.provider
          abort 'provider replaced' if #{enabled} && !TraceAI::Auto.provider.equal?(host)
          memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
          host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
          values = #{MediaAdmissionTest.values.inspect}
          ctx = OpenTelemetry::Trace::SpanContext.new(trace_id: "\\1" * 16, span_id: "\\2" * 8)
          [nil, 32, 128, 140].product([nil, 8, 16, 24]).each do |count, length|
            limits = OpenTelemetry::SDK::Trace::SpanLimits.new(**(count ? {
              attribute_count_limit: count, event_attribute_count_limit: count, link_attribute_count_limit: count
            } : {}))
            limits.instance_variable_set(:@attribute_length_limit, length)
            limits.instance_variable_set(:@event_attribute_length_limit, length)
            host.span_limits = limits
            # The AI marker comes first so that count limits never drop it.
            span = host.tracer('app').start_span('host-media', attributes: {'gen_ai.span.kind' => 'CHAIN'}.merge(values),
              links: [OpenTelemetry::Trace::Link.new(ctx, values.to_a.reverse.to_h)])
            abort 'setter return changed' unless span.add_attributes('plain' => 'text with spaces').equal?(span)
            span.add_event('media', attributes: values.to_a.reverse.to_h)
            span.finish
            [#{MediaAdmissionTest::PAYLOAD.inspect}, [1, 2, 3, 4]].each do |payload|
              key = 'input.messages.199.parts.0.content.chunk'
              late = {'gen_ai.span.kind' => 'CHAIN', key => [], 'input.mime_type' => 'application/json'}
              200.times { |i| late["filler." + i.to_s] = 'ordinary words' }
              late['input.messages.199.parts.0.mime_type'] = 'audio/pcm'
              span = host.tracer('app').start_span('host-late-media', attributes: late)
              abort 'setter return changed' unless span.set_attribute(key, payload).equal?(span)
              retained = span.to_span_data.attributes.dup
              abort 'nested MIME survived' if retained.key?('input.messages.199.parts.0.mime_type')
              span.add_event('media', attributes: retained.dup)
              abort 'link return changed' unless span.add_link(OpenTelemetry::Trace::Link.new(ctx, retained.dup)).equal?(span)
              span.finish
            end
          end
          body = Net::HTTP.get(URI('#{@provider.url}/health'))
          abort 'flush failed' if #{enabled} && !TraceAI::Auto.flush
          puts JSON.generate([body, memory.finished_spans.map { |s| [s.attributes, s.events.map(&:attributes), s.links.map(&:attributes), s.total_recorded_attributes] }])
        RUBY
        env = {'RUBYOPT' => enabled && boot == 'zero' ? '-rtraceai/auto' : nil}
        # Defer RUBYOPT initialization until the application's provider exists.
        env.merge!('FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil) if ownership == 'host' && boot == 'zero'
        stdout, stderr, status = app(code, env)
        assert status.success?, "#{ownership}/#{boot}/#{enabled}: #{stderr}"
        baseline ||= stdout
        assert_equal baseline, stdout, "host view changed: #{ownership}/#{boot}"
        spans = export_spans.map(&:first)
        if enabled
          assert_equal 48, spans.size
          assert_equal 32, spans.count { |span| span.name == 'host-late-media' }
          spans.select { |span| span.name == 'host-late-media' }.each do |span|
            assert_operator span.dropped_attributes_count, :>, 0
          end
          spans.each do |span|
            [attrs(span.attributes), *span.events.map { |event| attrs(event.attributes) }, *span.links.map { |link| attrs(link.attributes) }].each do |attributes|
              refute_includes attributes.to_s, MediaAdmissionTest::PAYLOAD
              refute_includes attributes.to_s, 'iVBOR'
              refute attributes.values.include?([1, 2, 3, 4])
            end
          end
        else
          assert_empty spans
        end
      end
    end
  end

  # Media bookkeeping runs inside the application's own span calls. Only spans
  # that can reach Future AGI need it: AI-marked spans and server spans. A
  # database or queue span must not pay for it.
  def test_media_bookkeeping_skips_application_spans_that_cannot_reach_future_agi
    code = app_sdk + <<~RUBY
      abort 'agent not active' unless TraceAI::Auto.active?
      calls = Hash.new(0)
      phase = :setup
      counter = Module.new do
        define_method(:media_admission) do |*arguments, **options|
          calls[phase] += 1
          super(*arguments, **options)
        end
      end
      TraceAI::Auto::Privacy.singleton_class.prepend(counter)
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      phase = :database
      tracer.in_span('db-query', attributes: {'db.statement' => 'SELECT 1'}) do |span|
        span.set_attribute('db.rows', 1)
        span.add_event('fetched', attributes: {'rows' => 1})
      end
      phase = :ai
      tracer.in_span('framework', attributes: {'gen_ai.span.kind' => 'CHAIN'}) { |span| span.set_attribute('note', 'ordinary') }
      phase = :server
      tracer.in_span('GET /health', kind: :server) { |span| span.set_attribute('http.route', '/health') }
      phase = :done
      abort 'bookkeeping ran on a database span: ' + calls.inspect unless calls[:database].zero?
      abort 'bookkeeping skipped an AI or server span: ' + calls.inspect unless calls[:ai].positive? && calls[:server].positive?
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    assert_equal ['framework'], export_spans.map { |span, _| span.name }
  end

  # A span can get its AI marker after other calls. What was recorded before
  # the marker was not observed, so its export treats the span as lossy and
  # omits anything that may be media.
  def test_ai_marker_added_after_other_span_calls_still_omits_media
    code = app_sdk + <<~RUBY
      abort 'agent not active' unless TraceAI::Auto.active?
      host = OpenTelemetry.tracer_provider
      host.span_limits = OpenTelemetry::SDK::Trace::SpanLimits.new(attribute_count_limit: 32, event_attribute_count_limit: 32, link_attribute_count_limit: 32)
      values = 40.times.to_h { |i| ['ordinary.' + i.to_s, 'safe sentence'] }.merge('blob.data' => #{MediaAdmissionTest::PAYLOAD.inspect})
      span = host.tracer('app').start_span('late-marker')
      span.add_event('media', attributes: values.dup)
      span.set_attribute('gen_ai.span.kind', 'CHAIN')
      span.finish
      abort 'application event changed' unless span.to_span_data.events.first.attributes['blob.data'] == #{MediaAdmissionTest::PAYLOAD.inspect}
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert_equal ['late-marker'], spans.map(&:name)
    assert_equal 1, spans.first.events.length
    refute_includes attrs(spans.first.events.first.attributes).to_s, MediaAdmissionTest::PAYLOAD
  end
end
