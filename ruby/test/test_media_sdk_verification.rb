# frozen_string_literal: true
require 'minitest/mock'
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/export'
require_relative '../lib/traceai/auto/media_admission'

# The media bookkeeping hooks private OpenTelemetry SDK span internals. They are armed only when a
# probe has proven them on the running SDK; otherwise every covered span exports without media.
class MediaSdkVerificationTest < Minitest::Test
  A = TraceAI::Auto
  P = A::Privacy
  M = A::MediaAdmission
  PAYLOAD = 'AQIDBA=='
  CONFIG = A::Config.new({}, env: {})

  def setup
    OpenTelemetry::SDK::Trace::Span.prepend(M) unless OpenTelemetry::SDK::Trace::Span.ancestors.include?(M)
  end

  def context
    OpenTelemetry::Trace::SpanContext.new(trace_id: "\1" * 16, span_id: "\2" * 8)
  end

  def media(prefix = 'gen_ai.input.messages.0.audio')
    {prefix + '.content.chunk' => PAYLOAD, prefix + '.mime_type' => 'audio/pcm', 'ordinary' => 'safe sentence',
     'arbitrary.magic' => ['iVBORw0KGgoAAAAA']}
  end

  def finished_span(count = nil)
    limits = OpenTelemetry::SDK::Trace::SpanLimits.new(**(count ? {attribute_count_limit: count} : {}))
    host = OpenTelemetry::SDK::Trace::TracerProvider.new
    host.span_limits = limits
    host.add_span_processor(A::CoveredAncestry.new)
    span = host.tracer('app').start_span('media', attributes: media, links: [OpenTelemetry::Trace::Link.new(context, media)])
    span.add_event('media', attributes: media)
    span.finish
    span
  end

  def no_media(text)
    refute_includes text, PAYLOAD
    refute_includes text, 'iVBOR'
  end

  # The application SDK exporter path and the Future AGI path (wire serializer), both through ResourceExporter.
  def exported_text(span)
    A.stub(:config, CONFIG) { export_both(span.to_span_data) }
  end

  def export_both(record)
    memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    A::ResourceExporter.new(memory, A::ExportResource.new({}), sdk_records: true).export([record])
    sdk = memory.finished_spans.map { |s| [s.attributes, s.events.map(&:attributes), s.links.map(&:attributes)] }
    captured = []
    wire = Object.new
    wire.define_singleton_method(:export) { |records, timeout: nil| captured << A::Protobuf.encode(records); 0 }
    A::ResourceExporter.new(wire, A::ExportResource.new({})).export([record])
    assert_equal 1, memory.finished_spans.size
    assert_equal 1, captured.size
    [A::Codec.generate(sdk), captured.join.b]
  end

  def test_unverified_hooks_export_no_media_on_either_path
    M.stub(:verified?, false) do
      [nil, 3, 128].each do |count|
        sdk_text, wire_bytes = exported_text(finished_span(count))
        no_media(sdk_text)
        refute_includes wire_bytes, PAYLOAD.b
        refute_includes wire_bytes, 'iVBOR'.b
        refute_includes wire_bytes, 'audio/pcm'.b
      end
    end
  end

  # No stub: whatever the running SDK proves, no media reaches either exporter path.
  def test_the_running_sdk_exports_no_media_on_either_path
    [nil, 3, 128].each do |count|
      sdk_text, wire_bytes = exported_text(finished_span(count))
      no_media(sdk_text)
      refute_includes wire_bytes, PAYLOAD.b
      refute_includes wire_bytes, 'iVBOR'.b
    end
  end

  def test_unverified_span_data_carries_a_failed_sidecar_not_media_evidence
    M.stub(:verified?, false) do
      sidecar = finished_span.to_span_data.instance_variable_get(:@traceai_media)
      assert_equal true, sidecar[:failed]
    end
  end

  def test_unverified_hooks_do_not_change_application_values
    M.stub(:verified?, false) do
      record = finished_span.to_span_data
      assert_equal PAYLOAD, record.attributes['gen_ai.input.messages.0.audio.content.chunk']
      assert_equal PAYLOAD, record.events.first.attributes['gen_ai.input.messages.0.audio.content.chunk']
    end
  end

  def test_verified_hooks_keep_normal_admission_and_omit_only_media
    M.stub(:verified?, true) do
      sdk_text, wire_bytes = exported_text(finished_span)
      no_media(sdk_text)
      refute_includes wire_bytes, PAYLOAD.b
      assert_includes sdk_text, 'safe sentence'
      assert_includes wire_bytes, 'safe sentence'.b
    end
  end

  def test_the_probe_reports_a_missing_or_changed_hook_as_unproven
    unless M::Probe.run
      flunk 'the media hooks are unproven on this SDK, which reports remote parents' if
        SkipWhenMediaHooksUnproven.unproven_outcome(ENV, OpenTelemetry::SDK::Trace::SpanData.members) == :fail
      skip 'hooks are unproven on this SDK'
    end
    M::Probe.stub(:sdk_method, nil) { refute M::Probe.run }
    wrong_arity = ->(_klass, _name) { Object.new.method(:to_s) }
    M::Probe.stub(:sdk_method, wrong_arity) { refute M::Probe.run }
  end

  def test_verification_is_decided_by_behaviour_not_by_version
    source = File.read(File.expand_path('../lib/traceai/auto/media_admission.rb', __dir__))
    refute_match(/VERSION|Gem::Version|Gem\.loaded_specs/, source)
  end

  def test_unverified_sdk_reports_once_without_values
    original = M.instance_variable_get(:@reported)
    M.instance_variable_set(:@reported, false)
    Thread.current[M::PROBE_KEY] = nil
    lines = []
    M.stub(:warn, ->(message) { lines << message }) do
      2.times { M.send(:report_unverified) }
    end
    assert_equal 1, lines.size
    refute_includes lines.first, PAYLOAD
  ensure
    M.instance_variable_set(:@reported, original)
  end

  # SDK/API releases differ in whether events and links are Structs or plain objects.
  def test_snapshot_copies_events_and_links_for_structs_and_plain_objects
    plain_event = Class.new { def initialize(*v) = (@name, @attributes, @timestamp = v) }
    plain_link = Class.new { def initialize(*v) = (@span_context, @attributes = v) }
    struct_link = Struct.new(:span_context, :attributes)
    secret = 'sk-live-' + 'a' * 24
    attrs = {'ordinary' => 'kept', 'gen_ai.input.messages.0.audio.content.chunk' => PAYLOAD,
             'gen_ai.input.messages.0.audio.mime_type' => 'audio/pcm', 'auth' => 'Bearer ' + secret}
    [[OpenTelemetry::SDK::Trace::Event.new('e', attrs, 5), OpenTelemetry::Trace::Link.new(context, attrs)],
     [plain_event.new('e', attrs, 5), plain_link.new(context, attrs)],
     [OpenTelemetry::SDK::Trace::Event.new('e', attrs, 5), struct_link.new(context, attrs)]].each do |event, link|
      source = finished_span.to_span_data
      source.remove_instance_variable(:@traceai_media) if source.instance_variable_defined?(:@traceai_media)
      source.events = [event]; source.links = [link]
      copy = P.snapshot(source, A::ExportResource.new({}), CONFIG)
      assert_equal ['e'], copy.events.map(&:name)
      assert_equal 5, copy.events.first.timestamp
      assert_equal 1, copy.links.size
      [copy.events.first.attributes, copy.links.first.attributes].each do |out|
        assert_equal 'kept', out['ordinary']
        text = A::Codec.generate(out)
        no_media(text)
        refute_includes text, secret
      end
      assert_equal context.span_id, copy.links.first.span_context.span_id
    end
  end

  def test_sdk_records_fill_unknown_future_members_with_nil
    future = Struct.new(*OpenTelemetry::SDK::Trace::SpanData.members, :brand_new_member)
    sdk_data = OpenTelemetry::SDK::Trace::SpanData
    record = finished_span.to_span_data
    OpenTelemetry::SDK::Trace.send(:remove_const, :SpanData)
    OpenTelemetry::SDK::Trace.const_set(:SpanData, future)
    delegate = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    A::ResourceExporter.new(delegate, A::ExportResource.new({}), sdk_records: true).export([record])
    exported = delegate.finished_spans.fetch(0)
    assert_equal 'media', exported.name
    assert_nil exported.brand_new_member
  ensure
    OpenTelemetry::SDK::Trace.send(:remove_const, :SpanData)
    OpenTelemetry::SDK::Trace.const_set(:SpanData, sdk_data)
  end
end
