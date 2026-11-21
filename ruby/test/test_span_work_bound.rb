# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/privacy'

# The fragment matcher's work is bounded for one value (MATCH_WORK) and, together, for every value of one span: values
# past the span's bound are replaced by the placeholder, and the scrub of the span ends in bounded time.
class SpanWorkBoundTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  PLACEHOLDER = P::REDACTED

  # 300 secrets that never occur in the text, and a value of 200 placeholders each followed by one character: the
  # matcher examines every candidate for every secret and changes nothing, about 1.3 million units of work, well
  # under MATCH_WORK for one value.
  SECRETS = Array.new(300) { |i| format('unrelated-secret-%03d!', i) }.freeze
  VALUE = ('x' + PLACEHOLDER) * 200

  def timed
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    result = yield
    [result, Process.clock_gettime(Process::CLOCK_MONOTONIC) - started]
  end

  def test_one_value_of_the_span_is_far_below_the_value_bound_and_unchanged
    work = [0, 0]
    assert_equal VALUE, P.close_fragments(VALUE, SECRETS, work)
    assert_operator work[0], :<, P::MATCH_WORK / 8
    assert_equal VALUE, P.clean_string(VALUE, SECRETS)
  end

  def test_values_past_the_span_work_bound_are_replaced_and_the_scrub_ends_in_bounded_time
    values = Array.new(80) { VALUE } + ['a plain value with no placeholder in it']
    cleaned, seconds = timed { P.scrub({'messages' => values}, SECRETS)['messages'] }
    assert_equal values.length, cleaned.length
    kept = cleaned.take_while { |item| item == VALUE }
    assert_operator kept.length, :>, 0, 'the first values of a span are scrubbed normally'
    assert_operator kept.length, :<, values.length - 10, 'values past the span bound are not scrubbed one by one forever'
    assert_equal [PLACEHOLDER], cleaned.drop(kept.length).uniq, 'every value past the bound is the placeholder, even one with no placeholder in it'
    assert_operator seconds, :<, 10
  end

  # The largest ordinary span: one value of about 2 MB (the span's text budget charges each string twice) with a
  # fragment beside a placeholder, about 8 million units of work.
  def test_a_three_million_character_value_with_one_fragment_still_settles_in_a_span
    text = ('plain words ' * 170_000) + PLACEHOLDER + 'YYYYYYYYYYYY' + ' end'
    cleaned = P.scrub({'messages' => [text, 'second value']}, ['XYYYYYYYYYYYY'])['messages']
    assert_equal [('plain words ' * 170_000) + PLACEHOLDER + ' end', 'second value'], cleaned
  end

  def test_each_span_has_its_own_bound
    values = Array.new(80) { VALUE }
    first = P.scrub({'messages' => values}, SECRETS)['messages']
    second = P.scrub({'messages' => values}, SECRETS)['messages']
    assert_equal first, second
    assert_equal VALUE, second.first
  end

  def test_a_registered_secret_with_invalid_utf8_bytes_does_not_raise
    broken = "\xFF\xFE-secret-bytes!".dup.force_encoding(Encoding::UTF_8)
    refute broken.valid_encoding?
    text = "see #{PLACEHOLDER} here and ordinary-secret-value"
    assert_equal "see #{PLACEHOLDER} here and #{PLACEHOLDER}", P.clean_string(text, [broken, 'ordinary-secret-value'])
    assert_equal({'k' => "see #{PLACEHOLDER} here and #{PLACEHOLDER}"}, P.scrub({'k' => text}, [broken, 'ordinary-secret-value']))
  end

  # Controls: ordinary spans give the same bytes as before the bound existed.
  def test_a_span_with_a_few_values_is_unchanged
    secrets = ['sk-live-abcdefgh12345678', 'overlap-abcdefgh-tail']
    attributes = {
      'gen_ai.input.messages' => ['hello sk-live-abcdefgh12345678 there', "pre #{PLACEHOLDER}-tail and overlap-abcdefgh-tail"],
      'note' => 'nothing to hide',
      'n' => 3
    }
    expected = {
      'gen_ai.input.messages' => ["hello #{PLACEHOLDER} there", "pre #{PLACEHOLDER} and #{PLACEHOLDER}"],
      'note' => 'nothing to hide',
      'n' => 3
    }
    assert_equal expected, P.scrub(attributes, secrets)
    assert_equal expected, P.scrub(attributes, secrets)
  end

  def test_the_overlap_chain_control_is_unchanged
    text = 'see AAAAAAAAAAAAXYYYYYYYYYYYY here'.sub('AAAAAAAAAAAAX', PLACEHOLDER)
    assert_equal "see #{PLACEHOLDER} here", P.clean_string(text, ['XYYYYYYYYYYYY'])
  end
end
