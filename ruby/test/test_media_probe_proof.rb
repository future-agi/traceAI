# frozen_string_literal: true
require 'minitest/mock'
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/media_admission'

# A supported OpenTelemetry SDK must prove the media hooks, and a probe that cannot run must fail the
# suite instead of skipping the content tests that depend on it.
class MediaProbeProofTest < Minitest::Test
  M = TraceAI::Auto::MediaAdmission
  Helper = SkipWhenMediaHooksUnproven
  Status = Struct.new(:ok) { def success? = ok }
  OK = Status.new(true)
  FAILED = Status.new(false)

  def test_the_probe_output_true_and_false_are_read_as_proven_and_unproven
    assert_equal true, Helper.interpret('true', '', OK)
    assert_equal false, Helper.interpret('false', '', OK)
  end

  def test_a_probe_that_exits_non_zero_is_an_error_not_unproven
    error = assert_raises(RuntimeError) { Helper.interpret('', 'LoadError: cannot load such file', FAILED) }
    assert_includes error.message, 'probe'
  end

  def test_a_probe_that_writes_to_stderr_is_an_error_not_unproven
    assert_raises(RuntimeError) { Helper.interpret('true', 'warning: something unexpected', OK) }
  end

  def test_a_probe_that_prints_anything_else_is_an_error_not_unproven
    ['', 'true ', "true\n", 'TRUE', 'maybe', 'truefalse'].each do |out|
      assert_raises(RuntimeError, out.inspect) { Helper.interpret(out, '', OK) }
    end
  end

  def test_the_flag_makes_an_unproven_sdk_fail_only_where_the_sdk_can_be_proven
    with_remote_flag = [:name, :parent_span_is_remote]
    without = [:name]
    assert_equal :fail, Helper.unproven_outcome({'TRACEAI_EXPECT_MEDIA_PROVEN' => '1'}, with_remote_flag)
    assert_equal :skip, Helper.unproven_outcome({'TRACEAI_EXPECT_MEDIA_PROVEN' => '1'}, without)
    assert_equal :skip, Helper.unproven_outcome({}, with_remote_flag)
    assert_equal :skip, Helper.unproven_outcome({'TRACEAI_EXPECT_MEDIA_PROVEN' => '0'}, with_remote_flag)
  end

  def test_the_hooks_are_proven_on_an_sdk_whose_span_data_reports_remote_parents
    members = OpenTelemetry::SDK::Trace::SpanData.members
    skip 'this SDK predates parent_span_is_remote; the hooks are expected to be unproven' unless members.include?(:parent_span_is_remote)
    assert M::Probe.run, 'the media hooks must be proven on a supported OpenTelemetry SDK'
    assert Helper.proven?, 'the child-process probe must agree'
  end

  def test_the_warning_says_that_content_events_and_links_are_not_exported
    lines = []
    original = M.instance_variable_get(:@reported)
    M.instance_variable_set(:@reported, false)
    M.stub(:warn, ->(message) { lines << message }) { M.send(:report_unverified) }
    assert_equal 1, lines.size
    %w[content events links].each { |word| assert_includes lines.first, word }
    refute_match(/only media|media is omitted/, lines.first)
  ensure
    M.instance_variable_set(:@reported, original)
  end

  DOCS = {
    'README' => File.expand_path('../README.md', __dir__),
    'ruby CHANGELOG' => File.expand_path('../CHANGELOG.md', __dir__),
    'root CHANGELOG' => File.expand_path('../../CHANGELOG.md', __dir__)
  }.freeze

  def test_every_document_says_what_an_unverifiable_sdk_loses
    DOCS.each do |name, path|
      text = File.read(path).gsub(/\s+/, ' ')
      sentence = text[/[^.]*bookkeeping hooks[^.]*\.(?:[^.]*\.){0,2}/].to_s
      refute_empty sentence, "#{name} no longer describes the startup probe"
      %w[content events links].each { |word| assert_includes sentence, word, "#{name}: #{sentence}" }
      refute_match(/omits? media from them|media is omitted from them/, sentence, name)
    end
  end

  def test_the_probe_leaves_the_live_span_class_and_thread_state_unchanged
    span = OpenTelemetry::SDK::Trace::Span
    before = [span.ancestors, span.instance_methods(false).sort, span.private_instance_methods(false).sort,
              M.instance_methods.sort, M.private_instance_methods.sort]
    M::Probe.run
    after = [span.ancestors, span.instance_methods(false).sort, span.private_instance_methods(false).sort,
             M.instance_methods.sort, M.private_instance_methods.sort]
    assert_equal before, after
    assert_nil Thread.current[M::PROBE_KEY]
  end
end
