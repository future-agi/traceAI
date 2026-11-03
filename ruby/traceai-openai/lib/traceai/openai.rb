# frozen_string_literal: true

require 'openai'
require 'opentelemetry-api'
require 'delegate'

module TraceAI
  # Opt-in OpenTelemetry instrumentation for the official openai Ruby SDK.
  # Request and response bodies are never recorded.
  module OpenAI
    VERSION = '0.1.0' # x-release-please-version

    def self.install!
      unless Gem::Version.new(::OpenAI::VERSION) >= Gem::Version.new('0.115.0')
        raise ArgumentError, 'traceai-openai requires openai >= 0.115.0'
      end
      # Both adapters patch Client#request. Installing them together creates
      # duplicate model spans and can change streaming behavior.
      if ::OpenAI::Client.ancestors.any? { |ancestor| ancestor.name == 'OpenTelemetry::Instrumentation::OpenAI::Patches::Client' }
        raise ArgumentError, 'Remove opentelemetry-instrumentation-openai before installing traceai-openai'
      end
      ::OpenAI::Client.prepend(ClientPatch) unless ::OpenAI::Client.ancestors.include?(ClientPatch)
    end

    def self.value(object, key)
      return nil unless object
      return object[key] || object[key.to_s] if object.is_a?(Hash)
      object.public_send(key) if object.respond_to?(key)
    end

    def self.decorate(span, response, operation)
      response = value(response, :response) || response if operation == 'responses'
      model = value(response, :model)
      id = value(response, :id)
      span.set_attribute('gen_ai.response.model', model) if model.is_a?(String)
      span.set_attribute('gen_ai.response.id', id) if id.is_a?(String)
      if operation == 'responses' && value(response, :status).to_s == 'failed'
        span.set_attribute('error.type', 'response.failed')
        span.status = OpenTelemetry::Trace::Status.error('response.failed')
      end
      usage = value(response, :usage)
      input = value(usage, operation == 'chat' ? :prompt_tokens : :input_tokens)
      output = value(usage, operation == 'chat' ? :completion_tokens : :output_tokens)
      span.set_attribute('gen_ai.usage.input_tokens', input) if input.is_a?(Integer) && input >= 0
      span.set_attribute('gen_ai.usage.output_tokens', output) if output.is_a?(Integer) && output >= 0
    end

    def self.error(span, exception)
      span.set_attribute('error.type', exception.class.name)
      span.status = OpenTelemetry::Trace::Status.error(exception.class.name)
    end

    module ClientPatch
      def request(req)
        operation = case req[:path]
                    when 'chat/completions' then 'chat'
                    when 'responses' then 'responses'
                    end
        return super unless operation && req[:method] == :post

        body = req[:body]
        model = TraceAI::OpenAI.value(body, :model)
        attributes = {'gen_ai.operation.name' => operation, 'gen_ai.provider.name' => 'openai',
                      'gen_ai.span.kind' => 'LLM', 'fi.span.kind' => 'LLM'}
        attributes['gen_ai.request.model'] = model if model.is_a?(String)
        if operation == 'chat' && req[:stream] && body.is_a?(Hash)
          # A final aggregate usage chunk is only sent when include_usage is true.
          # Do not override an explicit caller choice (including false).
          options = TraceAI::OpenAI.value(body, :stream_options)
          if options.nil?
            req[:body] = body.merge(stream_options: {include_usage: true})
          elsif options.is_a?(Hash) && !options.key?(:include_usage) && !options.key?('include_usage')
            req[:body] = body.merge(stream_options: options.merge(include_usage: true))
          end
        end
        tracer = OpenTelemetry.tracer_provider.tracer('traceai-openai', TraceAI::OpenAI::VERSION)
        span = tracer.start_span([operation, model].compact.join(' '), kind: :client, attributes: attributes)
        begin
          result = OpenTelemetry::Trace.with_span(span) { super(req) }
          if req[:stream]
            return Stream.new(result, span, operation)
          end
          TraceAI::OpenAI.decorate(span, result, operation)
          result
        rescue StandardError => e
          TraceAI::OpenAI.error(span, e)
          raise
        ensure
          span.finish unless req[:stream] && defined?(result) && result
        end
      end
    end

    # Keep the original SDK stream's metadata and close API available. A stream
    # ends its span on exhaustion, explicit close, or iterator failure.
    class Stream < SimpleDelegator
      include Enumerable

      def initialize(stream, span, operation)
        super(stream)
        @span = span
        @operation = operation
        @finished = false
      end

      def each
        return enum_for(:each) unless block_given?
        begin
          while true
            event = begin
              next_event
            rescue StopIteration
              break
            end
            yield event
          end
        ensure
          close
        end
      end

      def next
        next_event
      rescue StopIteration
        close
        raise
      end

      def to_enum(method = :each, *args)
        Kernel.instance_method(:to_enum).bind_call(self, method, *args)
      end
      alias enum_for to_enum

      def close
        return if @finished
        @finished = true
        __getobj__.close if __getobj__.respond_to?(:close)
      ensure
        @span.finish unless @span.nil?
        @span = nil
      end

      private

      def next_event
        @iterator ||= __getobj__.to_enum
        event = @iterator.next
        observe(event)
        event
      rescue StopIteration
        close
        raise
      rescue StandardError => e
        TraceAI::OpenAI.error(@span, e) unless @finished
        close
        raise
      end

      def observe(event)
        if @operation == 'responses'
          type = TraceAI::OpenAI.value(event, :type).to_s
          TraceAI::OpenAI.decorate(@span, event, @operation) if %w[response.completed response.incomplete response.failed].include?(type)
          if type == 'response.failed' || type == 'error'
            @span.set_attribute('error.type', type)
            @span.status = OpenTelemetry::Trace::Status.error(type)
          end
        else
          TraceAI::OpenAI.decorate(@span, event, @operation)
        end
      end
    end
  end
end
