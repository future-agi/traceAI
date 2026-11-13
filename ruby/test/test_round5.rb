# frozen_string_literal: true
require_relative 'test_round4_contract'
require_relative '../lib/traceai/auto/transports'

class Round5SecretsTest < Minitest::Test
  A = TraceAI::Auto
  P = A::Privacy

  def test_r5_registry_seeds_fi_options_and_all_credential_like_environment_names
    registry = A::SecretRegistry.new
    env = %w[KEY TOKEN SECRET PASSWORD PASSWD CREDENTIAL AUTH].to_h { |name| ["CUSTOM_#{name}_VALUE", "seed-#{name}-12345678"] }
    env['ORDINARY'] = 'not-a-credential-12345678'
    env['SHORT_TOKEN'] = 'short'
    config = A::Config.new({api_key: 'option-api-12345678', secret_key: 'option-secret-12345678'}, env: {})
    registry.seed(config, env)
    expected = env.reject { |key, _| %w[ORDINARY SHORT_TOKEN].include?(key) }.values + [config['api_key'], config['secret_key']]
    assert_equal expected.sort, registry.snapshot.sort
    assert registry.snapshot.frozen?
    assert registry.snapshot.all?(&:frozen?)
    refute_includes registry.inspect, '12345678'
  end

  def test_r5_registry_is_bounded_lru_and_caches_longest_first_immutable_snapshots
    registry = A::SecretRegistry.new
    values = 1024.times.map { |i| format('bounded-%04d-value', i) }
    values.each { |value| assert registry.register(value) }
    first = registry.snapshot
    assert_same first, registry.snapshot
    assert registry.register(values.first)
    assert_same first, registry.snapshot # A refresh changes recency, not membership.
    assert registry.register('new-longer-credential-value')
    current = registry.snapshot
    assert_equal 1024, current.length
    assert_includes current, values.first
    refute_includes current, values[1]
    assert_equal 'new-longer-credential-value', current.first
    assert_equal 1024, first.length
    assert_includes first, values[1] # In-flight snapshots cannot be mutated.
  end

  def test_r5_registry_registration_and_snapshots_are_thread_safe
    registry = A::SecretRegistry.new
    threads = 8.times.map do |thread|
      Thread.new do
        100.times do |index|
          registry.register("thread-#{thread}-#{index}-credential")
          snapshot = registry.snapshot
          raise 'mutable snapshot' unless snapshot.frozen? && snapshot.all?(&:frozen?)
          raise 'wrong order' unless snapshot.map(&:length) == snapshot.map(&:length).sort.reverse
        end
      end
    end
    threads.each(&:value)
    assert_equal 800, registry.snapshot.length
    assert_equal 800, registry.snapshot.uniq.length
  end

  def test_r5_public_registration_copies_strings_and_ignores_nonstring_or_short_values
    value = +'public-literal-credential-12345678'
    assert A.register_secret(value)
    value.replace('changed')
    assert_includes A.secret_values, 'public-literal-credential-12345678'
    refute A.register_secret('short')
    refute A.register_secret(123456789)
    hostile = Object.new
    def hostile.to_s = raise('user stringifier called')
    def hostile.class = raise('user getter called')
    refute A.register_secret(hostile)
  end

  def test_r5_suppressed_transport_hooks_observe_headers_and_urls_without_inspecting_bodies
    %i[net_http httpx].each do |transport|
      secret = "suppressed-#{transport}-header-12345678"
      query = "suppressed-#{transport}-query-12345678"
      request = Object.new
      request.define_singleton_method(:body) { raise 'suppressed body inspected' }
      if transport == :net_http
        request.define_singleton_method(:each_header) { {'authorization' => "Bearer #{secret}"}.each }
        request.define_singleton_method(:path) { "/unrecognized?access_token=#{query}" }
        base = Class.new { def request(*) = :native_result }
        base.prepend(A::NetHTTP)
        invoke = -> { base.new.request(request) }
      else
        request.define_singleton_method(:headers) { {'x-api-key' => secret} }
        request.define_singleton_method(:uri) { "http://127.0.0.1/unrecognized?api_key=#{query}" }
        base = Class.new { def send_request(*) = :native_result }
        base.prepend(A::HTTPXSession)
        invoke = -> { base.new.send(:send_request, request) }
      end
      A.stub(:active?, true) do
        A.stub(:capture_allowed?, false) { assert_equal :native_result, invoke.call }
      end
      assert_includes A.secret_values, secret
      assert_includes A.secret_values, query
    end
  end

  def test_r5_parsed_body_discovery_is_bounded_and_never_stringifies_unknown_values
    secret = 'parsed-nested-body-credential-12345678'
    hostile = Object.new
    def hostile.to_s = raise('user stringifier called')
    P.observe_body_credentials({'nested' => [{'api_key' => [secret, hostile]}]})
    assert_includes A.secret_values, secret
    cycle = []; cycle << cycle
    P.observe_body_credentials(cycle)
    # A large number of repeated branches must stop at the codec-sized limit.
    P.observe_body_credentials(Array.new(100_001, {'ordinary' => 'text'}))
  end

  def test_r5_discovery_covers_every_accepted_body_not_just_the_smaller_export_budget
    deep_secret = 'r5-deep-parsed-body-credential-12345678'
    wide_secret = 'r5-wide-parsed-body-credential-12345678'
    deep = {'api_key' => deep_secret}
    40.times { deep = {'nested' => deep} }
    wide = Array.new(20_000, nil) + [{'client_secret' => wide_secret}]
    [deep, wide].each { |value| P.observe_body_credentials(A::Codec.parse(A::Codec.generate(value))) }
    assert_includes A.secret_values, deep_secret
    assert_includes A.secret_values, wide_secret
  end
