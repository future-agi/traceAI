# frozen_string_literal: true
require_relative 'test_round5_followup'
require 'tempfile'

class PrivacyCatalogTest < Minitest::Test
  A = TraceAI::Auto
  P = A::Privacy
  SPEC = File.expand_path('../../spec/auto-instrumentation', __dir__)
  SHIPPING = File.expand_path('../lib/traceai/auto', __dir__)

  def test_s6_catalog_and_fixtures_are_byte_identical
    %w[privacy.json privacy-fixtures.json].each do |name|
      assert_equal File.binread(File.join(SPEC, name)), File.binread(File.join(SHIPPING, name)), name
    end
  end

  def test_s6_every_spec_privacy_fixture
    cases = JSON.parse(File.read(File.join(SHIPPING, 'privacy-fixtures.json'))).fetch('cases')
    failures = []
    cases.each do |fixture|
      input = fixture.fetch('input')
      begin
        hints = P.index_hints(input.fetch('siblings', {}))
        actual = P.scrub(input['value'], input.fetch('known_secrets', []), input['path'], 0, P::Budget.new, false, false, hints, input['site'] != 'free_text_urls')
        if actual == fixture['expected']
          assert_equal fixture['expected'], actual, fixture['name']
          again = P.scrub(actual, input.fetch('known_secrets', []), input['path'], 0, P::Budget.new, false, false, hints, input['site'] != 'free_text_urls')
          failures << "#{fixture['name']}: sanitizer is not idempotent" unless again == actual
        else
          failures << "#{fixture['name']}: expected #{fixture['expected'].inspect}, got #{actual.inspect}"
        end
      rescue StandardError => error
        failures << "#{fixture['name']}: #{error.class}"
      end
    end
    assert_empty failures, "#{failures.length}/#{cases.length} privacy fixtures failed:\n" + failures.join("\n")
  end

  def test_s6_credential_catalog_is_consistent_in_all_harvesting_sites
    names = P::CATALOG['credentials']['patterns'].map { |item| item['example'] }
    names += P::CATALOG['credentials']['seed_names']
    names += ['secret.max_tokens', 'client_secret_tokens', 'credentials.nested.value', 'metadata[0].x_vault_token']
    names.each do |name|
      value = 's6-native-credential-12345678'
      captured = []
      A.stub(:register_secret, ->(secret) { captured << secret }) do
        P.observe_request({name => value}, '')
        P.observe_request({}, '/path?' + name + '=' + value)
        P.observe_body_credentials({name => value})
      end
      assert_equal [value], captured.uniq, name
      assert_operator captured.length, :>=, 4, name
      assert_equal P::REDACTED, P.scrub(value, [], name), name
      assert_equal '/path?' + name + '=<redacted>', P.scrub('/path?' + name + '=' + value), name
    end
    P::CATALOG['credentials']['exclusions'].each do |rule|
      rule['examples'].each do |name|
        captured = []
        A.stub(:register_secret, ->(secret) { captured << secret }) do
          P.observe_request({name => 'ordinary-metric-12345678'}, '/path?' + name + '=ordinary-metric-12345678')
          P.observe_body_credentials({name => {'counter' => 'ordinary-metric-12345678'}})
        end
        assert_empty captured, name
      end
    end
  end

  def test_s6_harvesting_preserves_credential_ancestry_and_logprob_exclusions
    value = 's6-body-credential-12345678'
    captured = []
    A.stub(:register_secret, ->(secret) { captured << secret }) do
      P.observe_body_credentials({'credentials' => {'nested' => [value]},
                                 'choices' => [{'logprobs' => {'content' => [{'token' => 'ordinary-token-text-12345678'}]}}]})
    end
    assert_equal [value], captured
    assert_equal({'credentials' => '<redacted>'}, P.scrub({'credentials' => {'nested' => [value]}}))
    captured.clear
    A.stub(:register_secret, ->(secret) { captured << secret }) do
      P.observe_request({}, '/path#fragment?token=' + value)
    end
    assert_empty captured
  end

  def test_s6_media_hint_index_is_bounded_and_does_not_bleed_across_siblings
    too_many = Array.new(P::CATALOG['limits']['max_nodes'] + 1, 'audio/pcm')
    assert_raises(P::Limit) { P.scrub({'mime_type' => too_many, 'content' => 'AQIDBA=='}) }
    value = {'parts' => [{'mime_type' => 'audio/pcm', 'content' => 'AQIDBA=='}, {'content' => 'AQIDBA=='}],
             'ordinary' => {'content' => 'AQIDBA=='}}
    expected = {'parts' => [{'mime_type' => 'audio/pcm', 'content' => '<omitted: 8 base64 chars>'}, {'content' => 'AQIDBA=='}],
                'ordinary' => {'content' => 'AQIDBA=='}}
    assert_equal expected, P.scrub(value)
    assert_equal 'AQIDBA==', value['parts'][0]['content']
    path = ('ordinary.' * 2_000) + 'content'
    assert_equal 'words', P.scrub('words', [], path)
  end

  def test_s6_atomic_credential_pair_precedence_and_missing_half_warnings
    file = Tempfile.new(['s6-config', '.toml'], File.join(ROOT, '.bundle/tmp'))
    file.write("api_key = \"file-api-12345678\"\nsecret_key = \"file-secret-12345678\"\nproject_name = \"file-project\"\n")
    file.close
    env = {'FI_CONFIG_FILE' => file.path, 'FI_API_KEY' => 'env-api-12345678', 'FI_PROJECT_NAME' => 'env-project'}
    config = nil
    _, stderr = capture_io { config = A::Config.new({}, env: env) }
    assert_equal ['file-api-12345678', 'file-secret-12345678'], [config['api_key'], config['secret_key']]
    assert_equal 'env-project', config['project_name']
    assert_equal 1, stderr.lines.length
    assert_includes stderr, 'environment credential pair ignored: missing FI_SECRET_KEY'
    refute_includes stderr, '12345678'
    assert config.credentials?

    env['FI_SECRET_KEY'] = 'env-secret-12345678'
    _, stderr = capture_io { config = A::Config.new({secret_key: 'explicit-orphan-12345678'}, env: env) }
    assert_equal ['env-api-12345678', 'env-secret-12345678'], [config['api_key'], config['secret_key']]
    assert_includes stderr, 'explicit arguments credential pair ignored: missing FI_API_KEY'
    assert_equal 1, stderr.lines.length
    explicit = {api_key: 'explicit-api-12345678', secret_key: 'explicit-secret-12345678'}
    config = A::Config.new(explicit, env: env)
    assert_equal explicit.values, [config['api_key'], config['secret_key']]
    assert_empty config.warnings

    file.open; file.truncate(0); file.write("secret_key = \"file-orphan-12345678\"\n"); file.close
    _, stderr = capture_io { config = A::Config.new({}, env: env.reject { |key, _| key == 'FI_SECRET_KEY' }) }
    refute config.credentials?
    assert_nil config['api_key']; assert_nil config['secret_key']
    assert_equal 2, stderr.lines.length
    assert_includes stderr, 'config file credential pair ignored: missing FI_API_KEY'
    _, stderr = capture_io { config = A::Config.new({api_key: '', secret_key: 'explicit-orphan-12345678'}, env: {}) }
    refute config.credentials?
    assert_equal 1, stderr.lines.length
  ensure
    file&.unlink
  end
