# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/export'

# The agent does not declare a runtime dependency on opentelemetry-sdk, so an application can bundle
# a release (1.3 through 1.9) whose SpanData has no parent_span_is_remote member. Spans from such an
# SDK are still snapshotted and exported, reporting a local parent.
class SdkSpanDataWithoutRemoteFlagTest < Minitest::Test
  A = TraceAI::Auto
  RealSpanData = OpenTelemetry::SDK::Trace::SpanData
  OlderSpanData = Struct.new(*(RealSpanData.members - [:parent_span_is_remote]), keyword_init: false)

  class Capture
    attr_reader :records
    def export(records, timeout: nil)
      (@records ||= []).concat(records)
      0
    end
  end

  def older_span
    memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
    provider.tracer('app').start_span('older-sdk').finish
    real = memory.finished_spans.first
    OlderSpanData.new(*OlderSpanData.members.map { |member| real[member] })
  end

  def config = A::Config.new({}, env: {})
  def resource = A::ExportResource.new({'project_name' => 'compat'})

  def with_span_data(klass)
    sdk = OpenTelemetry::SDK::Trace
    sdk.send(:remove_const, :SpanData)
    sdk.const_set(:SpanData, klass)
    yield
  ensure
    sdk.send(:remove_const, :SpanData)
    sdk.const_set(:SpanData, RealSpanData)
  end

  def test_snapshot_of_span_data_without_the_remote_flag_reports_a_local_parent
    copy = A::Privacy.snapshot(older_span, resource, config)
    assert_equal 'older-sdk', copy.name
    assert_equal false, copy.parent_span_is_remote
  end

  def test_the_wire_form_of_such_a_span_carries_no_remote_parent_bits
    copy = A::Privacy.snapshot(older_span, resource, config)
    assert_includes A::Protobuf.span(copy), A::Protobuf.flags(16, copy.trace_flags | 256)
  end

  def test_the_exporter_forwards_such_a_span_with_internal_records
    delegate = Capture.new
    exporter = A::ResourceExporter.new(delegate, resource, sdk_records: false)
    assert_equal 0, exporter.export([older_span])
    assert_equal ['older-sdk'], delegate.records.map(&:name)
    assert_equal [false], delegate.records.map(&:parent_span_is_remote)
  end

  def test_the_exporter_forwards_such_a_span_with_sdk_records
    delegate = Capture.new
    span = older_span
    with_span_data(OlderSpanData) do
      exporter = A::ResourceExporter.new(delegate, resource, sdk_records: true)
      assert_equal 0, exporter.export([span])
    end
    assert_equal ['older-sdk'], delegate.records.map(&:name)
    assert_equal OlderSpanData.members, delegate.records.first.class.members
  end
end