end

class Round4BoundaryTest
  def test_r5_registry_scrubs_every_export_surface_even_for_a_span_created_before_discovery
    original = record
    secret = 'late-discovered-export-credential-12345678'
    prefix = 'late-discovered-export-credential'
    marker = "echo #{secret}"
    original.name = marker
    original.attributes = {marker => marker, 'array' => [marker, prefix], 'nested' => {'plain' => marker}}
    original.status = OpenTelemetry::Trace::Status.error(marker)
    original.events = [OpenTelemetry::SDK::Trace::Event.new(marker, {'exception.message' => marker, 'exception.stacktrace' => marker}, 123)]
    state = OpenTelemetry::Trace::Tracestate.from_string('vendor=' + secret)
    original.tracestate = state
    context = OpenTelemetry::Trace::SpanContext.new(tracestate: state)
    original.links = [OpenTelemetry::Trace::Link.new(context, {'plain' => marker})]
    original.resource = A::ExportResource.new({'plain' => marker})
    original.instrumentation_scope = A::ExportScope.new(marker, marker, {'plain' => marker})
    A.register_secret(prefix)
    A.register_secret(secret)
    delegate = Object.new
    copies = nil
    delegate.define_singleton_method(:export) { |spans, timeout: nil| copies = spans; 0 }
    exporter = A::ResourceExporter.new(delegate, A::ExportResource.new({'extra' => marker}))
    A.stub(:config, A::Config.new({}, env: {})) { assert_equal 0, exporter.export([original]) }
    copy = copies.first
    encoded = A::Protobuf.encode(copies)
    refute_includes encoded, secret
    refute_includes encoded, prefix
    refute_includes encoded, '-12345678' # Longest literal must be replaced first.
    assert_equal 'echo <redacted>', copy.name
    assert_equal 'vendor=<redacted>', copy.tracestate
    assert_equal 'vendor=<redacted>', copy.links.first.span_context.tracestate
    assert_equal marker, original.name
    assert_equal marker, original.attributes[marker]
    assert_equal original.span_id, copy.span_id
    assert_equal original.start_timestamp, copy.start_timestamp
    assert_equal original.end_timestamp, copy.end_timestamp
  end

  def test_r5_registry_access_failure_fails_closed_without_dropping_spans_or_mutating_originals
    original = record
    original.attributes = {'input.value' => 'PRIVATE_INPUT', 'ordinary' => 'PRIVATE_ECHO'}
    original.status = OpenTelemetry::Trace::Status.error('PRIVATE_STATUS')
    A::SECRET_REGISTRY.stub(:snapshot, -> { raise A::SecretRegistry::Unavailable }) do
      copy = P.snapshot(original, A::ExportResource.new({}), A::Config.new({}, env: {}))
      assert_empty copy.attributes
      assert_equal '', copy.name
      assert_equal '', copy.status.description
      assert_equal original.span_id, copy.span_id
      assert_equal original.start_timestamp, copy.start_timestamp
      assert_equal original.end_timestamp, copy.end_timestamp
      assert_equal 'PRIVATE_INPUT', original.attributes['input.value']
      refute_includes A::Protobuf.encode([copy]), 'PRIVATE'
    end
  end
