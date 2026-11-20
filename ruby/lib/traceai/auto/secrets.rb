# frozen_string_literal: true
# Dependency-free: this API is available before Bundler and before lazy init.
require_relative 'config'
module TraceAI
  module Auto
    class SecretRegistry
      CAPACITY = 1024
      # What a replaced secret becomes (the privacy catalog's credential replacement, Privacy::REDACTED).
      PLACEHOLDER = '<redacted>'
      # The shortest edge overlap between two secrets that is derived from.
      MIN_EDGE = 4
      ENV_NAME = /KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH/i
      class Unavailable < StandardError; end

      def initialize
        @mutex = Mutex.new
        @values = {}
        @snapshot = [].freeze
        @failed = false
      end

      def register(value)
        return false unless Object.instance_method(:class).bind_call(value) == String
        # Copy native strings; never retain mutable application-owned storage or
        # invoke a stringifier/getter on arbitrary application objects. Read the
        # copy as UTF-8 whatever the process locale tagged it (under LANG=C an
        # environment value is US-ASCII), so that it matches exported UTF-8 text.
        secret = Config.utf8(String.new(value))
        return false if secret.length < 8
        secret.freeze
        @mutex.synchronize do
          if @values.delete(secret)
            @values[secret] = true # Hash insertion order is the observation LRU.
          else
            @values[secret] = true
            @values.shift if @values.length > CAPACITY
            @snapshot = nil
            derive_variants(secret)
          end
        end
        true
      rescue StandardError
        # Do not log values, including in exception messages. Future snapshots
        # fail closed rather than exporting with a partially updated registry.
        @failed = true
        false
      end

      def seed(config, env = ENV)
        # A locale-tagged name with non-ASCII bytes is not a valid string for a
        # regular expression until it is read as UTF-8.
        env.each { |name, value| register(value) if ENV_NAME.match?(Config.utf8(name)) }
        # Resolution selects a pair for authentication, not for privacy. Every FI
        # value read from a recognized source remains a secret, including ignored
        # halves and complete lower-priority pairs. Config owns immutable copies.
        config.credential_values.each { |value| register(value) }
      rescue StandardError
        @failed = true
      end

      def snapshot
        raise Unavailable if @failed
        # One cached lookup per exported span; no lock, scan, sort or ancestry
        # bookkeeping on the span-start path. Rebuild only after membership changes.
        @snapshot || @mutex.synchronize { @snapshot ||= @values.keys.sort_by { |value| -value.length }.freeze }
      end

      def inspect = '#<TraceAI::Auto::SecretRegistry>'

      private

      # A secret learned after a span was captured with an overlapping secret already replaced leaves the rest of
      # the longer one next to the placeholder ("PFX-LONGPART-<redacted>"). For every registered secret that
      # overlaps the new one, register the text that remains once the overlapping part is the placeholder, in both
      # directions, so the export replaces what is left as well. Variants carry the placeholder and are applied
      # after the secrets themselves (see Privacy.clean_string); they are not derived from again.
      def derive_variants(secret)
        return if secret.include?(PLACEHOLDER)
        variants = []
        @values.each_key do |other|
          next if other == secret || other.include?(PLACEHOLDER)
          variants.concat(variants_between(secret, other))
        end
        variants.uniq.each do |variant|
          next if variant.length < 8 || variant == PLACEHOLDER || @values.key?(variant)
          @values[variant.freeze] = true
          @values.shift if @values.length > CAPACITY
        end
      end

      def variants_between(left, right)
        found = []
        [[left, right], [right, left]].each do |inner, outer|
          found << outer.gsub(inner, PLACEHOLDER) if outer.include?(inner)
        end
        [[left, right], [right, left]].each do |first, second|
          edge = edge_overlap(first, second)
          next unless edge
          found << first[0, first.length - edge] + PLACEHOLDER
          found << PLACEHOLDER + second[edge..]
        end
        found
      end

      # The longest part of `second`'s beginning that `first` ends with, at least MIN_EDGE long, or nil.
      def edge_overlap(first, second)
        limit = [first.length, second.length].min - 1
        return nil if limit < MIN_EDGE || !first.include?(second[0, MIN_EDGE])
        limit.downto(MIN_EDGE) { |length| return length if first.end_with?(second[0, length]) }
        nil
      end
    end

    SECRET_REGISTRY = SecretRegistry.new
    class << self
      # Register an extra literal credential (8+ characters). No diagnostics ever
      # expose the registry. Invalid values are ignored, without calling user code.
      def register_secret(value) = SECRET_REGISTRY.register(value)
      def secret_values = SECRET_REGISTRY.snapshot
    end
  end
end
