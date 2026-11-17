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
        options = {processor: processor, sample_rate: 1.0}
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
  def local_probe(env = {}, options: {}, trace_id: nil, random_draw: nil, **environment)
    env = env.merge(environment)
    code = <<~RUBY
      require 'net/http'; require 'traceai/auto/agent'
      before = Gem.loaded_specs.transform_values(&:version)
      TraceAI::Auto.init(**#{options.inspect})
      abort 'not local' unless TraceAI::Auto.provider.is_a?(TraceAI::Auto::LocalProvider)
      unless #{trace_id.nil?}
        controlled_id = [#{trace_id.inspect}].pack('H*')
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
      abort 'SDK loaded' if defined?(OpenTelemetry::SDK)
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
        end
        assert_empty stderr
      end
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