end

class EndToEndTest
  def r5_covered_error_code(transport, key)
    <<~RUBY
      require 'opentelemetry/sdk'; require 'anthropic'; require 'httpx'
      unless OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
        OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new
      end
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      tracer = OpenTelemetry.tracer_provider.tracer('app')
      before = tracer.start_span('started-before-request', with_parent: OpenTelemetry::Context.empty)
      queued = tracer.start_span('finished-before-request', attributes: {'ordinary' => #{key.inspect}}, with_parent: OpenTelemetry::Context.empty)
      queued.finish
      error = nil
      tracer.in_span('application-records-error') do |application|
        begin
          tracer.in_span('covered-library-call', attributes: {'gen_ai.span.kind' => 'LLM'}) do
            tracer.in_span('native-intermediate') do
              if #{(transport == 'net_http').inspect}
                client = Anthropic::Client.new(api_key: #{key.inspect}, base_url: '#{@provider.url}', max_retries: 0)
                client.messages.create(model: 'claude-test', max_tokens: 16, messages: [{role: 'user', content: 'hi'}])
              else
                response = HTTPX.post('#{@provider.url}/v1/chat/completions', headers: {'x-api-key' => #{key.inspect}}, json: {model: 'm', messages: []})
                abort 'status changed' unless response.status == 401
                response.raise_for_status
              end
            end
          end
          abort 'provider error lost'
        rescue StandardError => e
          error = e
          abort('error changed: ' + e.class.name + ': ' + e.message) unless e.message.include?(#{key.inspect})
          application.record_exception(e)
          application.status = OpenTelemetry::Trace::Status.error(e.message)
          application.set_attribute('ordinary', e.message)
          before.record_exception(e)
          before.set_attribute('ordinary', e.message)
        end
      end
      before.finish
      # No parent or shared trace with the request; discovery must be process-wide.
      tracer.in_span('unrelated-later-span') do |span|
        span.set_attribute('ordinary', error.message)
        span.record_exception(error)
      end
      original = memory.finished_spans.find { |span| span.name == 'application-records-error' }
      abort 'application exception not captured' unless original.events.any? { |event| event.attributes['exception.message'].include?(#{key.inspect}) }
      if defined?(TraceAI::Auto)
        TraceAI::Auto.flush
        abort 'other exporter mutated' unless original.attributes['ordinary'].include?(#{key.inspect})
        abort 'per-request credential state retained' if memory.finished_spans.any? { |span| span.instance_variable_defined?(:@traceai_request_credentials) || span.instance_variable_defined?(:@traceai_parent) }
        abort 'registry appeared in diagnostics' if TraceAI::Auto::Codec.generate(TraceAI::Auto.report).include?(#{key.inspect})
      end
      puts error.class.name + '|' + error.message
      puts 'SPAN_IDS ' + JSON.generate(memory.finished_spans.to_h { |span| [span.name, {id: span.span_id.unpack1('H*'), trace: span.trace_id.unpack1('H*'), started: span.start_timestamp, ended: span.end_timestamp}] })
    RUBY
  end

  def assert_r5_covered_error(transport)
    key = "r5-#{transport}-outgoing-only-credential-12345678"
    @provider.stop
    @provider = MockHTTP.new do |request|
      credential = request[:headers]['x-api-key'] || request[:headers]['authorization'].sub(/\ABearer /, '')
      [401, {'Content-Type' => 'application/json'}, JSON.generate(type: 'error', error: {type: 'authentication_error', message: 'provider echoes ' + credential})]
    end
    baseline = nil
    [false, true].each do |injected|
      stdout, stderr, status = app(r5_covered_error_code(transport, key), injected ? {} : {'RUBYOPT' => nil})
      assert status.success?, stderr
      assert_includes stdout, key
      message, ids = stdout.split("\nSPAN_IDS ", 2)
      identities = JSON.parse(ids)
      baseline ||= message
      assert_equal baseline, message # Original provider error class/message survive.
      assert_equal 1, @provider.requests.length
      @provider.requests.pop(true)
      spans = export_spans.map(&:first)
      unless injected
        assert_empty spans
        next
      end
      assert_equal identities.length, spans.length # S4 emits no duplicate wire span.
      assert spans.none? { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
      %w[application-records-error covered-library-call native-intermediate started-before-request finished-before-request unrelated-later-span].each do |name|
        identity = identities.fetch(name)
        span = spans.find { |candidate| candidate.span_id.unpack1('H*') == identity['id'] }
        refute_nil span, name
        assert_equal identity['started'], span.start_time_unix_nano
        assert_equal identity['ended'], span.end_time_unix_nano
        refute_includes span.to_h.to_s, key
      end
      refute_equal identities['covered-library-call']['trace'], identities['unrelated-later-span']['trace']
      refute_equal identities['covered-library-call']['trace'], identities['started-before-request']['trace']
      refute_includes spans.map(&:to_h).to_s, key
      application = spans.find { |span| span.span_id.unpack1('H*') == identities['application-records-error']['id'] }
      assert_equal 'application-records-error', application.name
      if transport == 'net_http'
        # The native Anthropic error is a Ruby Hash-inspect string beginning '{',
        # not JSON. Drop content, but retain independently sanitized identity.
        assert_empty attrs(application.attributes)
        assert_empty application.events
      else
        assert_includes attrs(application.attributes)['ordinary'], '<redacted>'
        assert application.events.any? { |event| attrs(event.attributes)['exception.message'].include?('<redacted>') }
      end
    end
  end

  def test_r5_net_http_covered_provider_401_is_scrubbed_across_the_process
    assert_r5_covered_error('net_http')
  end

  def test_r5_httpx_covered_provider_401_is_scrubbed_across_the_process
    assert_r5_covered_error('httpx')
  end

  def test_r5_fi_and_environment_echoes_are_seeded_once_and_public_api_registers_extra_secrets
    values = %w[KEY TOKEN SECRET PASSWORD PASSWD CREDENTIAL AUTH].to_h { |name| ["R5_PROVIDER_#{name}", "r5-env-#{name}-12345678"] }
    code = <<~RUBY
      require 'opentelemetry/sdk'
      # Seeded values survive removal from the environment after initialization.
      echoes = #{values.keys.inspect}.map { |name| ENV.delete(name) }
      echoes += [ENV.fetch('FI_API_KEY'), ENV.fetch('FI_SECRET_KEY')]
      extra = 'r5-explicit-public-credential-12345678'
      TraceAI::Auto.register_secret(extra)
      echoes << extra
      OpenTelemetry.tracer_provider.tracer('app').in_span('env-fi-echoes') do |span|
        span.set_attribute('ordinary', echoes.join(' '))
        span.set_attribute('array', echoes)
        span.record_exception(RuntimeError.new(echoes.join(' ')))
      end
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code, values)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert_equal 1, spans.length
    (values.values + %w[fixture-api-12345678 fixture-secret-12345678 r5-explicit-public-credential-12345678]).each do |secret|
      refute_includes spans.map(&:to_h).to_s, secret
    end
    assert attrs(spans.first.attributes)['array'].all? { |value| value == '<redacted>' }
  end

  def test_r5_outgoing_query_and_parsed_body_credentials_scrub_unrelated_later_spans
    %w[net_http httpx].each do |transport|
      query = "r5-#{transport}-query-credential-12345678"
      body = "r5-#{transport}-body-credential-12345678"
      @provider.stop
      @provider = MockHTTP.new { |_request| [401, {'Content-Type' => 'application/json'}, JSON.generate(error: {message: "echo #{query} #{body}"})] }
      code = <<~RUBY
        require 'net/http'; require 'httpx'
        payload = {model: 'm', messages: [], nested: [{client_secret: #{body.inspect}}]}
        path = '/v1/chat/completions?api_key=#{query}'
        if #{(transport == 'net_http').inspect}
          request = Net::HTTP::Post.new(path)
          request['content-type'] = 'application/json'; request.body = JSON.generate(payload)
          response = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |http| http.request(request) }
          output = response.body
        else
          response = HTTPX.post('#{@provider.url}' + path, json: payload)
          output = response.to_s
        end
        OpenTelemetry.tracer_provider.tracer('app').in_span('unrelated-body-query-echo') { |span| span.set_attribute('ordinary', output) }
        print output
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_includes stdout, query
      assert_includes stdout, body
      spans = export_spans.map(&:first)
      assert_equal 2, spans.length
      [query, body].each { |secret| refute_includes spans.map(&:to_h).to_s, secret }
      wire = spans.find { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
      assert_includes wire.status.message, '<redacted>'
      assert_equal :STATUS_CODE_ERROR, wire.status.code
    end
  end
end
