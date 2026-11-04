# frozen_string_literal: true
require 'minitest/autorun'
require 'ostruct'
gem 'openai', ENV['OPENAI_VERSION'] if ENV['OPENAI_VERSION'] && ENV['OPENAI_VERSION'] != 'latest'
require 'openai'
require 'opentelemetry/sdk'
require 'opentelemetry/instrumentation/openai'
require 'traceai/openai'

class LateInstrumentationTest < Minitest::Test
  def test_standard_adapter_installed_after_traceai_does_not_duplicate_chat_spans
    spans = []
    exporter = Class.new do
      def initialize(spans) = (@spans = spans)
      def export(batch, timeout: nil) = (@spans.concat(batch); OpenTelemetry::SDK::Trace::Export::SUCCESS)
      def shutdown(timeout: nil) = OpenTelemetry::SDK::Trace::Export::SUCCESS
      def force_flush(timeout: nil) = OpenTelemetry::SDK::Trace::Export::SUCCESS
    end.new(spans)
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
    OpenTelemetry.tracer_provider = provider
    TraceAI::OpenAI.install!
    assert OpenTelemetry::Instrumentation::OpenAI::Instrumentation.instance.install
    original = ::OpenAI::Internal::Transport::BaseClient.instance_method(:request)
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request) do |_req|
      OpenStruct.new(model: 'gpt', id: 'synthetic', usage: OpenStruct.new(prompt_tokens: 3, completion_tokens: 4))
    end
    ::OpenAI::Client.new(api_key: 'synthetic-key').chat.completions.create(model: 'gpt', messages: [{role: 'user', content: 'secret'}])
    assert_equal 1, spans.count { |span| span.attributes['gen_ai.operation.name'] == 'chat' }
    ::OpenAI::Client.new(api_key: 'synthetic-key').responses.create(model: 'gpt', input: 'secret')
    assert_equal 1, spans.count { |span| span.attributes['gen_ai.operation.name'] == 'responses' }
  ensure
    ::OpenAI::Internal::Transport::BaseClient.define_method(:request, original) if original
  end
end