end

class EndToEndTest
  def test_s6_credential_marked_headers_on_both_covered_transports
    %w[net_http httpx].each do |transport|
      %w[x-access-token private-token x-vault-token].each do |header|
        assert_r5_verifier_header_echo(transport, header, "s6-#{transport}-#{header}-12345678")
      end
    end
  end

  def test_s6_mime_content_and_response_query_credentials_in_detached_otlp
    audio = r5_verifier_audio_attributes.fetch('input.audio.0')
    attributes = {
      'gen_ai.input.messages' => JSON.generate([{type: 'blob', mime_type: 'audio/wav', content: audio}]),
      'input.attachment.0.content' => audio,
      'input.attachment.0.mime_type' => 'audio/wav',
      'ordinary' => 'visible'
    }
    assert_r5_followup_media_export(attributes, [audio], %w[gen_ai.input.messages input.attachment.0.content],
                                  {'input.attachment.0.mime_type' => 'audio/wav', 'ordinary' => 'visible'})
    names = %w[client_secret password refresh_token api-key]
    values = names.to_h { |name| [name, "s6-returned-#{name}-12345678"] }
    url = 'https://example.invalid/object?' + values.map { |name, value| name + '=' + value }.join('&') + '&signature=control'
    @provider.stop
    @provider = MockHTTP.new { |_| [200, {'Content-Type' => 'application/json'}, JSON.generate(choices: [{message: {content: url}}])] }
    code = <<~RUBY
      require 'opentelemetry/sdk'; require 'net/http'
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      OpenTelemetry.tracer_provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider.tracer('app').in_span('s6-response-urls') do |span|
        response = Net::HTTP.post(URI('#{@provider.url}/v1/chat/completions'), '{"model":"m","messages":[]}', {'content-type' => 'application/json'})
        span.set_attribute('output.value', response.body)
        span.add_event('response', attributes: {'output.value' => response.body})
      end
      TraceAI::Auto.flush
      abort 'host exporter mutated' unless memory.finished_spans.find { |span| span.name == 's6-response-urls' }.attributes['output.value'].include?(#{url.inspect})
      puts 'APPLICATION_RAN'
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal "APPLICATION_RAN\n", stdout
    spans = export_spans.map(&:first)
    assert_equal 2, spans.length
    values.each_value { |value| refute_includes spans.map(&:to_h).to_s, value }
    assert_includes spans.map(&:to_h).to_s, 'client_secret=<redacted>'
    assert_equal 1, @provider.requests.size
  end
end
