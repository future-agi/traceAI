# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/projection'

# Privacy catalog v3 rules that the shared fixture bundle does not exercise on
# its own: credential value shapes, the provider-response logprob entry point
# and the protected TIMING family in attribute projection.
class PrivacyCatalogV3Test < Minitest::Test
  A = TraceAI::Auto
  P = A::Privacy
  AP = A::AttributeProjection
  SPEC = File.expand_path('../../spec/auto-instrumentation', __dir__)
  R = '<redacted>'
  # Obviously fake values, assembled from parts so no source line looks like
  # a real credential to a scanner.
  PROVIDER_KEY = 'sk-' + 'test-PLACEHOLDER-PLACEHOLDER'
  AWS_KEY = 'AKIA' + 'PLACEHOLDER12345'
  JWT = 'eyJ' + 'placeholder.placeholder.placeholder'
  PEM_BEGIN = ['-----BEGIN', 'PRIVATE KEY-----'].join(' ')
  PEM_END = ['-----END', 'PRIVATE KEY-----'].join(' ')
  TIMING = %w[gen_ai.response.latency gen_ai.response.time_to_first_chunk gen_ai.response.time_to_first_text
              gen_ai.response.time_to_first_token gen_ai.server.time_to_first_token
              gen_ai.server.time_per_output_token gen_ai.server.request.duration].freeze

  def test_shipped_catalog_and_fixture_bundle_are_both_version_3
    catalog = JSON.parse(File.read(File.join(SPEC, 'privacy.json'), encoding: 'UTF-8'))
    bundle = JSON.parse(File.read(File.join(SPEC, 'privacy-fixtures.json'), encoding: 'UTF-8'))
    assert_equal catalog['version'], bundle['catalog_version']
    assert_equal 3, P::CATALOG['version']
    assert_equal [], P::CATALOG['credentials']['contextual_exclusions']
    assert P::CATALOG['credentials']['value_shapes'].any?
    assert P::CATALOG['logprob_tokens']['formats'].any? { |rule| rule['format'] == 'openai.chat' }
  end

  def test_token_timing_names_keep_numeric_values_at_every_site
    rule = P::CATALOG['credentials']['exclusions'].find { |item| item['id'] == 'token-timing' }
    refute_nil rule
    rule['examples'].each do |name|
      refute P.credential?(name), name
      [17, 0.125].each do |number|
        assert_equal({name => number}, P.scrub({name => number}), name)
        assert_equal({'nested' => [{name => number}]}, P.scrub({'nested' => [{name => number}]}), name)
        assert_equal number, P.scrub(number, [], 'metadata.' + name), name
        url = "https://example.invalid/object?#{name}=#{number}&model=keep#anchor"
        assert_equal url, P.scrub(url), name
      end
    end
  end

  def test_token_timing_names_never_hide_an_independent_credential
    %w[token access_token x-access-token session_token token_secret api_token_latency_key auth_token
       provider_secret_time_to_first_token metadata.api_key.time_to_first_token
       gen_ai.response.time_to_first_token.api_key].each do |name|
      assert P.credential?(name), name
      assert_equal R, P.scrub('SYNTHETIC_PRIVATE_20261030', [], name), name
    end
    captured = []
    A.stub(:register_secret, ->(secret) { captured << secret }) do
      P.observe_request({'time_to_first_token' => 'ordinary-metric-12345678'}, '/path?tokens_per_second=ordinary-metric-12345678')
      P.observe_body_credentials({'gen_ai.server.time_per_output_token' => {'counter' => 'ordinary-metric-12345678'}})
    end
    assert_empty captured
  end

  def test_access_key_identifiers_are_credentials_again
    %w[access_key_id aws_access_key_id].each do |name|
      assert P.credential?(name), name
      assert_equal R, P.scrub('SYNTHETIC_PRIVATE_20261030', [], name), name
    end
    refute P.credential?('key_id')
  end

  def test_value_shapes_redact_credentials_in_text_without_a_credential_name
    {
      "provider key #{PROVIDER_KEY} in prose" => "provider key #{R} in prose",
      "aws #{AWS_KEY} here" => "aws #{R} here",
      "jwt #{JWT} here" => "jwt #{R} here",
      'header was Bearer placeholder1234 then' => "header was #{R} then",
      'header was basic cGxhY2Vob2xkZXI6cGxhY2Vob2xkZXI= then' => "header was #{R} then"
    }.each do |input, expected|
      assert_equal expected, P.scrub(input), input
      assert_equal expected, P.scrub(expected), "idempotent: #{input}"
      assert_equal({'note' => expected}, P.scrub({'note' => input}), input)
    end
    assert_equal({R => 'value'}, P.scrub({"Bearer placeholder1234" => 'value'}))
    assert_equal 'time_to_first_token 0.125 stays visible', P.scrub('time_to_first_token 0.125 stays visible')
  end

  def test_private_key_shape_covers_every_line_of_complete_and_truncated_pem
    complete = "before\n#{PEM_BEGIN}\nPLACEHOLDERLINEONE\nPLACEHOLDERLINETWO\n#{PEM_END}\nafter"
    assert_equal "before\n#{R}\nafter", P.scrub(complete)
    rsa = PEM_BEGIN.sub('BEGIN ', 'BEGIN RSA ')
    truncated = "prefix #{rsa}\nPLACEHOLDERLINEONE\nPLACEHOL"
    assert_equal "prefix #{R}", P.scrub(truncated)
    refute_includes P.scrub({'output' => truncated}).to_s, 'PLACEHOLDERLINEONE'
  end

  def logprob_body(token, content = 'safe')
    entry = {'token' => token, 'bytes' => [65, 66], 'logprob' => -0.2, 'top_logprobs' => [{'token' => token, 'bytes' => [65, 66]}]}
    {'choices' => [{'index' => 0, 'message' => {'content' => content}, 'logprobs' => {'content' => [entry]}}]}
  end

  def test_provider_response_root_keeps_ordinary_logprob_tokens_but_generic_scrub_does_not
    safe = P.scrub_provider_response(logprob_body('Hello.'))
    entry = safe['choices'][0]['logprobs']['content'][0]
    assert_equal ['Hello.', [65, 66], 'Hello.', [65, 66]], [entry['token'], entry['bytes'], entry['top_logprobs'][0]['token'], entry['top_logprobs'][0]['bytes']]
    assert_equal 'safe', safe['choices'][0]['message']['content']
    generic = P.scrub(logprob_body('Hello.'))
    assert_equal R, generic['choices'][0]['logprobs']['content'][0]['token']
    assert_equal R, generic['choices'][0]['logprobs']['content'][0]['top_logprobs'][0]['token']
    assert_equal [], P.discover_provider_response_secrets(logprob_body('ordinary-word'))
    captured = []
    A.stub(:register_secret, ->(secret) { captured << secret }) { P.observe_body_credentials(logprob_body('ordinary-word')) }
    assert_includes captured, 'ordinary-word'
    credential = {'choices' => [{'index' => 0, 'logprobs' => {'content' => [{'token' => 'Hello.', 'api_key' => 'provider-response-12345678'}]}}]}
    assert_equal ['provider-response-12345678'], P.discover_provider_response_secrets(credential)
  end

  def test_provider_response_tokens_still_meet_registered_secrets_and_shapes
    secret = 'synthetic-d-wire-private'
    ["Bearer placeholder1234", secret].each do |token|
      output = P.scrub_provider_response(logprob_body(token), [secret])
      entry = output['choices'][0]['logprobs']['content'][0]
      [entry, entry['top_logprobs'][0]].each do |item|
        assert_equal R, item['token'], token
        assert_equal R, item['bytes'], token
      end
      assert_equal(-0.2, entry['logprob'])
    end
  end

  def test_masked_message_text_masks_the_whole_logprobs_block
    secret = 'synthetic-message-private'
    output = P.scrub_provider_response(logprob_body('Hello.', "answer #{secret}"), [secret])
    assert_equal "answer #{R}", output['choices'][0]['message']['content']
    assert_equal R, output['choices'][0]['logprobs']
    shaped = P.scrub_provider_response(logprob_body('Hello.', "use #{PROVIDER_KEY}"))
    assert_equal R, shaped['choices'][0]['logprobs']
  end

  def test_buffered_stream_masks_split_secrets_across_deltas_and_unindexed_choices
    secret = 'synthetic-split-private'
    chunk = lambda do |text, index = 0|
      choice = {'delta' => {'content' => text}, 'logprobs' => {'content' => [{'token' => text, 'bytes' => [1]}]}}
      choice['index'] = index unless index.nil?
      {'choices' => [choice]}
    end
    chunks = [chunk.call('synthetic-'), chunk.call('split-'), chunk.call('private'), chunk.call('other', 1)]
    output = P.scrub_provider_response(chunks, [secret])
    output.first(3).each do |item|
      assert_equal R, item['choices'][0]['delta']['content']
      assert_equal R, item['choices'][0]['logprobs']
    end
    assert_equal 'other', output[3]['choices'][0]['delta']['content']
    assert_equal 'other', output[3]['choices'][0]['logprobs']['content'][0]['token']
    unindexed = P.scrub_provider_response([chunk.call('Hello.', nil)])
    assert_equal R, unindexed[0]['choices'][0]['logprobs']
  end

  def test_provider_root_exemption_is_bound_to_the_exact_parsed_paths
    body = logprob_body('Hello.')
    # Equal in content to the anchored top_logprobs entry, but a different
    # object at an unanchored path: equality must not grant the exemption.
    body['tool_output'] = {'token' => 'Hello.', 'bytes' => [65, 66]}
    body['wrapper'] = logprob_body('Hello.')
    body['choices'][0]['logprobs']['content'][0]['nested'] = {'token' => 'Hello.'}
    output = P.scrub_provider_response(body)
    assert_equal 'Hello.', output['choices'][0]['logprobs']['content'][0]['token']
    assert_equal 'Hello.', output['choices'][0]['logprobs']['content'][0]['top_logprobs'][0]['token']
    assert_equal R, output['tool_output']['token']
    assert_equal R, output['wrapper']['choices'][0]['logprobs']['content'][0]['token']
    assert_equal R, output['choices'][0]['logprobs']['content'][0]['nested']['token']
    nested = JSON.generate(logprob_body('nested'))
    response = {'choices' => [{'index' => 0, 'message' => {'role' => 'assistant', 'content' => nested}}]}
    content = P.scrub_provider_response(response)['choices'][0]['message']['content']
    refute_includes content, '"token":"nested"'
    assert_includes content, '"token":"<redacted>"'
    assert_equal R, P.scrub_provider_response(logprob_body('Hello.'), [], 'unknown.format')['choices'][0]['logprobs']['content'][0]['token']
  end

  def test_timing_family_is_protected_between_usage_and_provenance
    TIMING.each { |key| assert_equal 5, AP.protected_family(key), key }
    assert_equal 4, AP.protected_family('gen_ai.usage.input_tokens')
    assert_equal 6, AP.protected_family('traceai.capture.layer')
    assert_equal 6, AP.protected_family('server.address')
    assert_nil AP.protected_family('gen_ai.response.time_to_first_byte')
  end

  def test_projection_reserves_present_timing_before_content_and_provenance
    values = {'gen_ai.usage.input_tokens' => 3, 'server.address' => 'localhost', 'traceai.capture.layer' => 'wire', 'output.value' => 'answer'}
    TIMING.each_with_index { |key, index| values[key] = 0.125 + index }
    selected = AP.project(values, 9).attributes
    assert_equal [AP::COUNT, 'gen_ai.usage.input_tokens', *TIMING.sort], selected.keys
    TIMING.each_with_index { |key, index| assert_equal 0.125 + index, selected[key], key }
    assert_equal 1, selected[AP::COUNT]
    assert_equal values, AP.project(values, 128).attributes
    smaller = AP.project(values, 3).attributes
    assert_equal [AP::COUNT, 'gen_ai.usage.input_tokens', TIMING.min], smaller.keys
  end
end
