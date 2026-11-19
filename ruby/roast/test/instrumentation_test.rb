# frozen_string_literal: true

require "minitest/autorun"
require "tempfile"
require "socket"
require "opentelemetry/exporter/otlp"
require "opentelemetry/sdk"
require "opentelemetry/sdk/trace/export/in_memory_span_exporter"
require "traceai/roast"

# Only disable Roast's log formatter; lifecycle events are emitted normally.
module Roast::EventMonitor
  def self.accept(_event); end
end

class RoastInstrumentationTest < Minitest::Test
  Cog = Struct.new(:name, :type, :failed) do
    def failed? = failed
  end

  def self.setup_instrumentation
    exporter = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new(
      resource: OpenTelemetry::SDK::Resources::Resource.create("project_name" => "roast-test", "project_type" => "observe")
    )
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
    TraceAI::Roast.install!(tracer: provider.tracer("test"))
    exporter
  end

  EXPORTER = setup_instrumentation

  def setup
    EXPORTER.reset
    Fiber[:path] = nil
  end

  def test_real_roast_workflow_emits_one_span_per_async_cog
    Tempfile.create(["roast-workflow", ".rb"]) do |file|
      file.write(<<~WORKFLOW)
        config do
          ruby { async! }
        end
        execute do
          ruby(:one) { 1 }
          ruby(:two) { 2 }
        end
      WORKFLOW
      file.flush
      Roast::Workflow.from_file(file.path, Roast::WorkflowParams.new([], [], {}))
    end
    spans = EXPORTER.finished_spans
    assert_equal 3, spans.length
    workflow = spans.find { |s| s.name == "roast.workflow" }
    assert_equal 2, spans.count { |s| s.parent_span_id == workflow.span_id }
  end

  def test_async_siblings_link_to_workflow_and_model_to_cog
    manager = Object.new
    Sync do |task|
      Roast::TaskContext.begin_execution_manager(manager)
      tasks = (1..2).map do |index|
        task.async do
          cog = Cog.new("step#{index}", "ruby", false)
          Roast::TaskContext.begin_cog(cog)
          OpenTelemetry::Trace.current_span.tap do |span|
            assert span.recording?, "model code must see active cog span"
          end
          TraceAI::Roast.instance_variable_get(:@tracer).in_span("model") {}
          Roast::TaskContext.end
        end
      end
      tasks.each(&:wait)
      Roast::TaskContext.end
    end
    spans = EXPORTER.finished_spans
    assert_equal 5, spans.size
    workflow = spans.find { |s| s.name == "roast.workflow" }
    cogs = spans.select { |s| s.name == "roast.cog ruby" }
    assert_equal 2, cogs.size
    cogs.each do |cog|
      assert_equal workflow.span_id, cog.parent_span_id
      assert_equal workflow.trace_id, cog.trace_id
      model = spans.find { |s| s.name == "model" && s.parent_span_id == cog.span_id }
      refute_nil model
    end
    assert_equal OpenTelemetry::Trace::Span::INVALID, OpenTelemetry::Trace.current_span
  end

  def test_failed_cog_sets_status_and_cleans_context
    manager = Object.new
    Roast::TaskContext.begin_execution_manager(manager)
    cog = Cog.new("failing", "cmd", true)
    Roast::TaskContext.begin_cog(cog)
    Roast::TaskContext.end
    Roast::TaskContext.end
    assert_equal OpenTelemetry::Trace::Status::ERROR, EXPORTER.finished_spans.find { |s| s.name == "roast.cog cmd" }.status.code
    assert_equal OpenTelemetry::Trace::Span::INVALID, OpenTelemetry::Trace.current_span
  end

  def test_uncaught_cog_exception_records_error_and_restores_parent
    manager = Object.new
    Roast::TaskContext.begin_execution_manager(manager)
    cog = Cog.new("broken", "ruby", false)
    assert_raises(RuntimeError) do
      begin
        Roast::TaskContext.begin_cog(cog)
        raise "failed execution"
      ensure
        Roast::TaskContext.end
      end
    end
    assert_equal "roast.workflow", OpenTelemetry::Trace.current_span.name
    Roast::TaskContext.end
    span = EXPORTER.finished_spans.find { |s| s.name == "roast.cog ruby" }
    assert_equal OpenTelemetry::Trace::Status::ERROR, span.status.code
    assert_nil span.events
  end

  def test_no_prompt_or_response_attributes
    manager = Object.new
    Roast::TaskContext.begin_execution_manager(manager)
    Roast::TaskContext.begin_cog(Cog.new("safe", "chat", false))
    Roast::TaskContext.end
    Roast::TaskContext.end
    EXPORTER.finished_spans.each do |span|
      assert_equal [], span.attributes.keys.grep(/prompt|response|content|api_key/i)
    end
  end

  def test_otlp_http_receiver_observes_project_headers_and_roast_spans
    manager = Object.new
    Roast::TaskContext.begin_execution_manager(manager)
    Roast::TaskContext.begin_cog(Cog.new("hello", "ruby", false))
    Roast::TaskContext.end
    Roast::TaskContext.end

    server = TCPServer.new("127.0.0.1", 0)
    receiver = Thread.new do
      client = server.accept
      request = +""
      request << client.readpartial(4096) until request.include?("\r\n\r\n")
      head, body = request.split("\r\n\r\n", 2)
      length = head[/content-length: (\d+)/i, 1].to_i
      body << client.read(length - body.bytesize) if body.bytesize < length
      client.write("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
      client.close
      [head, body]
    end
    exporter = OpenTelemetry::Exporter::OTLP::Exporter.new(
      endpoint: "http://127.0.0.1:#{server.addr[1]}/tracer/v1/traces",
      headers: { "X-Api-Key" => "synthetic-api", "X-Secret-Key" => "synthetic-secret" },
      compression: "none"
    )
    exporter.export(EXPORTER.finished_spans)
    head, body = receiver.value
    assert_includes head, "POST /tracer/v1/traces HTTP/1.1"
    assert_match(/x-api-key: synthetic-api/i, head)
    assert_match(/x-secret-key: synthetic-secret/i, head)
    assert_includes body, "project_name"
    assert_includes body, "roast-test"
    assert_includes body, "roast.workflow"
    assert_includes body, "roast.cog ruby"
  ensure
    server&.close
    receiver&.kill
  end

  def test_install_is_idempotent
    assert_equal false, TraceAI::Roast.install!
  end
end
