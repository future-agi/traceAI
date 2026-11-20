# frozen_string_literal: true
require_relative 'test_helper'
require 'tempfile'

require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/media_admission'

class SamplerTest < Minitest::Test
  include SkipWhenMediaHooksUnproven
  include ChildHelper

  def setup
    @collector = MockHTTP.new { |_request| [200, {}, ''] }
  end

  def teardown
    @collector.stop
  end

  # Exercise real SDK decisions, including the W3C parent sampled bit for both
  # local and remote parents. Nothing installs the agent's private provider globally.
  SDK_PROBE = <<~'RUBY'
    abort 'agent inactive' unless TraceAI::Auto.active?
    abort 'global provider replaced' unless OpenTelemetry.tracer_provider.equal?(original)
    tracer = provider.tracer('sampler-test')
    contexts = {'root' => OpenTelemetry::Context.empty}
    [false, true].each do |remote|
      [false, true].each do |sampled|
        flags = OpenTelemetry::Trace::TraceFlags.from_byte(sampled ? 1 : 0)
        parent = OpenTelemetry::Trace::SpanContext.new(trace_id: ['01' * 16].pack('H*'),
          span_id: ['02' * 8].pack('H*'), trace_flags: flags, remote: remote)
        name = (remote ? 'remote_' : 'local_') + (sampled ? 'sampled' : 'unsampled')
        contexts[name] = OpenTelemetry::Trace.context_with_span(
          OpenTelemetry::Trace.non_recording_span(parent), parent_context: OpenTelemetry::Context.empty)
      end
    end
    results = contexts.to_h do |name, context|
      span = tracer.start_span(name, with_parent: context)
      flags = [span.recording?, span.context.trace_flags.sampled?]
      span.finish
      [name, flags]
    end
    abort 'flush failed' unless TraceAI::Auto.flush
    puts JSON.generate(decisions: results, exported: memory.finished_spans.map(&:name).sort,
      exported_flags: memory.finished_spans.map { |span| span.trace_flags.sampled? })
  RUBY

  def sdk_probe(env = {}, options: {}, provisional: false, exporter: false, **environment)
    env = env.merge(environment)
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      original = OpenTelemetry.tracer_provider
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
      options = #{options.inspect}.merge(#{exporter ? 'exporter: memory' : 'processor: processor'})
      TraceAI::Auto.start(options, :report) if #{provisional}
      provider = TraceAI::Auto.init(**options)
      #{SDK_PROBE}
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil}.merge(env))
    assert status.success?, stderr
    assert_empty export_spans
    [JSON.parse(stdout), stderr]
  end

  # Force a kept trace ID so fractional tests never depend on a random draw.
  def ratio_probe(env = {}, options: {}, host: false, **environment)
    env = env.merge(environment)
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
      options = #{options.inspect}.merge(processor: processor)
      if #{host}
        sampler = OpenTelemetry::SDK::Trace::Samplers::ALWAYS_ON
        host = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: sampler)
        options[:provider] = host
      end
      provider = TraceAI::Auto.init(**options)
      abort 'host sampler replaced' if #{host} && !provider.sampler.equal?(sampler)
      tracer = provider.tracer('ratio-test')
      root = tracer.start_span('root', with_parent: OpenTelemetry::Context.empty)
      local = OpenTelemetry::Trace.context_with_span(root, parent_context: OpenTelemetry::Context.empty)
      tracer.start_span('local_child', with_parent: local).finish
      root.finish
      {
        'remote_half' => 'other=kept,fi=r:0.5',
        'remote_missing' => 'other=kept',
        'remote_large' => 'other=kept,fi=r:2',
        'remote_text' => 'other=kept,fi=r:abc',
        'remote_nan' => 'other=kept,fi=r:NaN',
        'remote_infinite' => 'other=kept,fi=r:Infinity',
        'remote_overflow' => 'other=kept,fi=r:1e999',
        'remote_zero' => 'other=kept,fi=r:0',
        'remote_one' => 'other=kept,fi=r:1',
        'remote_unsampled' => 'other=kept,fi=r:0.5',
        'local_half' => 'other=kept,fi=r:0.5',
        'local_missing' => 'other=kept',
        'invalid_parent' => 'other=kept,fi=r:0.5'
      }.each do |name, state|
        parent = OpenTelemetry::Trace::SpanContext.new(
          trace_id: name == 'invalid_parent' ? "\\0".b * 16 : ['01' * 16].pack('H*'),
          span_id: ['02' * 8].pack('H*'), remote: !name.start_with?('local_'),
          trace_flags: OpenTelemetry::Trace::TraceFlags.from_byte(name == 'remote_unsampled' ? 0 : 1),
          tracestate: OpenTelemetry::Trace::Tracestate.from_string(state))
        context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(parent),
          parent_context: OpenTelemetry::Context.empty)
        span = tracer.start_span(name, with_parent: context, attributes: {'existing' => 'kept'})
        if name == 'remote_half'
          inherited = OpenTelemetry::Trace.context_with_span(span, parent_context: OpenTelemetry::Context.empty)
          tracer.start_span('remote_half_child', with_parent: inherited).finish
        end
        span.finish
      end
      TraceAI::Auto.flush
      puts JSON.generate(memory.finished_spans.to_h { |span| [span.name,
        {attributes: span.attributes, state: span.tracestate.to_s, sampled: span.trace_flags.sampled?}] })
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil}.merge(env))
    assert status.success?, stderr
    assert_empty export_spans
    [JSON.parse(stdout), stderr]
  end

  # Real SDK roots and their now-valid local children must agree about which
  # trace owns the probability. Both kinds of invalid ID retain stale state in
  # the SDK's input context; testing only the first span misses child weighting.
  def root_boundary_cases
    cases = {'absent' => {valid: false, sampled: false}}
    %w[zero_trace zero_span valid].each do |id|
      [false, true].each do |remote|
        [false, true].each do |sampled|
          cases["#{id}_#{remote ? 'remote' : 'local'}_#{sampled ? 'sampled' : 'unsampled'}"] =
            {valid: id == 'valid', sampled: sampled}
        end
      end
    end
    cases
  end

  def root_boundary_probe(env = {}, options: {}, host: nil)
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
      options = #{options.inspect}.merge(processor: processor)
      mode = #{host.inspect}
      if mode
        # An application sampler, not a recognized owned delegate. In particular,
        # neither its identity nor its invalid-parent state may be rewritten.
        custom = Object.new
        custom.define_singleton_method(:description) { 'application-custom' }
        custom.define_singleton_method(:should_sample?) do |**arguments|
          OpenTelemetry::SDK::Trace::Samplers::ALWAYS_ON.should_sample?(**arguments)
        end
        host_provider = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: custom)
        host_provider.add_span_processor(processor)
        OpenTelemetry.tracer_provider = host_provider if mode == 'global'
        options[:provider] = host_provider if mode == 'explicit'
      end
      provider = TraceAI::Auto.init(**options)
      if mode == 'late'
        OpenTelemetry.tracer_provider = host_provider
        TraceAI::Auto.adopt_host_provider
        provider = TraceAI::Auto.provider
      end
      abort 'application sampler wrapped' if mode && !provider.sampler.equal?(custom)
      tracer = provider.tracer('root-boundary-test')
      decisions = {}
      states = {}
      #{root_boundary_cases.inspect}.each do |name, fixture|
        context = OpenTelemetry::Context.empty
        unless name == 'absent'
          parent = OpenTelemetry::Trace::SpanContext.new(
            trace_id: name.start_with?('zero_trace') ? "\\0".b * 16 : ['03' * 16].pack('H*'),
            span_id: name.start_with?('zero_span') ? "\\0".b * 8 : ['02' * 8].pack('H*'),
            remote: name.include?('_remote_'),
            trace_flags: OpenTelemetry::Trace::TraceFlags.from_byte(fixture[:sampled] ? 1 : 0),
            tracestate: OpenTelemetry::Trace::Tracestate.from_string('other=kept,fi=r:0.5,another=retained'))
          abort 'bad validity fixture' unless parent.valid? == fixture[:valid]
          context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(parent),
            parent_context: OpenTelemetry::Context.empty)
        end
        root = tracer.start_span(name, with_parent: context, attributes: {'existing' => 'kept'})
        local = OpenTelemetry::Trace.context_with_span(root, parent_context: OpenTelemetry::Context.empty)
        child = tracer.start_span(name + '_child', with_parent: local, attributes: {'existing' => 'kept'})
        decisions[name] = [root.recording?, root.context.trace_flags.sampled?]
        decisions[name + '_child'] = [child.recording?, child.context.trace_flags.sampled?]
        states[name] = root.context.tracestate.to_s
        states[name + '_child'] = child.context.tracestate.to_s
        abort 'new root context invalid' unless root.context.valid?
        child.finish
        root.finish
      end
      abort 'flush failed' unless TraceAI::Auto.flush
      puts JSON.generate(decisions: decisions, states: states, spans: memory.finished_spans.to_h { |span| [span.name,
        {attributes: span.attributes, state: span.tracestate.to_s, trace_id: span.trace_id.unpack1('H*')}] })
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil}.merge(env))
    assert status.success?, stderr
    assert_empty traceai_diagnostics(stderr)
    assert_empty export_spans
    JSON.parse(stdout)
  end

  def assert_root_boundaries(result, rate:, parent_based:, application: false)
    spans = result.fetch('spans')
    root_boundary_cases.each do |name, fixture|
      kept = application || (parent_based && fixture[:valid] ? fixture[:sampled] : rate > 0.0)
      expected_ratio = if !application && kept
                         parent_based && fixture[:valid] ? 0.5 : (rate if rate < 1.0)
                       end
      expected_state = if (application && name != 'absent') || fixture[:valid]
                         'other=kept,fi=r:0.5,another=retained'
                       else
                         ''
                       end
      if expected_ratio && !(parent_based && fixture[:valid])
        expected_state = fixture[:valid] ? "fi=r:#{expected_ratio},other=kept,another=retained" : "fi=r:#{expected_ratio}"
      end
      [name, name + '_child'].each do |span_name|
        assert_equal [kept, kept], result.fetch('decisions').fetch(span_name), span_name
        assert_equal expected_state, result.fetch('states').fetch(span_name), span_name
        unless kept
          refute spans.key?(span_name), span_name
          next
        end
        span = spans.fetch(span_name)
        assert_equal expected_state, span.fetch('state'), span_name
        assert_equal 'kept', span.fetch('attributes').fetch('existing'), span_name
        if expected_ratio
          ratio = span.fetch('attributes').fetch('fi.sampling.ratio')
          assert_instance_of Float, ratio, span_name
          assert_equal expected_ratio, ratio, span_name
        else
          refute span.fetch('attributes').key?('fi.sampling.ratio'), span_name
        end
      end
      assert_equal spans.fetch(name).fetch('trace_id'), spans.fetch(name + '_child').fetch('trace_id'), name if kept
    end
  end

  def test_owned_full_rate_roots_clear_invalid_parent_vendors_before_local_children
    [
      [{}, {}],
      [{'FI_SAMPLE_RATE' => '1'}, {}],
      [{}, {sample_rate: 1.0}],
      [{'OTEL_TRACES_SAMPLER' => 'parentbased_always_on'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '1'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '1'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'always_on'}, {}]
    ].each do |env, options|
      result = root_boundary_probe(env, options: options)
      nonparent = %w[traceidratio always_on].include?(env['OTEL_TRACES_SAMPLER'])
      assert_root_boundaries(result, rate: 1.0, parent_based: !nonparent)
    end
  end

  def test_owned_fractional_roots_replace_invalid_parent_vendors_with_only_own_ratio
    [
      [{}, {sample_rate: 0.25}],
      [{'FI_SAMPLE_RATE' => '0.25'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25'}, {}]
    ].each do |env, options|
      result = root_boundary_probe(env, options: options)
      assert_root_boundaries(result, rate: 0.25, parent_based: env['OTEL_TRACES_SAMPLER'] != 'traceidratio')
    end
  end

  def test_owned_zero_rate_boundaries_preserve_parentbased_and_nonparent_decisions
    [
      [{}, {sample_rate: 0.0}],
      [{'FI_SAMPLE_RATE' => '0'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'parentbased_always_off'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0'}, {}],
      [{'OTEL_TRACES_SAMPLER' => 'always_off'}, {}]
    ].each do |env, options|
      result = root_boundary_probe(env, options: options)
      nonparent = %w[traceidratio always_off].include?(env['OTEL_TRACES_SAMPLER'])
      assert_root_boundaries(result, rate: 0.0, parent_based: !nonparent)
    end
  end

  def test_application_custom_sampler_retains_invalid_parent_state_without_stamping
    %w[explicit global late].each do |mode|
      result = root_boundary_probe({'OTEL_TRACES_SAMPLER' => 'always_on'}, options: {sample_rate: 0.25}, host: mode)
      assert_root_boundaries(result, rate: 1.0, parent_based: false, application: true)
    end
  end

  def traceai_diagnostics(stderr)
    stderr.lines.grep(/\Atraceai-auto:/)
  end

  def test_diagnostics_assertions_ignore_sdk_and_runtime_warnings
    stderr = "SDK warning\nruby warning\ntraceai-auto: own warning\n"
    assert_equal ["traceai-auto: own warning\n"], traceai_diagnostics(stderr)
    assert_empty traceai_diagnostics("SDK warning\nruby warning\n")
  end

  def test_non_wire_owned_spans_protect_ratio_at_admission_and_after_setters
    skip 'media admission hooks are unproven on this SDK' unless TraceAI::Auto::MediaAdmission.verified?
    [1, 3].each do |limit|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
        OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
        starts = {}
        processor.define_singleton_method(:on_start) { |span, _parent| starts[span.name] = span.attributes }
        provider = TraceAI::Auto.init(sample_rate: 0.25, processor: processor)
        tracer = provider.tracer('non-wire-ratio-test')
        candidates = #{limit}.times.to_h { |i| ["existing_\#{i}", 'kept'] }.freeze
        original = candidates.dup
        root = tracer.start_span('root', with_parent: OpenTelemetry::Context.empty, attributes: candidates)
        local = OpenTelemetry::Trace.context_with_span(root, parent_context: OpenTelemetry::Context.empty)
        child = tracer.start_span('child', with_parent: local, attributes: candidates)
        remote = OpenTelemetry::Trace::SpanContext.new(trace_id: ['01' * 16].pack('H*'),
          span_id: ['02' * 8].pack('H*'), remote: true, trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED,
          tracestate: OpenTelemetry::Trace::Tracestate.from_string('other=kept,fi=r:0.5'))
        context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(remote),
          parent_context: OpenTelemetry::Context.empty)
        inherited = tracer.start_span('inherited', with_parent: context, attributes: candidates)
        [root, child, inherited].each do |span|
          span.set_attribute('later', 'kept')
          span['another'] = 'kept'
          span.add_attributes('batch_one' => 1, 'batch_two' => 2)
          span.finish
        end
        abort 'input attributes mutated' unless candidates == original
        # An unrelated host provider still uses native SDK admission and setters.
        host_memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        host = OpenTelemetry::SDK::Trace::TracerProvider.new
        host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(host_memory))
        OpenTelemetry.tracer_provider = host
        TraceAI::Auto.adopt_host_provider
        abort 'host not adopted' unless TraceAI::Auto.provider.equal?(host)
        span = host.tracer('host').start_span('host', with_parent: context, attributes: candidates)
        span.set_attribute('later', 'kept')
        span.add_attributes('batch_one' => 1, 'batch_two' => 2)
        span.finish
        abort 'flush failed' unless TraceAI::Auto.flush
        puts JSON.generate(starts: starts, spans: memory.finished_spans.to_h { |record| [record.name,
          {attributes: record.attributes, state: record.tracestate.to_s}] }, host: host_memory.finished_spans.first.attributes)
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil,
        'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => limit.to_s})
      assert status.success?, stderr
      result = JSON.parse(stdout)
      {'root' => 0.25, 'child' => 0.25, 'inherited' => 0.5}.each do |name, rate|
        assert_equal rate, result.fetch('starts').fetch(name)['fi.sampling.ratio'], "on_start #{name}/#{limit}"
        values = result.fetch('spans').fetch(name)
        assert_equal rate, values.fetch('attributes')['fi.sampling.ratio'], "finished #{name}/#{limit}"
        assert_equal limit, values.fetch('attributes').size
        assert_includes values.fetch('state').split(','), "fi=r:#{rate}"
      end
      assert_equal limit.times.to_h { |i| ["existing_#{i}", 'kept'] }, result.fetch('host')
      assert_empty traceai_diagnostics(stderr)
      assert_empty export_spans
    end
  end

  def test_sanitized_export_recovers_ratio_when_an_oversized_event_fails_closed
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      provider = TraceAI::Auto.init(sample_rate: 0.25, exporter: memory)
      tracer = provider.tracer('ratio-fallback-test')
      root = tracer.start_span('root', with_parent: OpenTelemetry::Context.empty,
        attributes: {'gen_ai.usage.input_tokens' => 7, 'output.value' => 'PRIVATE_CONTENT'})
      local = OpenTelemetry::Trace.context_with_span(root, parent_context: OpenTelemetry::Context.empty)
      child = tracer.start_span('child', with_parent: local, attributes: {'gen_ai.usage.input_tokens' => 7})
      huge = 'PRIVATE_EVENT' + 'x' * TraceAI::Auto::Privacy::CATALOG['limits']['max_text_chars']
      [root, child].each do |span|
        span.add_event('oversized', attributes: {'payload' => huge})
        span.finish
      end
      abort 'flush failed' unless TraceAI::Auto.flush
      # Recovery must not treat arbitrary values under the exact key as safe.
      invalid = [0.0, 1.0, -0.25, 2.0, Float::NAN, Float::INFINITY, '0.25', 'PRIVATE_RATIO', true, [0.25]]
      invalid_kept = invalid.map do |value|
        TraceAI::Auto::Privacy.safe_metadata({'fi.sampling.ratio' => value}).key?('fi.sampling.ratio')
      end
      puts JSON.generate(spans: memory.finished_spans.map { |record|
        {attributes: record.attributes, events: record.events, sampled: record.trace_flags.sampled?} }, invalid_kept: invalid_kept)
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil})
    assert status.success?, stderr
    result = JSON.parse(stdout)
    assert_equal 2, result.fetch('spans').length
    result.fetch('spans').each do |span|
      assert_equal({'gen_ai.usage.input_tokens' => 7, 'fi.sampling.ratio' => 0.25}, span.fetch('attributes'))
      assert_empty span.fetch('events')
      assert span.fetch('sampled')
    end
    refute result.fetch('invalid_kept').any?
    refute_includes stdout, 'PRIVATE_'
    assert_empty traceai_diagnostics(stderr)
    assert_empty export_spans
  end

  def test_unstamped_root_and_sampled_parent_preserve_the_wire_projection_slot
    [[0.25, true], [1.0, true], [1.0, false]].each do |rate, sampled_parent|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
        TraceAI::Auto.init(sample_rate: #{rate}, processor: processor)
        remote = OpenTelemetry::Trace::SpanContext.new(trace_id: ['01' * 16].pack('H*'),
          span_id: ['02' * 8].pack('H*'), remote: true, trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED,
          tracestate: OpenTelemetry::Trace::Tracestate.from_string('other=kept'))
        parent = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(remote),
          parent_context: OpenTelemetry::Context.empty)
        parent = OpenTelemetry::Context.empty unless #{sampled_parent}
        TraceAI::Auto.with_context(parent) do
          TraceAI::Auto.with_attributes('traceai.capture.dropped_content_count' => 7) do
            request = {method: 'POST', url: 'http://127.0.0.1:1234/v1/chat/completions',
              headers: {'content-type' => 'application/json'}, body: '{"model":"m","messages":[]}'}
            body = '{"choices":[{"message":{"role":"assistant","content":"answer"}}]}'
            response = {status: 200, headers: {'content-type' => 'application/json'}, body: body, size: body.bytesize}
            TraceAI::Auto.emit(request, response, TraceAI::Auto.current_context, Time.now)
          end
        end
        abort 'flush failed' unless TraceAI::Auto.flush
        puts JSON.generate(memory.finished_spans.map { |span| {attributes: span.attributes, state: span.tracestate.to_s} })
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil,
        'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '1'})
      assert status.success?, stderr
      spans = JSON.parse(stdout)
      assert_equal 1, spans.length
      values = spans.first.fetch('attributes')
      assert_equal 1, values.size
      assert_operator values.fetch('traceai.capture.dropped_content_count'), :>=, 7
      refute values.key?('fi.sampling.ratio')
      assert_equal(sampled_parent ? 'other=kept' : '', spans.first.fetch('state'))
      assert_empty traceai_diagnostics(stderr)
      assert_empty export_spans
    end
  end

  def test_ratio_admission_preserves_native_ai_filtering_and_coverage
    skip 'media admission hooks are unproven on this SDK' unless TraceAI::Auto::MediaAdmission.verified?
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      provider = TraceAI::Auto.init(sample_rate: 0.25)
      tracer = provider.tracer('native-ai')
      root = tracer.start_span('native-root', with_parent: OpenTelemetry::Context.empty,
        attributes: {'gen_ai.span.kind' => 'LLM'})
      parent = OpenTelemetry::Trace.context_with_span(root, parent_context: OpenTelemetry::Context.empty)
      nested = tracer.start_span('nested', with_parent: parent)
      context = OpenTelemetry::Trace.context_with_span(nested, parent_context: OpenTelemetry::Context.empty)
      allowed = TraceAI::Auto.with_context(context) { TraceAI::Auto.capture_allowed? }
      nested.finish
      root.finish
      abort 'flush failed' unless TraceAI::Auto.flush
      puts JSON.generate(capture_allowed: allowed)
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '1'})
    assert status.success?, stderr
    refute JSON.parse(stdout).fetch('capture_allowed'), 'native model coverage must suppress wire duplicates'
    spans = export_spans.map(&:first)
    assert_equal ['native-root'], spans.map(&:name)
    assert_equal({'fi.sampling.ratio' => 0.25}, attrs(spans.first.attributes))
    assert_empty traceai_diagnostics(stderr)
  end

  def test_ratio_restoration_keeps_original_media_admission_evidence
    skip 'media admission hooks are unproven on this SDK' unless TraceAI::Auto::MediaAdmission.verified?
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      provider = TraceAI::Auto.init(sample_rate: 0.25, exporter: memory)
      span = provider.tracer('native-media').start_span('media', with_parent: OpenTelemetry::Context.empty,
        attributes: {'blob.id.content.chunk' => JSON.generate({'private' => 'x' * 100}),
          'ordinary' => 'safe sentence', 'filler' => 'safe sentence',
          'blob.id.mimeType' => 'application/octet-stream'})
      span.add_event('safe-event', attributes: {'ordinary' => 'safe sentence'})
      span.finish
      abort 'flush failed' unless TraceAI::Auto.flush
      puts JSON.generate(memory.finished_spans.map { |record| {attributes: record.attributes,
        events: record.events.map { |event| {name: event.name, attributes: event.attributes} }} })
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil,
      'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '3', 'OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT' => '32'})
    assert status.success?, stderr
    spans = JSON.parse(stdout)
    assert_equal 1, spans.length
    values = spans.first.fetch('attributes')
    assert_equal 'safe sentence', values.fetch('ordinary', nil)
    assert_equal 0.25, values.fetch('fi.sampling.ratio')
    assert_equal '<omitted: media>', values.fetch('blob.id.content.chunk')
    assert_equal [{'name' => 'safe-event', 'attributes' => {'ordinary' => 'safe sentence'}}], spans.first.fetch('events')
    assert_empty traceai_diagnostics(stderr)
    assert_empty export_spans
  end

  def assert_parent_ratio_copy(spans)
    %w[remote_half remote_half_child local_half].each do |name|
      span = spans.fetch(name)
      assert span.fetch('attributes').key?('fi.sampling.ratio'), "#{name}: inherited ratio missing"
      ratio = span.fetch('attributes').fetch('fi.sampling.ratio')
      assert_instance_of Float, ratio, name
      assert_equal 0.5, ratio, name
      assert_equal 'other=kept,fi=r:0.5', span.fetch('state'), name
      assert span.fetch('sampled'), name
    end
    refute spans.key?('remote_unsampled')
    %w[remote_missing remote_large remote_text remote_nan remote_infinite remote_overflow remote_zero remote_one local_missing].each do |name|
      refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), name
      assert_includes spans.fetch(name).fetch('state').split(','), 'other=kept', name
    end
  end

  def test_default_parentbased_sampler_copies_remote_ratio_to_local_descendants
    # Explicitly unset both sampler sources: this is the cross-language default.
    spans, stderr = ratio_probe('FI_SAMPLE_RATE' => nil, 'OTEL_TRACES_SAMPLER' => nil,
                               'OTEL_TRACES_SAMPLER_ARG' => nil)
    assert_parent_ratio_copy(spans)
    %w[root local_child].each do |name|
      refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), name
      assert_equal '', spans.fetch(name).fetch('state'), name
    end
    refute spans.fetch('invalid_parent').fetch('attributes').key?('fi.sampling.ratio')
    assert_empty traceai_diagnostics(stderr)
  end

  def test_full_rate_parentbased_samplers_copy_upstream_ratio_without_stamping_roots
    [
      {'OTEL_TRACES_SAMPLER' => 'parentbased_always_on'},
      {'FI_SAMPLE_RATE' => '1'},
      {'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '1'}
    ].each do |env|
      spans, stderr = ratio_probe(env)
      assert_parent_ratio_copy(spans)
      %w[root local_child].each do |name|
        refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), "#{env}/#{name}"
        assert_equal '', spans.fetch(name).fetch('state'), "#{env}/#{name}"
      end
      refute spans.fetch('invalid_parent').fetch('attributes').key?('fi.sampling.ratio'), env.inspect
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_zero_root_parentbased_samplers_still_copy_sampled_parent_ratio
    [
      {'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0'},
      {'OTEL_TRACES_SAMPLER' => 'parentbased_always_off'},
      {'FI_SAMPLE_RATE' => '0'}
    ].each do |env|
      spans, stderr = ratio_probe(env)
      assert_parent_ratio_copy(spans)
      %w[root local_child invalid_parent].each { |name| refute spans.key?(name), "#{env}/#{name}" }
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_fractional_parentbased_root_and_local_child_share_ratio
    spans, stderr = ratio_probe({}, options: {sample_rate: 0.25})
    %w[root local_child invalid_parent].each do |name|
      assert_equal 0.25, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_includes spans.fetch(name).fetch('state').split(','), 'fi=r:0.25'
      assert spans.fetch(name).fetch('sampled')
    end
    %w[remote_half local_half].each do |name|
      assert_equal 0.5, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio')
      assert_equal 'other=kept,fi=r:0.5', spans.fetch(name).fetch('state')
      assert_equal 'kept', spans.fetch(name).fetch('attributes').fetch('existing')
    end
    refute spans.key?('remote_unsampled')
    assert_empty traceai_diagnostics(stderr)
  end

  def test_ratio_tracestate_uses_round_trip_decimal_without_truncation
    rate = 0.12345678901234568
    spans, stderr = ratio_probe({}, options: {sample_rate: rate})
    assert_equal rate, spans.fetch('root').fetch('attributes').fetch('fi.sampling.ratio')
    assert_equal "fi=r:#{rate}", spans.fetch('root').fetch('state')
    assert_equal rate, Float(spans.fetch('root').fetch('state').delete_prefix('fi=r:'))
    assert_empty traceai_diagnostics(stderr)
  end

  def test_parentbased_sampled_parent_without_valid_ratio_is_not_stamped
    spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25')
    %w[remote_missing remote_large remote_text remote_nan remote_infinite remote_overflow remote_zero remote_one local_missing].each do |name|
      refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), name
      assert_includes spans.fetch(name).fetch('state').split(','), 'other=kept'
    end
    assert_equal 'other=kept,fi=r:2', spans.fetch('remote_large').fetch('state')
    assert_equal 'other=kept,fi=r:abc', spans.fetch('remote_text').fetch('state')
    assert_empty traceai_diagnostics(stderr)
  end

  def test_nonparent_ratio_uses_configured_rate_and_replaces_only_fi
    spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25')
    spans.each do |name, span|
      assert_equal 0.25, span.fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal 1, span.fetch('state').split(',').count { |entry| entry.start_with?('fi=') }
      assert_includes span.fetch('state').split(','), 'fi=r:0.25'
      if %w[root local_child invalid_parent].include?(name)
        assert_equal 'fi=r:0.25', span.fetch('state'), name
      else
        assert_includes span.fetch('state').split(','), 'other=kept', name
      end
    end
    assert spans.key?('remote_unsampled')
    assert_empty traceai_diagnostics(stderr)
  end

  # W3C caps tracestate at 32 members; a vendor adding its key to a full list
  # evicts the right-most member. Exercised through real SDK spans and parents.
  def full_tracestate_probe(env = {}, options: {})
    code = <<~'RUBY'.sub('OPTIONS', options.inspect)
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'json'
      OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { ['01' * 16].pack('H*') }
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
      provider = TraceAI::Auto.init(**OPTIONS.merge(processor: processor))
      tracer = provider.tracer('ratio-test')
      members = (0...32).map { |i| "v#{i}=x#{i}" }
      {
        'full_without_fi' => members,
        'full_with_fi' => members.first(5) + ['fi=r:0.5'] + members[5...31],
        'almost_full' => members.first(31),
        'short_with_fi' => members.first(3) + ['fi=r:0.5'] + members[3...6],
        'invalid_full' => members
      }.each do |name, state|
        parent = OpenTelemetry::Trace::SpanContext.new(
          trace_id: name == 'invalid_full' ? "\0".b * 16 : ['01' * 16].pack('H*'),
          span_id: ['02' * 8].pack('H*'), remote: true,
          trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED,
          tracestate: OpenTelemetry::Trace::Tracestate.from_string(state.join(',')))
        context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(parent),
          parent_context: OpenTelemetry::Context.empty)
        span = tracer.start_span(name, with_parent: context)
        child = OpenTelemetry::Trace.context_with_span(span, parent_context: OpenTelemetry::Context.empty)
        tracer.start_span(name + '_child', with_parent: child).finish
        span.finish
      end
      TraceAI::Auto.flush
      puts JSON.generate(memory.finished_spans.to_h { |span| [span.name,
        {attributes: span.attributes, state: span.tracestate.to_s}] })
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil}.merge(env))
    assert status.success?, stderr
    assert_empty export_spans
    [JSON.parse(stdout), stderr]
  end

  def test_nonparent_ratio_evicts_only_the_right_most_member_to_add_fi_to_a_full_tracestate
    spans, stderr = full_tracestate_probe({'OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25'})
    members = (0...32).map { |i| "v#{i}=x#{i}" }
    full = ['fi=r:0.25'] + members.first(31)
    %w[full_without_fi full_without_fi_child].each do |name|
      assert_equal 0.25, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal full.join(','), spans.fetch(name).fetch('state'), name
    end
    # W3C: a modified key MUST move to the left and a new key SHOULD be added
    # at the left, so fi is always first and the other members keep their order.
    %w[full_with_fi full_with_fi_child almost_full almost_full_child].each do |name|
      assert_equal 0.25, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal full.join(','), spans.fetch(name).fetch('state'), name
    end
    short = ['fi=r:0.25'] + members.first(6)
    %w[short_with_fi short_with_fi_child].each do |name|
      assert_equal 0.25, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal short.join(','), spans.fetch(name).fetch('state'), name
    end
    assert_equal 'fi=r:0.25', spans.fetch('invalid_full').fetch('state')
    assert_equal 'fi=r:0.25', spans.fetch('invalid_full_child').fetch('state')
    assert_empty traceai_diagnostics(stderr)
  end

  def test_parentbased_ratio_resets_full_invalid_parent_state_and_copies_full_valid_parent_state
    spans, stderr = full_tracestate_probe({}, options: {sample_rate: 0.25})
    members = (0...32).map { |i| "v#{i}=x#{i}" }
    assert_equal 'fi=r:0.25', spans.fetch('invalid_full').fetch('state')
    assert_equal 'fi=r:0.25', spans.fetch('invalid_full_child').fetch('state')
    assert_equal 0.25, spans.fetch('invalid_full').fetch('attributes').fetch('fi.sampling.ratio')
    with_fi = (members.first(5) + ['fi=r:0.5'] + members[5...31]).join(',')
    %w[full_with_fi full_with_fi_child].each do |name|
      assert_equal 0.5, spans.fetch(name).fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal with_fi, spans.fetch(name).fetch('state'), name
    end
    %w[full_without_fi full_without_fi_child].each do |name|
      refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), name
      assert_equal members.join(','), spans.fetch(name).fetch('state'), name
    end
    assert_empty traceai_diagnostics(stderr)
  end

  def test_nonparent_full_rate_and_off_samplers_never_copy_upstream_ratio
    %w[traceidratio always_on always_off].each do |name|
      spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '1')
      spans.each_value { |span| refute span.fetch('attributes').key?('fi.sampling.ratio'), name }
      if name == 'always_off'
        assert_empty spans
      else
        assert_equal '', spans.fetch('root').fetch('state')
        %w[remote_half remote_half_child local_half remote_unsampled].each do |span|
          assert_equal 'other=kept,fi=r:0.5', spans.fetch(span).fetch('state'), "#{name}/#{span}"
        end
      end
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_invalid_sampler_fallback_copies_upstream_ratio_but_does_not_stamp_roots
    spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => 'unsupported')
    assert_parent_ratio_copy(spans)
    refute spans.fetch('root').fetch('attributes').key?('fi.sampling.ratio')
    assert_equal '', spans.fetch('root').fetch('state')
    assert_equal 1, traceai_diagnostics(stderr).length
    assert_includes traceai_diagnostics(stderr).join, 'invalid OTEL_TRACES_SAMPLER'
  end

  def test_sampling_config_does_not_stamp_application_owned_provider
    [{}, {sample_rate: 0.25}, {sample_rate: 1.0}].each do |options|
      spans, stderr = ratio_probe({}, options: options, host: true)
      spans.each_value { |span| refute span.fetch('attributes').key?('fi.sampling.ratio'), options.inspect }
      assert_equal '', spans.fetch('root').fetch('state')
      assert_equal 'other=kept,fi=r:0.5', spans.fetch('remote_half').fetch('state')
      assert_equal 'other=kept,fi=r:0.5', spans.fetch('remote_half_child').fetch('state')
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_ratio_wrapper_preserves_delegate_result_and_arguments
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/sampling'
      samplers = OpenTelemetry::SDK::Trace::Samplers
      arguments = {trace_id: ['01' * 16].pack('H*'), parent_context: OpenTelemetry::Context.empty,
        links: [], name: 'test', kind: :client, attributes: {'input' => 'kept'}}
      state = OpenTelemetry::Trace::Tracestate.from_string('other=kept,fi=r:0.5')
      [samplers::Decision::DROP, samplers::Decision::RECORD_ONLY, samplers::Decision::RECORD_AND_SAMPLE].each do |decision|
        original = samplers::Result.new(decision: decision, attributes: {'delegate' => 'kept'}, tracestate: state)
        delegate = Object.new
        delegate.define_singleton_method(:should_sample?) do |**received|
          abort 'arguments changed' unless received == arguments
          original
        end
        delegate.define_singleton_method(:description) { 'delegate' }
        wrapper = TraceAI::Auto::RatioSampler.new(delegate, 0.25, parent_based: false)
        result = wrapper.should_sample?(**arguments)
        abort 'description changed' unless wrapper.description == 'delegate'
        abort 'decision changed' unless [result.sampled?, result.recording?] == [original.sampled?, original.recording?]
        abort 'attributes lost' unless result.attributes['delegate'] == 'kept'
        abort 'delegate mutated' if original.attributes.key?('fi.sampling.ratio')
        if decision == samplers::Decision::RECORD_AND_SAMPLE
          abort 'not double' unless result.attributes['fi.sampling.ratio'].is_a?(Float)
          abort 'ratio missing' unless result.attributes['fi.sampling.ratio'] == 0.25
          abort 'root inherited delegate state' unless result.tracestate.to_s == 'fi=r:0.25'
        else
          abort 'non-sampled result replaced' unless result.equal?(original)
        end
      end
      puts 'DELEGATED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "DELEGATED\n", stdout
    assert_empty traceai_diagnostics(stderr)
  end

  def test_parentbased_endpoint_wrappers_preserve_drop_and_record_only_results
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/sampling'
      samplers = OpenTelemetry::SDK::Trace::Samplers
      state = OpenTelemetry::Trace::Tracestate.from_string('other=kept,fi=r:0.5')
      parent = OpenTelemetry::Trace::SpanContext.new(trace_id: ['01' * 16].pack('H*'),
        span_id: ['02' * 8].pack('H*'), remote: true, trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED,
        tracestate: state)
      context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(parent),
        parent_context: OpenTelemetry::Context.empty)
      arguments = {trace_id: parent.trace_id, parent_context: context, links: [], name: 'test',
        kind: :client, attributes: {'input' => 'kept'}}
      [0.0, 1.0].each do |rate|
        [samplers::Decision::DROP, samplers::Decision::RECORD_ONLY, samplers::Decision::RECORD_AND_SAMPLE].each do |decision|
          original = samplers::Result.new(decision: decision, attributes: {'delegate' => 'kept'}, tracestate: state)
          delegate = Object.new
          delegate.define_singleton_method(:should_sample?) do |**received|
            abort 'arguments changed' unless received == arguments
            original
          end
          delegate.define_singleton_method(:description) { 'delegate' }
          wrapper = TraceAI::Auto::RatioSampler.new(delegate, rate, parent_based: true)
          root = OpenTelemetry::Trace.current_span(OpenTelemetry::Context.empty).context
          abort 'root endpoint eligible' unless wrapper.ratio_for(root).nil?
          result = wrapper.should_sample?(**arguments)
          abort 'description changed' unless wrapper.description == 'delegate'
          abort 'delegate mutated' if original.attributes.key?('fi.sampling.ratio')
          if decision == samplers::Decision::RECORD_AND_SAMPLE
            abort 'delegate metadata lost' unless result.attributes == {'delegate' => 'kept', 'fi.sampling.ratio' => 0.5}
            abort 'decision changed' unless result.sampled? && result.recording?
            abort 'tracestate replaced' unless result.tracestate.equal?(state)
          else
            abort 'non-sampled result replaced' unless result.equal?(original)
          end
        end
      end
      puts 'DELEGATED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "DELEGATED\n", stdout
    assert_empty traceai_diagnostics(stderr)
  end

  def test_invalid_parent_state_rebasing_preserves_delegate_decision_attributes_and_arguments
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/sampling'
      samplers = OpenTelemetry::SDK::Trace::Samplers
      state = OpenTelemetry::Trace::Tracestate.from_string('other=kept,fi=r:0.5,another=retained')
      [false, true].each do |zero_trace|
        parent = OpenTelemetry::Trace::SpanContext.new(
          trace_id: zero_trace ? "\0".b * 16 : ['03' * 16].pack('H*'),
          span_id: zero_trace ? ['02' * 8].pack('H*') : "\0".b * 8,
          trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED, tracestate: state)
        context = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(parent),
          parent_context: OpenTelemetry::Context.empty)
        arguments = {trace_id: ['01' * 16].pack('H*'), parent_context: context,
          links: [], name: 'test', kind: :client, attributes: {'input' => 'kept'}}
        [false, true].each do |parent_based|
          [0.0, 0.25, 1.0].each do |rate|
            [samplers::Decision::DROP, samplers::Decision::RECORD_ONLY, samplers::Decision::RECORD_AND_SAMPLE].each do |decision|
              attributes = {'delegate' => 'kept'}.freeze
              original = samplers::Result.new(decision: decision, attributes: attributes, tracestate: state)
              delegate = Object.new
              delegate.define_singleton_method(:description) { 'delegate' }
              delegate.define_singleton_method(:should_sample?) do |**received|
                abort 'arguments changed' unless received == arguments
                original
              end
              wrapper = TraceAI::Auto::RatioSampler.new(delegate, rate, parent_based: parent_based)
              result = wrapper.should_sample?(**arguments)
              abort 'description changed' unless wrapper.description == 'delegate'
              abort 'decision changed' unless [result.sampled?, result.recording?] == [original.sampled?, original.recording?]
              expected = result.sampled? && rate == 0.25 ? {'delegate' => 'kept', 'fi.sampling.ratio' => 0.25} : attributes
              abort 'attributes changed' unless result.attributes == expected
              expected_state = result.sampled? && rate == 0.25 ? 'fi=r:0.25' : ''
              abort 'invalid state leaked' unless result.tracestate.to_s == expected_state
              abort 'input state mutated' unless parent.tracestate.equal?(state) && original.tracestate.equal?(state)
              abort 'input attributes mutated' unless original.attributes.equal?(attributes)
            end
          end
        end
      end
      puts 'REBASED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "REBASED\n", stdout
    assert_empty traceai_diagnostics(stderr)
  end

  def assert_decisions(result, root:, sampled_parent:, unsampled_parent:)
    expected = {'root' => root, 'local_sampled' => sampled_parent, 'remote_sampled' => sampled_parent,
                'local_unsampled' => unsampled_parent, 'remote_unsampled' => unsampled_parent}
    assert_equal expected.transform_values { |sampled| [sampled, sampled] }, result.fetch('decisions')
    assert_equal expected.select { |_name, sampled| sampled }.keys.sort, result.fetch('exported')
    assert result.fetch('exported_flags').all?, 'export changed a sampled flag'
  end

  def test_env_always_off_drops_roots_and_sampled_parents_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'always_off')
    assert_decisions(result, root: false, sampled_parent: false, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_env_always_on_samples_even_an_unsampled_parents_child
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'always_on')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: true)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_env_traceidratio_zero_drops_roots_and_sampled_parents_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0')
    assert_decisions(result, root: false, sampled_parent: false, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_env_parentbased_traceidratio_zero_keeps_sampled_parent_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0')
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_env_parentbased_always_off_keeps_sampled_parent_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'parentbased_always_off')
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_env_parentbased_always_on_and_unset_keep_default_parent_decisions
    [nil, '', 'parentbased_always_on'].each do |name|
      result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => name)
      assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_ratio_without_argument_defaults_to_one_and_ignores_parent_sampling
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'traceidratio')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: true)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_explicit_fi_rate_wins_over_env_sampler_in_both_directions
    [['1', 'always_off', true], ['0', 'always_on', false]].each do |rate, name, sampled|
      result, stderr = sdk_probe('FI_SAMPLE_RATE' => rate, 'OTEL_TRACES_SAMPLER' => name)
      assert_decisions(result, root: sampled, sampled_parent: true, unsampled_parent: false)
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_init_rate_wins_over_fi_rate_and_invalid_otel_configuration
    result, stderr = sdk_probe({'FI_SAMPLE_RATE' => '0', 'OTEL_TRACES_SAMPLER' => 'unsupported',
                               'OTEL_TRACES_SAMPLER_ARG' => 'not-a-number'}, options: {sample_rate: 1.0})
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_config_file_rate_is_explicit_even_when_it_equals_default
    file = Tempfile.new(['sampler', '.toml'], File.join(ROOT, '.bundle/tmp'))
    file.write("sample_rate = 1.0\n"); file.close
    result, stderr = sdk_probe('FI_CONFIG_FILE' => file.path, 'OTEL_TRACES_SAMPLER' => 'always_off')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  ensure
    file&.unlink
  end

  def test_invalid_sampler_falls_back_with_one_warning_across_provisional_init
    result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => 'unsupported'}, provisional: true)
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_equal 1, traceai_diagnostics(stderr).length
    assert_includes traceai_diagnostics(stderr).join, 'invalid OTEL_TRACES_SAMPLER'
    refute_includes traceai_diagnostics(stderr).join, 'unsupported'
  end

  def test_invalid_ratio_arguments_fall_back_to_parentbased_one_with_one_warning
    %w[traceidratio parentbased_traceidratio].each do |name|
      %w[not-a-number NaN Infinity -0.1 1.1].each do |argument|
        result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => argument)
        assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
        assert_equal 1, traceai_diagnostics(stderr).length, "#{name}/#{argument}"
        assert_includes traceai_diagnostics(stderr).join, 'OTEL_TRACES_SAMPLER_ARG'
        refute_includes traceai_diagnostics(stderr).join, argument
      end
    end
  end

  def test_empty_ratio_arguments_fall_back_once_and_drop_unsampled_parent_children
    %w[traceidratio parentbased_traceidratio].each do |name|
      result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => ''}, provisional: true)
      assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
      assert_equal 1, traceai_diagnostics(stderr).length, name
      assert_includes traceai_diagnostics(stderr).join, 'OTEL_TRACES_SAMPLER_ARG'
    end
  end

  def test_non_ratio_sampler_ignores_its_argument
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'always_off', 'OTEL_TRACES_SAMPLER_ARG' => 'not-a-number')
    assert_decisions(result, root: false, sampled_parent: false, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_supplied_exporter_receives_only_chosen_sampled_spans
    result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0'}, exporter: true)
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty traceai_diagnostics(stderr)
  end

  def test_application_owned_sampler_is_untouched_for_explicit_global_and_late_providers
    %w[explicit global late].each do |mode|
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'traceai/auto/agent'
        sampler = OpenTelemetry::SDK::Trace::Samplers::ALWAYS_OFF
        host = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: sampler)
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)
        mode = #{mode.inspect}
        OpenTelemetry.tracer_provider = host if mode == 'global'
        options = {processor: processor, sample_rate: 0.25}
        options[:provider] = host if mode == 'explicit'
        TraceAI::Auto.init(**options)
        if mode == 'late'
          OpenTelemetry.tracer_provider = host
          TraceAI::Auto.adopt_host_provider
        end
        abort 'host replaced' unless TraceAI::Auto.provider.equal?(host)
        abort 'sampler replaced' unless host.sampler.equal?(sampler)
        span = TraceAI::Auto.tracer.start_span('dropped')
        abort 'span recorded or marked sampled' if span.recording? || span.context.trace_flags.sampled?
        span.finish
        abort 'dropped span exported' unless memory.finished_spans.empty?
        puts 'HOST_UNCHANGED'
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil,
                                         'OTEL_TRACES_SAMPLER' => 'always_on'})
      assert status.success?, "#{mode}: #{stderr}"
      assert_equal "HOST_UNCHANGED\n", stdout
      assert_empty traceai_diagnostics(stderr)
      assert_empty export_spans
    end
  end

  def test_invalid_env_is_not_parsed_for_application_owned_provider
    code = <<~'RUBY'
      require 'opentelemetry/sdk'; require 'traceai/auto/agent'
      sampler = OpenTelemetry::SDK::Trace::Samplers::ALWAYS_ON
      host = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: sampler)
      TraceAI::Auto.init(provider: host)
      abort 'sampler replaced' unless host.sampler.equal?(sampler)
      puts 'HOST_UNCHANGED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil, 'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil,
                                       'OTEL_TRACES_SAMPLER' => 'unsupported'})
    assert status.success?, stderr
    assert_equal "HOST_UNCHANGED\n", stdout
    assert_empty traceai_diagnostics(stderr)
  end

  # The standalone path must keep working without selecting any SDK gems.
  def local_probe(env = {}, options: {}, trace_id: nil, random_draw: nil, sdk: false, **environment)
    env = env.merge(environment)
    code = <<~RUBY
      require 'opentelemetry/sdk' if #{sdk}
      require 'net/http'; require 'traceai/auto/agent'
      before = Gem.loaded_specs.transform_values(&:version)
      TraceAI::Auto.init(**#{options.inspect})
      abort 'not local' unless #{sdk} || TraceAI::Auto.provider.is_a?(TraceAI::Auto::LocalProvider)
      unless #{trace_id.nil?}
        controlled_id = [#{trace_id.inspect}].pack('H*')
        OpenTelemetry::Trace.define_singleton_method(:generate_trace_id) { controlled_id } if #{sdk}
        original_urandom = Random.method(:urandom)
        Random.define_singleton_method(:urandom) { |size| size == 16 ? controlled_id : original_urandom.call(size) }
      end
      Random.define_singleton_method(:rand) { #{random_draw.inspect} } unless #{random_draw.nil?}
      request = {method: 'POST', url: 'http://127.0.0.1:1234/v1/chat/completions',
        headers: {'content-type' => 'application/json'}, body: '{"model":"m","messages":[]}'}
      body = '{"choices":[{"message":{"role":"assistant","content":"answer"}}]}'
      response = {status: 200, headers: {'content-type' => 'application/json'}, body: body, size: body.bytesize}
      TraceAI::Auto.emit(request, response, TraceAI::Auto.current_context, Time.now)
      abort 'flush failed' unless TraceAI::Auto.flush
      abort 'agent activated gems' unless Gem.loaded_specs.transform_values(&:version) == before
      abort 'SDK loaded' if !#{sdk} && defined?(OpenTelemetry::SDK)
      puts 'LOCAL_DONE'
    RUBY
    standalone = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('RUBYOPT' => nil)
    stdout, stderr, status = app(code, standalone.merge(env), chdir: File.dirname(ROOT))
    assert status.success?, stderr
    assert_equal "LOCAL_DONE\n", stdout
    [export_spans.map(&:first), stderr]
  end

  def test_standalone_env_off_and_zero_ratio_drop_without_loading_sdk
    %w[always_off traceidratio parentbased_always_off parentbased_traceidratio].each do |name|
      spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '0')
      assert_empty spans, name
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def assert_exported_ratio(span, ratio)
    attribute = span.attributes.find { |entry| entry.key == 'fi.sampling.ratio' }
    refute_nil attribute
    assert_equal :double_value, attribute.value.value
    assert_equal ratio, attribute.value.double_value
    assert_equal "fi=r:#{ratio}", span.trace_state
  end

  def test_standalone_zero_attribute_limit_keeps_only_ratio_tracestate
    spans, stderr = local_probe({'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '0'},
                               options: {sample_rate: 0.25}, trace_id: '01' * 16, random_draw: 0.1)
    assert_equal 1, spans.length
    assert_empty spans.first.attributes
    assert_equal 'fi=r:0.25', spans.first.trace_state
    assert_equal 1, spans.first.flags & 1
    assert_empty traceai_diagnostics(stderr)
  end

  def test_wire_ratio_survives_a_saturated_positive_attribute_limit
    [false, true].each do |sdk|
      spans, stderr = local_probe({'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '1'},
                                 options: {sample_rate: 0.25}, sdk: sdk,
                                 trace_id: '01' * 16, random_draw: 0.1)
      assert_equal 1, spans.length
      assert_exported_ratio(spans.first, 0.25)
      assert_equal 1, spans.first.attributes.length
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_standalone_fractional_otel_ratios_sample_the_exported_trace_id
    # At 0.5 the SDK cutoff is 2**63, exclusive, using only the low 64 bits.
    # Opposing random draws make a probability-only implementation fail deterministically.
    cases = [
      ['ffffffffffffffff7fffffffffffffff', 0.75, true],
      ['00000000000000008000000000000000', 0.25, false],
      ['0000000000000000ffffffffffffffff', 0.25, false]
    ]
    %w[traceidratio parentbased_traceidratio].each do |name|
      cases.each do |trace_id, random_draw, sampled|
        spans, stderr = local_probe({'OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '0.5'},
                                   trace_id: trace_id, random_draw: random_draw)
        assert_equal(sampled ? 1 : 0, spans.length, "#{name}/#{trace_id}")
        if sampled
          assert_equal trace_id, spans.first.trace_id.unpack1('H*')
          assert_equal 1, spans.first.flags & 1
          assert_exported_ratio(spans.first, 0.5)
        end
        assert_empty traceai_diagnostics(stderr)
      end
    end
  end

  def test_standalone_otel_ratio_endpoints_follow_sdk_cutoffs
    %w[traceidratio parentbased_traceidratio].each do |name|
      [['0', 'ffffffffffffffff0000000000000000', false],
       ['1', 'ffffffffffffffffffffffffffffffff', true]].each do |argument, trace_id, sampled|
        spans, stderr = local_probe({'OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => argument}, trace_id: trace_id)
        assert_equal(sampled ? 1 : 0, spans.length)
        if sampled
          assert_equal trace_id, spans.first.trace_id.unpack1('H*')
          assert_equal 1, spans.first.flags & 1
          refute attrs(spans.first.attributes).key?('fi.sampling.ratio')
          assert_equal '', spans.first.trace_state
        end
        assert_empty traceai_diagnostics(stderr)
      end
    end
  end

  def test_standalone_explicit_fractional_rate_preserves_random_sampling
    [{sample_rate: 0.5}, {}].each do |options|
      env = {'OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.5'}
      env['FI_SAMPLE_RATE'] = '0.5' if options.empty?
      [['0000000000000000ffffffffffffffff', 0.25, true],
       ['ffffffffffffffff0000000000000001', 0.75, false]].each do |trace_id, random_draw, sampled|
        spans, stderr = local_probe(env, options: options, trace_id: trace_id, random_draw: random_draw)
        assert_equal(sampled ? 1 : 0, spans.length)
        if sampled
          assert_equal trace_id, spans.first.trace_id.unpack1('H*')
          assert_equal 1, spans.first.flags & 1
          assert_exported_ratio(spans.first, 0.5)
        end
        assert_empty traceai_diagnostics(stderr)
      end
    end
  end

  def test_standalone_always_on_variants_do_not_add_sampling_metadata
    %w[always_on parentbased_always_on].each do |name|
      spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => name)
      assert_equal 1, spans.length
      refute attrs(spans.first.attributes).key?('fi.sampling.ratio')
      assert_equal '', spans.first.trace_state
      assert_empty traceai_diagnostics(stderr)
    end
  end

  def test_standalone_explicit_rate_wins_and_exported_sampled_bit_is_honest
    spans, stderr = local_probe({'OTEL_TRACES_SAMPLER' => 'always_off'}, options: {sample_rate: 1.0})
    assert_equal 1, spans.length
    assert_equal 1, spans.first.flags & 1
    assert_empty traceai_diagnostics(stderr)
    spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => 'always_on', 'FI_SAMPLE_RATE' => '0')
    assert_empty spans
    assert_empty traceai_diagnostics(stderr)
  end

  def test_standalone_empty_ratio_argument_warns_once_and_uses_default
    %w[traceidratio parentbased_traceidratio].each do |name|
      spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '')
      assert_equal 1, spans.length
      assert_equal 1, spans.first.flags & 1
      assert_equal 1, traceai_diagnostics(stderr).length
      assert_includes traceai_diagnostics(stderr).join, 'OTEL_TRACES_SAMPLER_ARG'
    end
  end

  def test_standalone_invalid_env_warns_once_and_uses_default
    spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => 'unsupported')
    assert_equal 1, spans.length
    assert_equal 1, spans.first.flags & 1
    assert_equal 1, traceai_diagnostics(stderr).length
    assert_includes traceai_diagnostics(stderr).join, 'invalid OTEL_TRACES_SAMPLER'
  end
end
