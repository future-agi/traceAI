# frozen_string_literal: true
require_relative 'test_helper'
require_relative 'test_wire'

class Round6TokenAlgebraTest < Minitest::Test
  def test_a8_list_content_cannot_short_circuit_later_usage_snapshots
    fixture = {
      'request' => {'method' => 'POST', 'url' => 'https://api.mistral.ai/v1/chat/completions',
                    'headers' => {'content-type' => 'application/json'}, 'json' => {'model' => 'm', 'messages' => [], 'stream' => true}},
      'response' => {'status' => 200, 'headers' => {'content-type' => 'text/event-stream'},
                     'sse' => [{'choices' => [{'delta' => {'content' => []}}]},
                               {'choices' => [{'delta' => {'content' => [{'type' => 'text', 'text' => 'hello'}]}}]},
                               {'usage' => {'prompt_tokens' => 3, 'completion_tokens' => 2, 'total_tokens' => 5}}],
                     'arrival_offsets_ms' => [10, 20, 30]}}
    attrs = FixtureHarness.run(fixture).attributes
    assert_equal 3, attrs['gen_ai.usage.input_tokens']
    assert_equal 2, attrs['gen_ai.usage.output_tokens']
    assert_equal 5, attrs['gen_ai.usage.total_tokens']
    assert_equal 'hello', attrs['output.value']
  end

  Dir[File.join(FixtureHarness::SPEC, 'fixtures/**/*.json')].sort.each do |file|
    fixture = JSON.parse(File.read(file))
    expected = fixture.fetch('expect')
    keys = expected.fetch('attributes', {}).keys + expected.fetch('absent', [])
    next unless keys.any? { |key| key.start_with?('gen_ai.usage.') }
    define_method("test_a8_#{File.basename(file, '.json').tr('-', '_')}") do
      result = FixtureHarness.run(fixture)
      refute_nil result, fixture['name']
      expected.fetch('attributes', {}).each do |key, value|
        next unless key.start_with?('gen_ai.usage.')
        assert_equal value, result.attributes[key], fixture['name'] + ' ' + key
      end
      expected.fetch('absent', []).each do |key|
        next unless key.start_with?('gen_ai.usage.')
        refute result.attributes.key?(key), fixture['name'] + ' ' + key
      end
    end
  end
end
