# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/export'

# The agent forwards spans from the application's OpenTelemetry SDK and reads
# SpanData#parent_span_is_remote, which the SDK has only since 1.10. Gemfile's declared SDK
# floor is that version, so every SDK this suite runs on must report remote parents.
class SdkSpanDataCompatTest < Minitest::Test
  A = TraceAI::Auto
  SpanData = OpenTelemetry::SDK::Trace::SpanData

  def finished_span(remote_parent:)
    memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
    tracer = provider.tracer('app')
    if remote_parent
      context = OpenTelemetry::Trace::SpanContext.new(trace_id: OpenTelemetry::Trace.generate_trace_id,
        span_id: OpenTelemetry::Trace.generate_span_id, trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED, remote: true)
      parent = OpenTelemetry::Trace.context_with_span(OpenTelemetry::Trace.non_recording_span(context))
      tracer.start_span('child', with_parent: parent).finish
    else
      tracer.start_span('root').finish
    end
    memory.finished_spans.first
  end

  def snapshot(span)
    A::Privacy.snapshot(span, A::ExportResource.new({'project_name' => 'compat'}), A::Config.new({}, env: {}))
  end

  def test_the_sdk_is_at_or_above_the_declared_floor
    assert_includes SpanData.members, :parent_span_is_remote,
      "opentelemetry-sdk #{Gem.loaded_specs['opentelemetry-sdk']&.version} is below the declared floor 1.10"
  end

  def test_snapshot_keeps_the_remote_parent_flag
    copy = snapshot(finished_span(remote_parent: true))
    assert_equal 'child', copy.name
    assert_equal true, copy.parent_span_is_remote
    copy = snapshot(finished_span(remote_parent: false))
    assert_equal 'root', copy.name
    refute_equal true, copy.parent_span_is_remote
  end

  def test_otlp_flags_carry_the_remote_parent_bits
    sampled = 1
    assert_includes A::Protobuf.span(snapshot(finished_span(remote_parent: true))), A::Protobuf.flags(16, sampled | 256 | 512)
    assert_includes A::Protobuf.span(snapshot(finished_span(remote_parent: false))), A::Protobuf.flags(16, sampled | 256)
  end

  def test_sdk_records_for_the_application_exporter_are_built_for_this_sdk
    delegate = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    exporter = A::ResourceExporter.new(delegate, A::ExportResource.new({'project_name' => 'compat'}), sdk_records: true)
    assert_equal 0, exporter.export([finished_span(remote_parent: true)])
    exported = delegate.finished_spans.fetch(0)
    assert_equal 'child', exported.name
    assert_equal true, exported.parent_span_is_remote
  end
end
