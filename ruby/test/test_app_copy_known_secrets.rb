# frozen_string_literal: true
require_relative 'test_helper'

# What the application's own exporters receive from the agent carries the same known-secret replacement as the copy
# sent to Future AGI, with or without any FI_HIDE_* switch.
class AppCopyKnownSecretsTest < Minitest::Test
  include ChildHelper
  SECRET = 'fixture-secret-12345678'
  CHAT = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: "echo #{SECRET} done"}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5}).freeze

  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, CHAT] }
  end

  def teardown
    @provider.stop; @collector.stop
  end

  # An application with its own SDK provider and exporter; one model call whose request and reply both hold the secret.
  def run_app(overrides = {})
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'json'; require 'traceai/auto/agent'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init
      uri = URI('#{@provider.url}/v1/chat/completions')
      req = Net::HTTP::Post.new(uri); req['Content-Type'] = 'application/json'
      req.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'my key is #{SECRET} thanks'}])
      Net::HTTP.start(uri.host, uri.port) { |http| http.request(req) }
      abort 'flush failed' unless TraceAI::Auto.flush
      dump = memory.finished_spans.map do |span|
        {name: span.name, attributes: span.attributes, status: span.status.description, events: (span.events || []).map { |e| [e.name, e.attributes] },
         links: (span.links || []).map(&:attributes)}
      end
      puts JSON.generate(dump)
    RUBY
    stdout, stderr, status = app(code, {'RUBYOPT' => nil}.merge(overrides))
    assert status.success?, stderr
    [stdout, export_spans.map(&:first)]
  end

  def test_the_application_copy_has_known_secrets_replaced
    stdout, fi = run_app
    refute_empty JSON.parse(stdout), 'the application exporter received no span'
    refute_includes stdout, SECRET, 'a known secret reached the application\'s exporter'
    refute_includes fi.map(&:to_h).to_s, SECRET, 'a known secret reached Future AGI'
  end

  def test_replacement_does_not_depend_on_a_hide_switch
    stdout, fi = run_app('FI_HIDE_OUTPUTS' => 'true')
    refute_empty JSON.parse(stdout)
    refute_includes stdout, SECRET
    refute_includes fi.map(&:to_h).to_s, SECRET
  end

  def test_replacement_does_not_depend_on_the_input_switch_either
    stdout, fi = run_app('FI_HIDE_INPUTS' => 'false', 'FI_HIDE_OUTPUTS' => 'false')
    refute_includes stdout, SECRET
    refute_includes fi.map(&:to_h).to_s, SECRET
  end
end
