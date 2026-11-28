# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'

# The shared privacy spec v4 rules, one focused test per rule group. Every
# expected text below is what spec/auto-instrumentation/generate_privacy_fixtures.py
# (class Reference) exports for the same input at the same site.
class PairRuleSpecTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  R = '<redacted>'
  V = 'SYNTHETIC_PRIVATE_20261030'

  def prose(value, secrets = []) = P.scrub(value, secrets, nil, 0, P::Budget.new, false, false, {}, false)
  def framed(value, path = nil) = P.scrub(value, [], path)

  def test_group_1_pair_rule_replaces_credential_pairs_to_the_true_value_end
    assert_equal "connect with password=#{R}", prose("connect with password=#{V} and more")
    assert_equal "login password: #{R}, model=keep", prose('login password: SYNTHETIC PRIVATE, model=keep')
    # A placeholder already in the value is no exception: the value still runs to its end.
    assert_equal "api_key=#{R}", prose("api_key=#{R}SYNTHETIC_TAIL next")
    assert_equal "dbPass=#{R}", prose("dbPass=#{V} rest")
  end

  def test_group_2_whole_url_values_read_the_pair_rule_outside_catalog_spans
    assert_equal "https://example.invalid/a?model=keep,password=#{R}", framed("https://example.invalid/a?model=keep,password=#{V}")
    assert_equal "https://example.invalid/a?token=#{R}&model=keep", framed("https://example.invalid/a?token=#{V}&model=keep")
  end

  def test_group_3_url_catalog_reads_fragments_second_question_marks_and_path_segments
    assert_equal "https://example.invalid/a#password=#{R}&view=1", framed("https://example.invalid/a#password=#{V}&view=1")
    assert_equal "https://example.invalid/a?next=/b?password=#{R}", framed("https://example.invalid/a?next=/b?password=#{V}")
    assert_equal "https://example.invalid/password=#{R}/next", framed("https://example.invalid/password=#{V}/next")
    assert_equal "https://example.invalid/password%3D#{R}/next", framed("https://example.invalid/password%3D#{V}/next")
    assert_equal "see https://example.invalid/a#password=#{R}", prose("see https://example.invalid/a#password=#{V}&view=1 then")
  end

  def test_group_4_vocabulary_words_are_credential_names_at_every_site
    assert_equal R, framed(V, 'db.pwd')
    assert_equal "https://example.invalid/a?pin=#{R}&model=keep", framed("https://example.invalid/a?pin=#{V}&model=keep")
    assert_equal({'user-pin' => R, 'model' => 'keep'}, framed({'user-pin' => V, 'model' => 'keep'}))
    assert P.credential?('dbPass')
    refute P.credential?('spinner')
  end

  def test_group_5_json_quoted_names_follow_the_three_value_cases
    assert_equal %(text "password": "#{R}", "model": "keep"), prose(%(text "password": "SYNTHETIC", "model": "keep"))
    assert_equal %(text "password": "#{R}", "model": "keep"), prose(%(text "password": 12345, "model": "keep"))
    assert_equal %(text "password": "#{R}"), prose(%(text "password": {"a": 1}, "model": "keep"))
    assert_equal "text 'password': '#{R}'", prose("text 'password': SYNTHETIC PRIVATE VALUE")
  end

  def test_group_6_map_keys_take_the_export_text_rules_and_collisions_keep_one_key
    assert_equal({"https://example.invalid/a?token=#{R}" => R}, framed({"https://example.invalid/a?token=#{V}" => 'keep'}))
    assert_equal({"a?token=#{R}" => R, 'model' => 'keep'},
                 framed({'a?token=SYNTHETIC_ONE_20261030' => 'one', 'a?token=SYNTHETIC_TWO_20261030' => 'two', 'model' => 'keep'}))
  end

  def test_group_7_depth_budget_exports_the_whole_value_as_the_placeholder
    deep = ('a'..'z').to_a.join('.') + '.aa.bb.cc.dd.ee.ff.gg'
    assert_equal R, framed('keep', deep)
    assert_equal R, prose("x #{deep}=1 y")
  end

  def test_group_8_bounded_names_fail_closed
    long = 'a' * 135
    assert_equal "keep #{long}=#{R}", prose("keep #{long}=SYNTHETIC tail")
    marks = 'x?a?b?c?d?e?f?g?h?i'
    assert_equal "https://example.invalid/a?q=#{marks}=#{R}", framed("https://example.invalid/a?q=#{marks}=#{V}")
  end

  def test_group_9_adversarial_text_is_read_in_linear_time
    unit = "--token\nv "
    value = (unit * (1_600_000 / unit.length + 1))[0, 1_600_000]
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    out = P.pair_rule(value)
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
    refute_equal value, out
    refute_includes out, "\nv "
    assert_operator elapsed, :<, 30.0, "pair rule over 1.6M characters took #{elapsed.round(2)}s"
  end

  # The export of 1.6M characters of adversarial prose (many '?', '=', long names, quoted names, --name words and URLs)
  # finishes within EXPORT_BOUND seconds per input. Measured on the development VM: at most about 6s (see the RESULTS note).
  EXPORT_BOUND = 30.0
  def test_adversarial_exports_of_1_6m_characters_stay_within_the_bound
    {
      'question-capped' => '?' * 8 + 'x' * 120 + '=', 'question-pad' => '?' + 'x' * 126 + '=',
      'long-names' => 'n' * 127 + '=v&', 'quoted-eq-control' => '"model"=v ', 'encoded-quoted' => '"model"%20\\n: "keep", ',
      'lb-escapes' => "--token\n" + '%41' * 40 + '=v ', 'word-separators' => '--token a=b:c%3Dd ',
      # URL path pairs and query pairs (few enough URLs to stay within the export's node budget).
      'urls' => 'https://example.invalid/' + 'a;b=1,' * 60 + '?q=' + 'x?' * 4 + 'y=1 '
    }.each do |label, unit|
      value = (unit * (1_600_000 / unit.length + 1))[0, 1_600_000]
      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      out = prose(value)
      elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started
      assert_kind_of String, out, label
      assert_operator elapsed, :<, EXPORT_BOUND, "#{label}: export of 1.6M characters took #{elapsed.round(2)}s"
    end
  end

  def test_group_10_name_to_separator_gaps
    assert_equal "password  =#{R}", prose("password  =#{V} keep")
    assert_equal "password\t=#{R}", prose("password\t=#{V} keep")
    assert_equal "password%20=#{R}", prose("password%20=#{V} keep")
    assert_equal "model         =#{R}", prose('model         =keep')
  end

  def test_group_11_and_12_encoded_separators_and_escaped_name_read_back
    assert_equal "password%3D#{R}", prose("password%3D#{V} keep")
    assert_equal "user%5Bpassword%5D=#{R}", prose("user%5Bpassword%5D=#{V} keep")
  end

  def test_group_13_dashed_names_and_line_breaks
    assert_equal "run --password #{R} --model keep", prose("run --password #{V} --model keep")
    assert_equal "run --password\n#{R} keep", prose("run --password\n#{V} keep")
    assert_equal "run --password\n\nkeep this", prose("run --password\n\nkeep this")
    assert_equal "run -password #{R} keep", prose("run -password #{V} keep")
    assert_equal 'run -p keep', prose('run -p keep')
    assert_equal "run --password :#{R}", prose('run --password ://SYNTHETIC_PRIVATE keep')
  end

  def test_group_14_quoted_names_with_equals
    assert_equal %("password"=#{R}), prose(%("password"=#{V} keep))
    assert_equal "'password'%3D#{R}", prose("'password'%3D#{V} keep")
    assert_equal "&quot;password&quot;=#{R}", prose("&quot;password&quot;=#{V} keep")
  end

  # The spec's patterns read `\s`, `\b` and case-insensitive letters as Python does over str.
  def test_value_shapes_read_whitespace_boundaries_and_case_as_the_spec
    assert_equal R, prose("Bearer \u001cabcdefgh1234")
    assert_equal R, prose("Basic \u3000abcdefgh1234")
    assert_equal "q #{R}", prose("q Bearer \u001d--password")
    assert_equal R, prose("Bearer abcdefgh12\u0130tail")
    assert_equal "\u0301#{R}", prose("\u0301sk-" + 'a' * 26)
  end

  def test_magic_bytes_ignore_the_unused_low_bits_of_the_last_character
    assert_equal '?a=<omitted: 3 base64 chars> x', framed('?a=/// x')
    assert_equal '<omitted: 22 base64 chars>', framed('iVBORw0KGgoiVBORw0KGgo', 'x')
  end

  def test_an_attribute_name_past_the_depth_budget_replaces_only_that_attribute
    deep = (['a'] * 33).join('.')
    assert_equal({'llm.model_name' => 'gpt', 'input.value' => 'hi', deep => R},
                 P.scrub_attributes({'llm.model_name' => 'gpt', 'input.value' => 'hi', deep => 'v'}))
  end

  # Known secrets are applied one at a time, longest first, before the pair rule, as the spec's reference does: the
  # pair rule reads the name a secret's placeholder leaves beside it ('pwd', so the value is replaced). The rest of the
  # second secret ('pwd') is closed beside the placeholder only after the pair rule.
  def test_overlapping_secrets_next_to_a_credential_name_leave_the_name_to_the_pair_rule
    secrets = %w[ABCDEFGH12 EFGH12pwd]
    assert_equal "login #{R}=#{R}", P.scrub('login ABCDEFGH12pwd=TAILVTAILV keep', secrets, nil, 0, P::Budget.new, false, false, {}, false)
    assert_equal "https://h.example/p?q=#{R}=#{R}", P.scrub('https://h.example/p?q=ABCDEFGH12pwd=TAILVTAILV', secrets)
    assert_equal "login #{R}pwd=#{R}", P.clean_string('login ABCDEFGH12pwd=TAILVTAILV keep', ['ABCDEFGH12'], P::Budget.new, false)
  end

  # Structured JSON is one JSON value with only whitespace after it; with any other trailing text the value is not
  # exported (the malformed-JSON rule fails closed), so no marker reaches an export.
  def test_json_followed_by_text_exports_no_marker
    ['{} password=TAILV tail', '[1] token: TAILV', '{"a":1} trailing password=TAILV', '{"a":"x"} password=TAILV',
     '{}api_keydata:TAILVTAILV'].each do |value|
      exported = P.scrub_attributes({'output.value' => value, 'gen_ai.usage.input_tokens' => 3})
      refute_includes exported.to_s, 'TAILV', value
      assert_equal 3, exported['gen_ai.usage.input_tokens'], value
    end
  end
end
