# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/privacy'

# Credential value shapes (spec/auto-instrumentation/privacy.json) must scrub in
# bounded time on adversarial repeated-start input, and must replace real JWTs whole.
class ValueShapeRedosTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  R = '<redacted>'
  # Reusable constants: repeat count and per-input time budget.
  N = 10_000
  BUDGET_MS = 100
  ADVERSARIAL = {
    jwt_dash_start: -> { 'eyJ-' * N },
    jwt_dash_start_trailing_dot: -> { 'eyJ-' * N + '.' },
    jwt_dash_dot_start_two_dots: -> { 'eyJ.eyJ-' * 5000 },
    sk: -> { 'sk-' * N },
    akia: -> { 'AKIA' * N },
    asia: -> { 'ASIA' * N },
    bearer: -> { 'Bearer ' * N },
    basic: -> { 'Basic ' * N },
    bearer_spaces: -> { 'Bearer' + ' ' * N },
    private_key_begin: -> { '-----BEGIN PRIVATE KEY-----' * N },
    private_key_rsa_begin: -> { '-----BEGIN RSA PRIVATE KEY-----' * N }
  }.freeze

  def self.warm_up
    @warm ||= begin
      P.scrub('warm eyJabcdefgh.abcdefgh.abcdefgh sk-abcdefghijklmnopqrstu Bearer abcdefgh1')
      true
    end
  end

  def timed_ms(input)
    self.class.warm_up
    # The fastest of three runs, so one slow run (a collection, a busy machine) does not decide.
    Array.new(3) do
      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      P.scrub(input)
      ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000).round(1)
    end.min
  end

  ADVERSARIAL.each do |name, builder|
    define_method("test_value_shapes_bounded_time_#{name}") do
      elapsed = timed_ms(builder.call)
      warn "[redos-timing] #{name}: #{elapsed} ms" if ENV['REDOS_TIMING']
      assert_operator elapsed, :<, BUDGET_MS, "#{name} took #{elapsed} ms (budget #{BUDGET_MS} ms)"
    end
  end

  def jwt(sig = 'c2lnbmF0dXJlMTIzNDU2')
    seg = ->(s) { [s].pack('m0').tr('+/', '-_').delete('=') }
    "#{seg.call('{"alg":"HS256","typ":"JWT"}')}.#{seg.call('{"sub":"1234567890","name":"Fake User"}')}.#{sig}"
  end

  def test_real_looking_jwts_are_replaced_whole
    sig_dash = jwt('fakeSignatureEndsWithDash-')
    sig_under = jwt('fakeSignatureEndsWithUnder_')
    plain = jwt
    {
      'bare' => [plain, R],
      'signature ending in dash' => [sig_dash, R],
      'signature ending in underscore' => [sig_under, R],
      'preceded by dash' => ["x-#{plain}", "x-#{R}"],
      'preceded by dash, signature ending in dash' => ["x-#{sig_dash}", "x-#{R}"],
      # A credential-named pair's value runs to its true end (the shared spec's pair rule); under any other name the
      # shape alone is replaced and the text after it stays.
      'in surrounding text' => ["token=#{plain}; next", "token=#{R}"],
      'in surrounding text, other name' => ["model=#{plain}; next", "model=#{R}; next"],
      'in prose, dash signature' => ["Authorization failed for #{sig_dash} today.", "Authorization failed for #{R} today."],
      'in quotes' => ["{\"t\":\"#{plain}\"}", "{\"t\":\"#{R}\"}"]
    }.each do |label, (input, expected)|
      assert input.include?('eyJ'), label
      out = P.scrub(input)
      assert_equal expected, out, label
      plain_sig = input[/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.([A-Za-z0-9_-]+)/, 1]
      refute_includes out, plain_sig, "#{label}: signature left behind"
    end
  end

  # --- Large real JWTs must be redacted whole (no length bound on header or payload). ---
  def b64url(bytes) = [bytes].pack('m0').tr('+/', '-_').delete('=')

  # A header with a ~2 KB x5c-like certificate run; it still starts with "eyJ" (base64url of '{"').
  def big_header_jwt
    header = b64url('{"alg":"RS256","typ":"JWT","x5c":["' + ('MIIC' * 450) + '"]}')
    assert_operator header.length, :>, 2000
    assert header.start_with?('eyJ')
    "#{header}.#{b64url('{"sub":"1234567890","name":"Fake User"}')}.c2lnbmF0dXJlMTIzNDU2"
  end

  def big_payload_jwt
    payload = b64url('{"sub":"1234567890","blob":"' + ('Zm9vYmFy' * 1500) + '"}')
    assert_operator payload.length, :>, 12_000
    "#{b64url('{"alg":"HS256","typ":"JWT"}')}.#{payload}.c2lnbmF0dXJlMTIzNDU2"
  end

  {
    'header_2kb' => :big_header_jwt,
    'payload_12kb' => :big_payload_jwt
  }.each do |name, maker|
    define_method("test_large_jwt_#{name}_replaced_whole") do
      token = send(maker)
      assert_equal R, P.scrub(token)
    end

    define_method("test_large_jwt_#{name}_after_bearer_replaced_whole") do
      token = send(maker)
      out = P.scrub("Bearer #{token}")
      assert_equal "Bearer #{R}", out
      refute_match(/eyJ|[A-Za-z0-9_-]{20}/, out.sub('Bearer', ''))
    end
  end

  # --- Differential: SDK scrub vs the plain unbounded reference regex, in this Ruby's engine. ---
  # The spec's leading \b is Python's over str: before 'eyJ' it holds when the previous character is not alphanumeric
  # (str.isalnum) and not '_', so a combining mark (U+0301) before the token is a boundary, unlike Ruby's \b.
  REFERENCE = /(?<![\p{L}\p{N}_])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/.freeze
  PIECES = ['eyJ', 'eyJaaaaaaaa', 'aaaaaaaa', '.', '-', '_', ' ', 'x'].freeze

  # The full scrubber also has a base64-omission pass that rewrites some of these strings, so the
  # differential targets the value-shape step (VALUE_SHAPES via shape_gsub) alone.
  JWT_SHAPE = P::VALUE_SHAPES.find { |shape| shape.source.start_with?('\\beyJ') }

  def test_jwt_shape_found
    refute_nil JWT_SHAPE
  end

  # Adversarial near-misses around valid tokens, all compared with the reference.
  def near_misses
    token = 'eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl'
    out = [token]
    token.length.times do |i|
      ['!', ' ', '#'].each { |c| out << token.dup.tap { |t| t[i] = c } }
      out << token.dup.tap { |t| t[i] = '' } << token.dup.tap { |t| t[i] = '.' } << token.dup.tap { |t| t.insert(i, '.') }
      out << token.dup.tap { |t| t.insert(i, '-') } << token.dup.tap { |t| t.insert(i, '_') }
    end
    out
  end

  def test_jwt_shape_matches_reference_on_near_misses
    base = near_misses
    affixes = ['', 'x', '-', '_', ' ', '.', "\u00e9", "\u00e9-", 'Bearer ', 'eyJ', 'eyJ-', 'eyJ.']
    base.each do |input|
      affixes.each do |pre|
        affixes.each do |post|
          text = pre + input + post
          assert_equal text.gsub(REFERENCE, R), P.shape_gsub(JWT_SHAPE, text), text.inspect
        end
      end
    end
  end

  def test_jwt_shape_non_ascii_letter_boundary
    ['é', 'É', 'ß', 'ж', '中', '٣', "\u0301", 'é-', '-é', "é\u00e9"].each do |mark|
      ['eyJaaaaaaaa.bbbbbbbb.cccccccc', 'xeyJaaaaaaaa.bbbbbbbb.cccccccc.eyJaaaaaaaa.bbbbbbbb.cccccccc'].each do |body|
        [mark + body, body + mark, mark + body + mark, "a.#{mark}#{body}"].each do |text|
          assert_equal text.gsub(REFERENCE, R), P.shape_gsub(JWT_SHAPE, text), text.inspect
        end
      end
    end
  end

  MIXED = ['eyJ', 'eyJaaaaaaaa', 'aaaaaaaa', 'bbbbbbbbbbbb', '.', '.', '-', '_', ' ', 'x', 'é', '..', 'eyJ-', '=', "\n"].freeze

  def test_jwt_shape_matches_unbounded_reference_on_random_strings
    rng = Random.new(20_240_607)
    20_000.times do |round|
      input = Array.new(1 + rng.rand(24)) { PIECES[rng.rand(PIECES.length)] }.join
      expected = input.gsub(REFERENCE, R)
      assert_equal expected, P.shape_gsub(JWT_SHAPE, input), "round #{round}: #{input.inspect}"
    end
    20_000.times do |round|
      input = Array.new(1 + rng.rand(24)) { MIXED[rng.rand(MIXED.length)] }.join
      expected = input.gsub(REFERENCE, R)
      assert_equal expected, P.shape_gsub(JWT_SHAPE, input), "mixed round #{round}: #{input.inspect}"
    end
  end

  # Largest size at which the backtracking reference finishes quickly in this test.
  LARGE = 200_000

  def test_jwt_shape_matches_reference_on_one_large_input
    rng = Random.new(7)
    input = +''
    input << Array.new(40) { MIXED[rng.rand(MIXED.length)] }.join << 'eyJ-' * 3000 << big_payload_jwt << ' ' while input.length < LARGE
    input = input[0, LARGE]
    assert_equal input.gsub(REFERENCE, R), P.shape_gsub(JWT_SHAPE, input)
  end
end
