# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/export'

# The fragment matcher's work is bounded for one value (MATCH_WORK) and, together, for every value of one span: values
# past the span's bound are replaced by the placeholder, and the scrub of the span ends in bounded time.
class SpanWorkBoundTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  PLACEHOLDER = P::REDACTED

  # 300 secrets of 300 characters that never occur in the text, and a value of 200 placeholders each followed by one
  # character: the matcher reads every secret and changes nothing, under 1 million units of work, well under
  # MATCH_WORK for one value.
  SECRETS = Array.new(300) { |i| format('unrelated-secret-%03d-', i) + ('s' * 280) }.freeze
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
    cleaned = P.scrub({'messages' => values}, SECRETS)['messages']
    assert_equal values.length, cleaned.length
    kept = cleaned.take_while { |item| item == VALUE }
    assert_operator kept.length, :>, 0, 'the first values of a span are scrubbed normally'
    assert_operator kept.length, :<, values.length - 10, 'values past the span bound are not scrubbed one by one forever'
    assert_equal [PLACEHOLDER], cleaned.drop(kept.length).uniq, 'every value past the bound is the placeholder, even one with no placeholder in it'
  end

  # Past the bound a value costs nothing, so the time of a scrub stops growing with the number of values: four times
  # the values take about as long (a scrub with no span bound takes about four times as long, and a quadratic one
  # sixteen times). Best of three, so a busy machine does not decide.
  def best_of_three(count)
    values = Array.new(count) { VALUE }
    Array.new(3) { timed { P.scrub({'messages' => values}, SECRETS) }.last }.min
  end

  def with_span_bound(value)
    saved = P::MATCH_SPAN_WORK
    P.send(:remove_const, :MATCH_SPAN_WORK); P.const_set(:MATCH_SPAN_WORK, value)
    yield
  ensure
    P.send(:remove_const, :MATCH_SPAN_WORK); P.const_set(:MATCH_SPAN_WORK, saved)
  end

  def test_the_time_of_a_span_scrub_stops_growing_once_the_bound_is_reached
    limit = 4_000_000
    small, large = with_span_bound(limit) { [best_of_three(100), best_of_three(400)] }
    assert_operator large, :<, small * 2, "100 values took #{small.round(3)}s, 400 values #{large.round(3)}s"
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

  # A caller's secret with invalid bytes is read as the registry reads it: the invalid bytes become U+FFFD and the
  # valid characters stay. The text it protects holds the same replacement characters.
  def test_a_provider_response_secret_with_invalid_bytes_around_a_valid_run_is_replaced_not_skipped
    broken = "\xFF\xFEVALIDRUN-1234\xFF".dup.force_encoding(Encoding::UTF_8)
    refute broken.valid_encoding?
    response = {'choices' => [{'index' => 0, 'message' => {'role' => 'assistant', 'content' => "hello \uFFFD\uFFFDVALIDRUN-1234\uFFFD world"}}]}
    cleaned = P.scrub_provider_response(response, [broken])
    assert_equal "hello #{PLACEHOLDER} world", cleaned['choices'][0]['message']['content']
    refute_includes JSON.generate(cleaned), 'VALIDRUN-1234'
  end

  def test_the_other_scrub_entry_points_read_an_invalid_byte_secret_the_same_way
    broken = "\xFF\xFEVALIDRUN-1234\xFF".dup.force_encoding(Encoding::UTF_8)
    text = "hello \uFFFD\uFFFDVALIDRUN-1234\uFFFD world"
    assert_equal "hello #{PLACEHOLDER} world", P.scrub(text, [broken])
    assert_equal "hello #{PLACEHOLDER} world", P.clean_string(text, [broken])
  end

  # The cost of a span grows with its placeholders times the registered secrets. A full registry of long secrets and
  # a few hundred placeholder-bearing values pass the span bound; the span keeps its identity and the values past the
  # bound are the placeholder.
  def span_with(attributes)
    memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
    provider.tracer('app').start_span('identity-span').finish
    record = memory.finished_spans.first
    record.attributes = attributes
    record.total_recorded_attributes = attributes.size
    record
  end

  def test_a_span_that_passes_the_bound_keeps_its_name_and_resource_and_replaces_the_values_past_it
    registry = Array.new(1024) { |i| format('registered-credential-%04d-', i) + ('k' * 180) }
    value = ('x' + PLACEHOLDER) * 100
    attributes = (0...200).to_h { |i| ["app.value.#{i}", value] }
    record = span_with(attributes)
    config = TraceAI::Auto::Config.new({}, env: {})
    copy = TraceAI::Auto::Privacy.snapshot(record, TraceAI::Auto::ExportResource.new({'service.name' => 'checkout-service'}), config, registry)
    assert_equal 'identity-span', copy.name
    assert_equal 'checkout-service', copy.resource.attributes['service.name']
    kept = (0...200).take_while { |i| copy.attributes["app.value.#{i}"] == value }
    assert_operator kept.length, :>, 0
    assert_operator kept.length, :<, 200, 'the bound is reached'
    # The value that reached the bound is the placeholder under its own name; past it the names are the placeholder
    # too, so the later attributes share one entry.
    named, others = copy.attributes.partition { |key, _| key.start_with?('app.value.') }
    assert_equal({PLACEHOLDER => PLACEHOLDER}, others.to_h)
    assert_equal [value, PLACEHOLDER], named.map(&:last).uniq.sort_by { |item| item == PLACEHOLDER ? 1 : 0 }
    assert_operator named.length, :<=, kept.length + 1
  end
end
