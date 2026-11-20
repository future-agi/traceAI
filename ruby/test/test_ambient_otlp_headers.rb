# frozen_string_literal: true
require_relative 'test_helper'
# Checks that OTLP header variables meant for some other backend, set in the
# process environment, are not added to requests bound for the Future AGI
# collector. Everything is synthetic and goes to a 127.0.0.1 capture server.
class AmbientOtlpHeadersTest < Minitest::Test
  include ChildHelper
  AMBIENT_AUTH = 'Bearer SYNTHETIC-OTHER-BACKEND'
  ENV_VARS = {'OTEL_EXPORTER_OTLP_HEADERS' => 'x-ambient-marker-a=SYNTHETIC-MARKER-A',
              'OTEL_EXPORTER_OTLP_TRACES_HEADERS' => 'x-ambient-marker-b=SYNTHETIC-MARKER-B'}.freeze
  NO_AMBIENT = {'OTEL_EXPORTER_OTLP_HEADERS' => nil, 'OTEL_EXPORTER_OTLP_TRACES_HEADERS' => nil}.freeze
  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/json'}, '{"id":"c","object":"chat.completion","model":"gpt-4o-mini","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'] }
  end
  def teardown
    @provider.stop; @collector.stop
  end
  def traffic
    code = <<~RUBY
      require 'net/http'; require 'json'
      uri = URI('#{@provider.url}/v1/chat/completions')
      req = Net::HTTP::Post.new(uri); req['Content-Type'] = 'application/json'
      req.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'hi'}])
      Net::HTTP.start(uri.host, uri.port) { |http| http.request(req) }
    RUBY
  end
  def run_app(code, **env) = app(code, env.transform_keys(&:to_s).merge(NO_AMBIENT) { |_k, mine, _ambient| mine })
  def received
    requests = []
    requests << @collector.requests.pop(true) until @collector.requests.empty?
    requests
  end
  def assert_fi_headers_only(requests, marker)
    refute_empty requests, 'the capture server received no export request'
    requests.each do |req|
      assert_equal 'fixture-api-12345678', req[:headers]['x-api-key']
      assert_equal 'fixture-secret-12345678', req[:headers]['x-secret-key']
      refute req[:headers].key?('x-ambient-marker-a') || req[:headers].key?('x-ambient-marker-b'), "marker header #{marker} reached the FI exporter"
      refute_equal AMBIENT_AUTH, req[:headers]['authorization'], 'ambient Authorization reached the FI exporter'
      refute req[:headers].key?('authorization')
    end
  end
  ENV_VARS.each_key do |name|
    suffix = name.downcase
    define_method("test_zero_code_path_ignores_#{suffix}") do
      value = "authorization=#{AMBIENT_AUTH},#{ENV_VARS[name]}"
      _out, stderr, status = run_app(traffic, **{name.to_sym => value})
      assert status.success?, stderr
      assert_fi_headers_only(received, name)
    end
    define_method("test_application_sdk_provider_ignores_#{suffix}") do
      value = "authorization=#{AMBIENT_AUTH},#{ENV_VARS[name]}"
      code = app_sdk + "OpenTelemetry.tracer_provider.tracer('app').in_span('app-work') {}\n" + traffic
      _out, stderr, status = run_app(code, **{name.to_sym => value})
      assert status.success?, stderr
      assert_fi_headers_only(received, name)
    end
    # The documented explicit-header OTLP exporter setup (see roast/README.md).
    define_method("test_documented_otlp_exporter_with_explicit_headers_ignores_#{suffix}") do
      value = "authorization=#{AMBIENT_AUTH},#{ENV_VARS[name]}"
      code = <<~RUBY
        require 'opentelemetry/sdk'; require 'opentelemetry/exporter/otlp'
        exporter = OpenTelemetry::Exporter::OTLP::Exporter.new(endpoint: '#{@collector.url}/tracer/v1/traces',
          headers: {'X-Api-Key' => 'fixture-api-12345678', 'X-Secret-Key' => 'fixture-secret-12345678'}, compression: 'none')
        provider = OpenTelemetry::SDK::Trace::TracerProvider.new
        provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
        provider.tracer('app').in_span('app-work') {}
        provider.shutdown
      RUBY
      _out, stderr, status = run_app(code, RUBYOPT: nil, **{name.to_sym => value})
      assert status.success?, stderr
      assert_fi_headers_only(received, name)
    end
  end
end
