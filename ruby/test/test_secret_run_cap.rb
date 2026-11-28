# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/secrets'

# One registration adds the secret itself and at most a few of its longest valid runs, so one value with invalid bytes can
# never push the seeded Future AGI keys and the environment secrets out of the registry.
class SecretRunCapTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  PLACEHOLDER = P::REDACTED
  CAP = 8 # the runs one secret adds, besides the secret itself

  FI_KEY = 'fi-api-key-0123456789'
  ENV_SECRET = 'env-secret-value-9876543210'

  # 2,000 valid runs of 8 characters, each different, separated by one invalid byte.
  def long_value(count = 2000) = Array.new(count) { |i| format('run-%04d', i) }.join("\xFF").b

  def seeded_registry
    registry = TraceAI::Auto::SecretRegistry.new
    registry.register(FI_KEY)
    registry.register(ENV_SECRET)
    registry
  end

  def test_one_value_with_many_valid_runs_adds_at_most_the_cap_and_the_value_itself
    registry = seeded_registry
    before = registry.snapshot.length
    assert registry.register(long_value)
    assert_operator registry.snapshot.length - before, :<=, CAP + 1
  end

  def test_the_seeded_key_and_an_environment_secret_are_still_replaced_after_such_a_value
    registry = seeded_registry
    registry.register(long_value)
    text = "key #{FI_KEY} env #{ENV_SECRET}"
    assert_equal "key #{PLACEHOLDER} env #{PLACEHOLDER}", P.scrub(text, registry.snapshot)
  end

  def test_the_longest_runs_are_the_ones_that_are_kept
    registry = TraceAI::Auto::SecretRegistry.new
    short = Array.new(20) { |i| format('short-%02d', i) }
    long = Array.new(3) { |i| format('a-much-longer-valid-run-%02d', i) }
    registry.register((short + long).join("\xFF").b)
    long.each { |run| assert_includes registry.snapshot, run }
  end

  def test_a_caller_secret_adds_at_most_the_cap_and_the_value_itself_to_a_normalised_list
    assert_operator P.normalize_secrets([long_value]).length, :<=, CAP + 1
  end

  def test_a_value_with_few_runs_registers_all_of_them
    registry = TraceAI::Auto::SecretRegistry.new
    registry.register("\xFFRUN-ONE-123456\xFERUN-TWO-654321\xFF".b)
    assert_includes registry.snapshot, 'RUN-ONE-123456'
    assert_includes registry.snapshot, 'RUN-TWO-654321'
  end
end
