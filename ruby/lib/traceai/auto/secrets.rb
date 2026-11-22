# frozen_string_literal: true
# Dependency-free: this API is available before Bundler and before lazy init.
require_relative 'config'
module TraceAI
  module Auto
    class SecretRegistry
      CAPACITY = 1024
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
        raw = String.new(value)
        secret = Config.utf8(raw)
        return false if secret.length < 8
        # A secret that is a byte slice cutting through a multi-byte character reads as U+FFFD there, which matches less than the
        # valid text around it: its longest runs of valid characters (at most Config::VALID_RUNS_PER_SECRET) are registered as
        # secrets of their own, before the secret, so the secret is the newest entry. One call therefore takes at most
        # VALID_RUNS_PER_SECRET + 1 places, far fewer than CAPACITY, and cannot evict the seeded Future AGI keys or the environment
        # secrets by itself (the registry is not pinned; many registrations still age out the oldest, as before).
        Config.valid_runs(raw).each { |run| remember(run.freeze) }
        remember(secret.freeze)
        true
      rescue StandardError
        # Do not log values, including in exception messages. Future snapshots
        # fail closed rather than exporting with a partially updated registry.
        @failed = true
        false
      end

      def remember(secret)
        @mutex.synchronize do
          if @values.delete(secret)
            @values[secret] = true # Hash insertion order is the observation LRU.
          else
            @values[secret] = true
            @values.shift if @values.length > CAPACITY
            @snapshot = nil
          end
        end
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

      private :remember
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
