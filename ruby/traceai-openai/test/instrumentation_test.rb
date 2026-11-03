# frozen_string_literal: true
require 'minitest/autorun'
require 'ostruct'
gem 'openai', ENV['OPENAI_VERSION'] if ENV['OPENAI_VERSION'] && ENV['OPENAI_VERSION'] != 'latest'
require 'openai'
require 'opentelemetry/sdk'
require 'traceai/openai'

class InstrumentationTest < Minitest::Test
  class FakeStream
    attr_reader :closed
    def initialize(events)
      @events = events
      @closed = false
    end
    def each(&block)
      @events.each(&block)
    end
    def close
      @closed = true
    end
  end

  def setup
    @spans = []
    exporter = Class.new do
      def initialize(spans) = (@spans = spans)
      def export(spans, timeout: nil) = (@spans.concat(spans); OpenTelemetry::SDK::Trace::Export::SUCCESS)
      def shutdown(timeout: nil) = OpenTelemetry::SDK::Trace::Export::SUCCESS
      def force_flush(timeout: nil) = OpenTelemetry::SDK::Trace::Export::SUCCESS
    end.new(@spans)
    processor = OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter)
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(processor)
    OpenTelemetry.tracer_provider = provider
    TraceAI::OpenAI.install!
    @client = ::OpenAI::Client.new(api_key: 'synthetic-key')
  end

  def with_request_result(result)
    original = ::OpenAI::Internal::Transport::BaseClient.instance_method(:request)
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request) { |_req| result }
    yield
  ensure
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request, original)
  end

  def test_chat_and_responses_sync_usage_without_content
    result = OpenStruct.new(model: 'gpt-4o-mini', id: 'synthetic-response', usage: OpenStruct.new(prompt_tokens: 3, completion_tokens: 5, input_tokens: 7, output_tokens: 11))
    with_request_result(result) do
      @client.chat.completions.create(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'PRIVATE'}])
      @client.responses.create(model: 'gpt-4o-mini', input: 'PRIVATE')
    end
    assert_equal 2, @spans.size
    chat, responses = @spans
    assert_equal 'chat', chat.attributes['gen_ai.operation.name']
    assert_equal 3, chat.attributes['gen_ai.usage.input_tokens']
    assert_equal 5, chat.attributes['gen_ai.usage.output_tokens']
    assert_equal 'responses', responses.attributes['gen_ai.operation.name']
    assert_equal 7, responses.attributes['gen_ai.usage.input_tokens']
    assert_equal 11, responses.attributes['gen_ai.usage.output_tokens']
    refute_includes @spans.to_s, 'PRIVATE'
  end

  def test_streams_finish_on_exhaustion_close_and_error
    [:chat, :responses].each do |kind|
      usage = OpenStruct.new(input_tokens: 9, output_tokens: 4, prompt_tokens: 9, completion_tokens: 4)
      event = kind == :chat ? OpenStruct.new(usage: usage, model: 'gpt', id: 'id') : OpenStruct.new(type: 'response.completed', response: OpenStruct.new(usage: usage, model: 'gpt', id: 'id'))
      raw = FakeStream.new([event])
      with_request_result(raw) do
        stream = kind == :chat ? @client.chat.completions.stream_raw(model: 'gpt', messages: [{role: 'user', content: 'secret'}]) : @client.responses.stream_raw(model: 'gpt', input: 'secret')
        assert_empty @spans
        enumeration = stream.to_enum
        assert_empty @spans
        assert_equal event, enumeration.to_a.first
        refute_equal OpenTelemetry::Trace::Status::ERROR, @spans.first.status.code
        assert_equal 1, @spans.size
        assert_equal 9, @spans.first.attributes['gen_ai.usage.input_tokens']
        stream.close
        assert raw.closed
      end
      @spans.clear
    end
  end

  def test_error_sets_status_and_reraises
    with_request_result(nil) do
      original = ::OpenAI::Internal::Transport::BaseClient.instance_method(:request)
      ::OpenAI::Internal::Transport::BaseClient.define_method(:request) { |_req| raise RuntimeError, 'synthetic failure' }
      assert_raises(RuntimeError) { @client.responses.create(model: 'gpt', input: 'secret') }
      assert_equal 1, @spans.size
      assert_equal OpenTelemetry::Trace::Status::ERROR, @spans.first.status.code
      ::OpenAI::Internal::Transport::BaseClient.define_method(:request, original)
    end
  end
  def test_next_and_explicit_close_finish_once
    usage = OpenStruct.new(input_tokens: 2, output_tokens: 0)
    event = OpenStruct.new(type: 'response.completed', response: OpenStruct.new(usage: usage))
    raw = FakeStream.new([event])
    with_request_result(raw) do
      stream = @client.responses.stream_raw(model: 'gpt', input: 'secret')
      assert_equal event, stream.next
      stream.close
      stream.close
      assert_equal 1, @spans.length
      assert_equal 0, @spans.first.attributes['gen_ai.usage.output_tokens']
    end
  end

  def test_failed_stream_event_marks_error
    event = OpenStruct.new(type: 'response.failed', response: OpenStruct.new(id: 'synthetic'))
    with_request_result(FakeStream.new([event])) do
      @client.responses.stream_raw(model: 'gpt', input: 'secret').each { |_| }
    end
    assert_equal OpenTelemetry::Trace::Status::ERROR, @spans.first.status.code
  end

  def test_other_paths_do_not_create_model_spans_and_install_is_idempotent
    TraceAI::OpenAI.install!
    with_request_result(OpenStruct.new(id: 'synthetic')) do
      @client.models.list
    end
    assert_empty @spans
    assert_equal 1, ::OpenAI::Client.ancestors.count(TraceAI::OpenAI::ClientPatch)
  end


  def test_stream_decode_error_marks_span_but_user_iteration_error_does_not
    broken_stream = Object.new
    def broken_stream.each
      raise RuntimeError, 'PRIVATE response body'
    end
    def broken_stream.close = nil
    with_request_result(broken_stream) do
      assert_raises(RuntimeError) { @client.responses.stream_raw(model: 'gpt', input: 'secret').each { |_| } }
    end
    assert_equal OpenTelemetry::Trace::Status::ERROR, @spans.first.status.code
    refute_includes @spans.first.to_s, 'PRIVATE'
    @spans.clear
    with_request_result(FakeStream.new([OpenStruct.new(type: 'response.created')])) do
      assert_raises(RuntimeError) do
        @client.responses.stream_raw(model: 'gpt', input: 'secret').each { |_| raise 'application failure' }
      end
    end
    refute_equal OpenTelemetry::Trace::Status::ERROR, @spans.first.status.code
  end

  def test_symbol_response_events_and_error_event
    usage = OpenStruct.new(input_tokens: 8, output_tokens: 3)
    completed = OpenStruct.new(type: :'response.completed', response: OpenStruct.new(id: 'id', model: 'gpt', usage: usage))
    with_request_result(FakeStream.new([completed])) do
      @client.responses.stream_raw(model: 'gpt', input: 'secret').each { |_| }
    end
    assert_equal 8, @spans.last.attributes['gen_ai.usage.input_tokens']
    assert_equal 'id', @spans.last.attributes['gen_ai.response.id']
    @spans.clear
    [:'response.failed', :error].each do |type|
      with_request_result(FakeStream.new([OpenStruct.new(type: type, response: OpenStruct.new(usage: usage))])) do
        @client.responses.stream_raw(model: 'gpt', input: 'secret').each { |_| }
      end
      assert_equal OpenTelemetry::Trace::Status::ERROR, @spans.last.status.code
    end
  end

  def test_model_span_classification_and_synchronous_failure
    with_request_result(OpenStruct.new(status: :failed, model: 'gpt')) do
      @client.responses.create(model: 'gpt', input: 'secret')
    end
    assert_equal 'LLM', @spans.last.attributes['gen_ai.span.kind']
    assert_equal 'LLM', @spans.last.attributes['fi.span.kind']
    assert_equal OpenTelemetry::Trace::Status::ERROR, @spans.last.status.code
  end

  def test_nested_sdk_spans_are_children_of_model_span
    original = ::OpenAI::Internal::Transport::BaseClient.instance_method(:request)
    tracer = OpenTelemetry.tracer_provider.tracer('synthetic-test')
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request) do |_req|
      tracer.in_span('nested-http') { OpenStruct.new(model: 'gpt') }
    end
    @client.responses.create(model: 'gpt', input: 'secret')
    model = @spans.find { |span| span.name.start_with?('responses') }
    http = @spans.find { |span| span.name == 'nested-http' }
    assert_equal model.hex_span_id, http.parent_span_id.unpack1('H*')
  ensure
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request, original) if original
  end

  def test_stream_reads_run_under_model_span_but_user_iteration_keeps_caller_context
    tracer = OpenTelemetry.tracer_provider.tracer('synthetic-test')
    raw = Object.new
    raw.define_singleton_method(:each) do |&block|
      tracer.in_span('stream-http-read', kind: :client) do
        block.call(OpenStruct.new(type: :'response.completed', response: OpenStruct.new(model: 'gpt')))
      end
    end
    raw.define_singleton_method(:close) { nil }
    with_request_result(raw) do
      tracer.in_span('app-root') do
        app_span = OpenTelemetry::Trace.current_span
        stream = @client.responses.stream_raw(model: 'gpt', input: 'secret')
        assert_same app_span, OpenTelemetry::Trace.current_span
        stream.each do |_event|
          assert_same app_span, OpenTelemetry::Trace.current_span
        end
      end
    end
    model = @spans.find { |span| span.name.start_with?('responses ') }
    read = @spans.find { |span| span.name == 'stream-http-read' }
    root = @spans.find { |span| span.name == 'app-root' }
    assert_equal root.hex_span_id, model.parent_span_id.unpack1('H*')
    assert_equal model.hex_span_id, read.parent_span_id.unpack1('H*')
  end

  def test_next_reads_stream_under_model_span_without_changing_caller_context
    tracer = OpenTelemetry.tracer_provider.tracer('synthetic-test')
    raw = Object.new
    raw.define_singleton_method(:each) do |&block|
      tracer.in_span('next-http-read') { nil }
      block.call(OpenStruct.new(type: :'response.completed'))
    end
    raw.define_singleton_method(:close) { nil }
    with_request_result(raw) do
      tracer.in_span('app-root') do
        app_span = OpenTelemetry::Trace.current_span
        stream = @client.responses.stream_raw(model: 'gpt', input: 'secret')
        stream.next
        assert_same app_span, OpenTelemetry::Trace.current_span
        stream.close
      end
    end
    model = @spans.find { |span| span.name.start_with?('responses ') }
    read = @spans.find { |span| span.name == 'next-http-read' }
    assert_equal model.hex_span_id, read.parent_span_id.unpack1('H*')
  end

  def test_chat_stream_automatically_requests_usage_without_overriding_false
    original = ::OpenAI::Internal::Transport::BaseClient.instance_method(:request)
    bodies = []
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request) do |req|
      bodies << req[:body]
      FakeStream.new([])
    end
    @client.chat.completions.stream_raw(model: 'gpt', messages: [{role: 'user', content: 'secret'}]).close
    assert_equal true, bodies.last[:stream_options][:include_usage]
    @client.chat.completions.stream_raw(model: 'gpt', messages: [{role: 'user', content: 'secret'}], stream_options: {include_usage: false}).close
    assert_equal false, bodies.last[:stream_options][:include_usage]
  ensure
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request, original) if original
  end

end
