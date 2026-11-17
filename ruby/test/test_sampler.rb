# frozen_string_literal: true
require_relative 'test_helper'
require 'tempfile'

class SamplerTest < Minitest::Test
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
        tracer.start_span(name, with_parent: context, attributes: {'existing' => 'kept'}).finish
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
    assert_empty stderr
  end

  def test_ratio_tracestate_uses_round_trip_decimal_without_truncation
    rate = 0.12345678901234568
    spans, stderr = ratio_probe({}, options: {sample_rate: rate})
    assert_equal rate, spans.fetch('root').fetch('attributes').fetch('fi.sampling.ratio')
    assert_equal "fi=r:#{rate}", spans.fetch('root').fetch('state')
    assert_equal rate, Float(spans.fetch('root').fetch('state').delete_prefix('fi=r:'))
    assert_empty stderr
  end

  def test_parentbased_sampled_parent_without_valid_ratio_is_not_stamped
    spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25')
    %w[remote_missing remote_large remote_text remote_nan remote_infinite remote_overflow remote_zero remote_one local_missing].each do |name|
      refute spans.fetch(name).fetch('attributes').key?('fi.sampling.ratio'), name
      assert_includes spans.fetch(name).fetch('state').split(','), 'other=kept'
    end
    assert_equal 'other=kept,fi=r:2', spans.fetch('remote_large').fetch('state')
    assert_equal 'other=kept,fi=r:abc', spans.fetch('remote_text').fetch('state')
    assert_empty stderr
  end

  def test_nonparent_ratio_uses_configured_rate_and_replaces_only_fi
    spans, stderr = ratio_probe('OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.25')
    spans.each do |name, span|
      assert_equal 0.25, span.fetch('attributes').fetch('fi.sampling.ratio'), name
      assert_equal 1, span.fetch('state').split(',').count { |entry| entry.start_with?('fi=') }
      assert_includes span.fetch('state').split(','), 'fi=r:0.25'
      assert_includes span.fetch('state').split(','), 'other=kept' unless %w[root local_child].include?(name)
    end
    assert spans.key?('remote_unsampled')
    assert_empty stderr
  end

  def test_ratio_one_and_nonratio_samplers_never_stamp
    %w[traceidratio parentbased_traceidratio always_on parentbased_always_on always_off parentbased_always_off unsupported].each do |name|
      spans, = ratio_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '1')
      spans.each_value { |span| refute span.fetch('attributes').key?('fi.sampling.ratio'), name }
      assert_equal '', spans.fetch('root').fetch('state') if spans.key?('root')
    end
  end

  def test_fractional_config_does_not_stamp_application_owned_provider
    spans, stderr = ratio_probe({}, options: {sample_rate: 0.25}, host: true)
    spans.each_value { |span| refute span.fetch('attributes').key?('fi.sampling.ratio') }
    assert_equal '', spans.fetch('root').fetch('state')
    assert_equal 'other=kept,fi=r:0.5', spans.fetch('remote_half').fetch('state')
    assert_empty stderr
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
          abort 'state changed' unless result.tracestate.to_s == 'other=kept,fi=r:0.25'
        else
          abort 'non-sampled result replaced' unless result.equal?(original)
        end
      end
      puts 'DELEGATED'
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr
    assert_equal "DELEGATED\n", stdout
    assert_empty stderr
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
    assert_empty stderr
  end

  def test_env_always_on_samples_even_an_unsampled_parents_child
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'always_on')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: true)
    assert_empty stderr
  end

  def test_env_traceidratio_zero_drops_roots_and_sampled_parents_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0')
    assert_decisions(result, root: false, sampled_parent: false, unsampled_parent: false)
    assert_empty stderr
  end

  def test_env_parentbased_traceidratio_zero_keeps_sampled_parent_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0')
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty stderr
  end

  def test_env_parentbased_always_off_keeps_sampled_parent_children
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'parentbased_always_off')
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty stderr
  end

  def test_env_parentbased_always_on_and_unset_keep_default_parent_decisions
    [nil, '', 'parentbased_always_on'].each do |name|
      result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => name)
      assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
      assert_empty stderr
    end
  end

  def test_ratio_without_argument_defaults_to_one_and_ignores_parent_sampling
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'traceidratio')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: true)
    assert_empty stderr
  end

  def test_explicit_fi_rate_wins_over_env_sampler_in_both_directions
    [['1', 'always_off', true], ['0', 'always_on', false]].each do |rate, name, sampled|
      result, stderr = sdk_probe('FI_SAMPLE_RATE' => rate, 'OTEL_TRACES_SAMPLER' => name)
      assert_decisions(result, root: sampled, sampled_parent: true, unsampled_parent: false)
      assert_empty stderr
    end
  end

  def test_init_rate_wins_over_fi_rate_and_invalid_otel_configuration
    result, stderr = sdk_probe({'FI_SAMPLE_RATE' => '0', 'OTEL_TRACES_SAMPLER' => 'unsupported',
                               'OTEL_TRACES_SAMPLER_ARG' => 'not-a-number'}, options: {sample_rate: 1.0})
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_empty stderr
  end

  def test_config_file_rate_is_explicit_even_when_it_equals_default
    file = Tempfile.new(['sampler', '.toml'], File.join(ROOT, '.bundle/tmp'))
    file.write("sample_rate = 1.0\n"); file.close
    result, stderr = sdk_probe('FI_CONFIG_FILE' => file.path, 'OTEL_TRACES_SAMPLER' => 'always_off')
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_empty stderr
  ensure
    file&.unlink
  end

  def test_invalid_sampler_falls_back_with_one_warning_across_provisional_init
    result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => 'unsupported'}, provisional: true)
    assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
    assert_equal 1, stderr.lines.length
    assert_includes stderr, 'invalid OTEL_TRACES_SAMPLER'
    refute_includes stderr, 'unsupported'
  end

  def test_invalid_ratio_arguments_fall_back_to_parentbased_one_with_one_warning
    %w[traceidratio parentbased_traceidratio].each do |name|
      %w[not-a-number NaN Infinity -0.1 1.1].each do |argument|
        result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => argument)
        assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
        assert_equal 1, stderr.lines.length, "#{name}/#{argument}"
        assert_includes stderr, 'OTEL_TRACES_SAMPLER_ARG'
        refute_includes stderr, argument
      end
    end
  end

  def test_empty_ratio_arguments_fall_back_once_and_drop_unsampled_parent_children
    %w[traceidratio parentbased_traceidratio].each do |name|
      result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => ''}, provisional: true)
      assert_decisions(result, root: true, sampled_parent: true, unsampled_parent: false)
      assert_equal 1, stderr.lines.length, name
      assert_includes stderr, 'OTEL_TRACES_SAMPLER_ARG'
    end
  end

  def test_non_ratio_sampler_ignores_its_argument
    result, stderr = sdk_probe('OTEL_TRACES_SAMPLER' => 'always_off', 'OTEL_TRACES_SAMPLER_ARG' => 'not-a-number')
    assert_decisions(result, root: false, sampled_parent: false, unsampled_parent: false)
    assert_empty stderr
  end

  def test_supplied_exporter_receives_only_chosen_sampled_spans
    result, stderr = sdk_probe({'OTEL_TRACES_SAMPLER' => 'parentbased_traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0'}, exporter: true)
    assert_decisions(result, root: false, sampled_parent: true, unsampled_parent: false)
    assert_empty stderr
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
      assert_empty stderr
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
    assert_empty stderr
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
      assert_empty stderr
    end
  end

  def assert_exported_ratio(span, ratio)
    attribute = span.attributes.find { |entry| entry.key == 'fi.sampling.ratio' }
    refute_nil attribute
    assert_equal :double_value, attribute.value.value
    assert_equal ratio, attribute.value.double_value
    assert_equal "fi=r:#{ratio}", span.trace_state
  end

  def test_wire_ratio_survives_a_saturated_positive_attribute_limit
    [false, true].each do |sdk|
      spans, stderr = local_probe({'OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT' => '1'},
                                 options: {sample_rate: 0.25}, sdk: sdk,
                                 trace_id: '01' * 16, random_draw: 0.1)
      assert_equal 1, spans.length
      assert_exported_ratio(spans.first, 0.25)
      assert_equal 1, spans.first.attributes.length
      assert_empty stderr
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
        assert_empty stderr
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
        assert_empty stderr
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
        assert_empty stderr
      end
    end
  end

  def test_standalone_always_on_variants_do_not_add_sampling_metadata
    %w[always_on parentbased_always_on].each do |name|
      spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => name)
      assert_equal 1, spans.length
      refute attrs(spans.first.attributes).key?('fi.sampling.ratio')
      assert_equal '', spans.first.trace_state
      assert_empty stderr
    end
  end

  def test_standalone_explicit_rate_wins_and_exported_sampled_bit_is_honest
    spans, stderr = local_probe({'OTEL_TRACES_SAMPLER' => 'always_off'}, options: {sample_rate: 1.0})
    assert_equal 1, spans.length
    assert_equal 1, spans.first.flags & 1
    assert_empty stderr
    spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => 'always_on', 'FI_SAMPLE_RATE' => '0')
    assert_empty spans
    assert_empty stderr
  end

  def test_standalone_empty_ratio_argument_warns_once_and_uses_default
    %w[traceidratio parentbased_traceidratio].each do |name|
      spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => name, 'OTEL_TRACES_SAMPLER_ARG' => '')
      assert_equal 1, spans.length
      assert_equal 1, spans.first.flags & 1
      assert_equal 1, stderr.lines.length
      assert_includes stderr, 'OTEL_TRACES_SAMPLER_ARG'
    end
  end

  def test_standalone_invalid_env_warns_once_and_uses_default
    spans, stderr = local_probe('OTEL_TRACES_SAMPLER' => 'unsupported')
    assert_equal 1, spans.length
    assert_equal 1, spans.first.flags & 1
    assert_equal 1, stderr.lines.length
    assert_includes stderr, 'invalid OTEL_TRACES_SAMPLER'
  end
end
