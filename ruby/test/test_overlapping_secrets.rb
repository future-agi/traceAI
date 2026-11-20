# frozen_string_literal: true
require_relative 'test_helper'

# Known secrets that overlap never leave a fragment of the longer one beside the placeholder. A secret learned after a
# span was captured can still be replaced in the copy sent to Future AGI, which scrubs at export with the registry as it
# stands; the application's own exporter reads the span the moment it ends, so it can only have what was known by then.
class OverlappingSecretsTest < Minitest::Test
  include ChildHelper
  PLACEHOLDER = '<redacted>'

  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    @status = 200
    @provider = MockHTTP.new do |_r|
      if @status == 200
        [200, {'Content-Type' => 'application/json'}, JSON.generate(id: 'c', model: 'm', choices: [{index: 0, message: {role: 'assistant', content: 'ok'}, finish_reason: 'stop'}], usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2})]
      else
        [@status, {'Content-Type' => 'application/json'}, JSON.generate(error: {message: @error_text, type: 'server_error'})]
      end
    end
  end

  def teardown
    @provider.stop; @collector.stop
  end

  # Child application with its own SDK provider and exporter. `before` secrets are registered before the call, `after`
  # secrets between the call and the export. The value, model name and (with status 500) error text hold `text`.
  def run_app(before:, after:, text:, status: 200, limit: nil, overrides: {})
    @status = status; @error_text = "failed: #{text}"
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'; require 'json'; require 'traceai/auto/agent'
      limits = #{limit ? "OpenTelemetry::SDK::Trace::SpanLimits.new(attribute_length_limit: #{limit})" : 'nil'}
      host = limits ? OpenTelemetry::SDK::Trace::TracerProvider.new(span_limits: limits) : OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      TraceAI::Auto.init
      #{before.inspect}.each { |secret| TraceAI::Auto.register_secret(secret) }
      uri = URI('#{@provider.url}/v1/chat/completions')
      req = Net::HTTP::Post.new(uri); req['Content-Type'] = 'application/json'
      req.body = JSON.generate(model: 'm-' + #{text.inspect}, messages: [{role: 'user', content: 'see ' + #{text.inspect} + ' here'}])
      Net::HTTP.start(uri.host, uri.port) { |http| http.request(req) }
      #{after.inspect}.each { |secret| TraceAI::Auto.register_secret(secret) }
      abort 'flush failed' unless TraceAI::Auto.flush
      puts JSON.generate(memory.finished_spans.select { |s| s.name.start_with?('chat') }.map { |s| {name: s.name, status: s.status.description, attributes: s.attributes} })
    RUBY
    stdout, stderr, status_info = app(code, {'RUBYOPT' => nil}.merge(overrides))
    assert status_info.success?, stderr
    spans = export_spans.map(&:first).select { |span| span.name.start_with?('chat') }
    [stdout, spans.map { |span| [span.name, span.status&.message.to_s, attrs(span.attributes)].to_s }.join("\n")]
  end

  def refute_any(parts, copy, where)
    parts.each { |part| refute_includes copy, part, "#{where}: #{part.inspect} reached the exported bytes" }
  end

  # The longer secret contains the shorter; the shorter was known first, the longer is learned after the call.
  def test_shorter_secret_first_then_a_longer_one_that_contains_it_fi_copy
    short, long = 'overlap-secret-aaaa1111', 'PFX-LONGPART-overlap-secret-aaaa1111'
    app_copy, fi = run_app(before: [short], after: [long], text: long)
    refute_any([short, long, 'PFX-LONGPART'], fi, 'Future AGI copy')
    assert_includes fi, "see #{PLACEHOLDER} here"
    refute_empty JSON.parse(app_copy)
  end

  def test_the_same_with_an_error_status_and_a_model_name_holding_the_secret_fi_copy
    short, long = 'overlap-secret-bbbb2222', 'PFX-LONGPART-overlap-secret-bbbb2222'
    _app, fi = run_app(before: [short], after: [long], text: long, status: 500)
    refute_any([short, long, 'PFX-LONGPART'], fi, 'Future AGI copy')
  end

  def test_longer_secret_first_then_the_shorter_one_both_copies
    short, long = 'overlap-secret-cccc3333', 'PFX-LONGPART-overlap-secret-cccc3333'
    app_copy, fi = run_app(before: [long], after: [short], text: long)
    [[app_copy, 'application copy'], [fi, 'Future AGI copy']].each { |copy, where| refute_any([short, long, 'PFX-LONGPART'], copy, where) }
  end

  def test_one_secret_a_substring_of_the_other_both_known_before_the_call_both_copies
    short, long = 'overlap-secret-dddd4444', 'PFX-LONGPART-overlap-secret-dddd4444'
    app_copy, fi = run_app(before: [short, long], after: [], text: long)
    [[app_copy, 'application copy'], [fi, 'Future AGI copy']].each { |copy, where| refute_any([short, long, 'PFX-LONGPART'], copy, where) }
  end

  # Two secrets that overlap at their edges: "AAAAAAAA-BBBBBBBB" and "BBBBBBBB-CCCCCCCC" in "AAAAAAAA-BBBBBBBB-CCCCCCCC".
  def test_edge_overlapping_secrets_in_either_order_fi_copy
    [%w[AAAAAAAA-BBBBBBBB BBBBBBBB-CCCCCCCC], %w[BBBBBBBB-CCCCCCCC AAAAAAAA-BBBBBBBB]].each do |first, second|
      _app, fi = run_app(before: [first], after: [second], text: 'AAAAAAAA-BBBBBBBB-CCCCCCCC')
      refute_any(%w[AAAAAAAA BBBBBBBB CCCCCCCC], fi, "Future AGI copy (#{first} first)")
    end
  end

  def test_edge_overlapping_secrets_known_before_the_call_both_copies
    app_copy, fi = run_app(before: %w[AAAAAAAA-BBBBBBBB BBBBBBBB-CCCCCCCC], after: [], text: 'AAAAAAAA-BBBBBBBB-CCCCCCCC')
    [[app_copy, 'application copy'], [fi, 'Future AGI copy']].each { |copy, where| refute_any(%w[AAAAAAAA BBBBBBBB CCCCCCCC], copy, where) }
  end

  # The secret is replaced on the whole value before the application's attribute length limit cuts it.
  def test_a_secret_straddling_the_value_length_limit_both_copies
    secret = 'STRADDLE-SECRET-ABCDEF'
    text = ('a' * 58) + secret + ' tail'
    app_copy, fi = run_app(before: [secret], after: [], text: text, limit: 64)
    [[app_copy, 'application copy'], [fi, 'Future AGI copy']].each { |copy, where| refute_any(%w[STRADD ADDLE-S -ABCDEF], copy, where) }
  end
end
