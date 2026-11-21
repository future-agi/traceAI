# frozen_string_literal: true
require_relative 'codec'
module TraceAI
  module Auto
    # RubyLLM integration. RubyLLM 2.x reports every model request
    # (chat.ruby_llm) and every tool execution (tool_call.ruby_llm) through
    # RubyLLM.instrument, which calls Support::Instrumentation.instrument with
    # the event name, a payload and a block that holds the work. The hook below
    # wraps exactly that call, so it sees one chat span per model request, one
    # tool span per tool call, streaming included, with no dependence on
    # Chat's private methods. The block runs unchanged and its value or
    # exception passes through; whatever the hook does on its own is rescued.
    #
    # Supported: RubyLLM >= 2.0, < 3 (tested at 2.0.0, 2.1.1 and 2.2.3).
    # RubyLLM 1.x has a different instrumentation module and is not hooked.
    module RubyLLMInstrumentor
      LAYER = 'ruby_llm'
      EVENTS = RUBY_LLM_EVENTS
      # RubyLLM provider slugs whose OpenTelemetry names differ.
      PROVIDERS = {'gemini' => 'gcp.gemini', 'vertexai' => 'gcp.vertex_ai', 'bedrock' => 'aws.bedrock', 'mistral' => 'mistral_ai',
                   'xai' => 'x_ai', 'azure' => 'azure.ai.openai'}.freeze
      module_function

      def event?(name) = Codec.type(name) == String && EVENTS.key?(name)

      # The hook is installed only for a RubyLLM inside the supported range. When
      # the version cannot be read, the shape of the module decides.
      def supported?
        version = Gem.loaded_specs['ruby_llm']&.version
        return Gem::Version.new(RUBY_LLM_MIN) <= version && version < Gem::Version.new(RUBY_LLM_BELOW) if version
        target = Object.const_get(:RubyLLM, false).const_get(:Support, false).const_get(:Instrumentation, false)
        target.respond_to?(:instrument)
      rescue NameError
        false
      end

      def text(value)
        klass = Codec.type(value)
        return value if klass == String
        return Codec.generate(value) if klass == Hash || klass == Array
        return value.name if klass == Symbol
        return Codec.generate(value) if [Integer, Float, TrueClass, FalseClass].include?(klass)
        # RubyLLM::Content and similar wrappers carry their text in #text.
        inner = value.respond_to?(:text) ? value.text : nil
        Codec.type(inner) == String ? inner : nil
      end

      def string(value)
        klass = Codec.type(value)
        return value if klass == String
        klass == Symbol ? value.name : nil
      end

      def provider_name(slug)
        name = string(slug)
        name && PROVIDERS.fetch(name, name)
      end

      def calls_of(message)
        calls = message.respond_to?(:tool_calls) ? message.tool_calls : nil
        calls = calls.values if calls.respond_to?(:values)
        Array(calls).select { |call| call.respond_to?(:name) && call.respond_to?(:arguments) }
      end

      def message_hash(message)
        entry = {'role' => string(message.role)}
        content = text(message.content)
        entry['content'] = content if content && !content.empty?
        id = message.respond_to?(:tool_call_id) ? string(message.tool_call_id) : nil
        entry['tool_call_id'] = id if id
        calls = calls_of(message).map { |call| {'id' => string(call.id), 'name' => string(call.name), 'arguments' => call.arguments} }
        entry['tool_calls'] = calls unless calls.empty?
        entry
      end

      def put_messages(attributes, prefix, messages)
        messages.each_with_index do |message, index|
          base = "#{prefix}.#{index}.message"
          entry = message_hash(message)
          attributes["#{base}.role"] = entry['role'] if entry['role']
          attributes["#{base}.content"] = entry['content'] if entry['content']
          attributes["#{base}.tool_call_id"] = entry['tool_call_id'] if entry['tool_call_id']
          (entry['tool_calls'] || []).each_with_index do |call, k|
            tool = "#{base}.tool_calls.#{k}.tool_call"
            attributes["#{tool}.id"] = call['id'] if call['id']
            attributes["#{tool}.function.name"] = call['name'] if call['name']
            attributes["#{tool}.function.arguments"] = Codec.generate(call['arguments']) unless call['arguments'].nil?
          end
        end
      end

      # Opens the span for an event. Returns [scope, kind] or nil.
      def open(name, payload)
        kind = EVENTS[name]
        payload = {} unless Codec.type(payload) == Hash
        case kind
        when :chat
          model = string(payload[:model])
          identity = {'gen_ai.span.kind' => 'LLM', 'gen_ai.operation.name' => 'chat', 'gen_ai.provider.name' => provider_name(payload[:provider]),
                      'gen_ai.request.model' => model, 'traceai.capture.layer' => LAYER}.compact
          # The request RubyLLM makes inside this span is already reported by it.
          scope = Auto.begin_span(['chat', model].compact.join(' '), kind: :client, identity: identity, capture: true)
        when :tool
          tool = string(payload[:tool_name])
          identity = {'gen_ai.span.kind' => 'TOOL', 'gen_ai.operation.name' => 'execute_tool', 'gen_ai.tool.name' => tool,
                      'gen_ai.tool.call.id' => string(payload[:tool_call_id]), 'gen_ai.tool.type' => 'function',
                      'gen_ai.provider.name' => provider_name(payload[:provider]), 'traceai.capture.layer' => LAYER}.compact
          scope = Auto.begin_span(['execute_tool', tool].compact.join(' '), kind: :internal, identity: identity)
        end
        scope && [scope, kind]
      end

      # Ends the span of an event. `outcome` is :ok, :aborted (the call was left
      # without a value or an exception, such as a `break` out of a streaming
      # block) or the exception that ended the call.
      def close(opened, payload, event, outcome)
        scope, kind = opened
        payload = {} unless Codec.type(payload) == Hash
        event = payload unless Codec.type(event) == Hash
        attributes = {}
        section { kind == :chat ? chat_attributes(attributes, payload, event) : tool_attributes(attributes, payload, event, outcome) }
        ok = !outcome.is_a?(Exception)
        description = nil
        unless ok
          attributes['error.type'] = Codec.type_name(outcome)
          description = begin
            string(outcome.message)
          rescue StandardError
            nil
          end
        end
        attributes['traceai.ruby_llm.incomplete'] = true if outcome == :aborted
        Auto.finish_span(scope, attributes, ok: ok, description: description)
      end

      def section
        yield
      rescue StandardError => e
        Auto.debug(e)
      end

      def chat_attributes(attributes, payload, event)
        temperature = payload[:temperature]
        attributes['gen_ai.request.temperature'] = temperature if temperature.is_a?(Numeric)
        max = payload[:max_output_tokens]
        attributes['gen_ai.request.max_tokens'] = max if max.is_a?(Integer)
        attributes['gen_ai.request.stream'] = true if payload[:streaming] == true
        messages = Array(payload[:input_messages])
        section do
          put_messages(attributes, 'gen_ai.input.messages', messages)
          last = messages.reverse_each.find { |m| m.respond_to?(:role) && string(m.role) == 'user' }
          input = last && text(last.content)
          if input && !input.empty?
            attributes['input.value'] = input; attributes['input.mime_type'] = 'text/plain'
          end
          attributes['raw.input'] = Codec.generate(messages.map { |m| message_hash(m) }) unless messages.empty?
        end
        response = event[:response]
        return unless response.respond_to?(:role) && response.respond_to?(:content)
        model = string(event[:response_model]) || (response.respond_to?(:model_id) ? string(response.model_id) : nil)
        attributes['gen_ai.response.model'] = model if model
        reason = response.respond_to?(:finish_reason) ? string(response.finish_reason) : nil
        reason ||= 'tool_calls' unless calls_of(response).empty?
        attributes['gen_ai.response.finish_reasons'] = [reason] if reason
        section { usage(attributes, event[:tokens] || (response.respond_to?(:tokens) ? response.tokens : nil)) }
        section do
          put_messages(attributes, 'gen_ai.output.messages', [response])
          content = text(response.content)
          calls = calls_of(response)
          if content && !content.empty?
            attributes['output.value'] = content; attributes['output.mime_type'] = 'text/plain'
          elsif !calls.empty?
            attributes['output.value'] = Codec.generate(calls.map { |c| {'id' => string(c.id), 'name' => string(c.name), 'arguments' => c.arguments} })
            attributes['output.mime_type'] = 'application/json'
          end
          attributes['raw.output'] = Codec.generate(message_hash(response))
        end
      end

      def usage(attributes, tokens)
        return unless tokens
        counts = {'input_tokens' => :input, 'output_tokens' => :output, 'cache_read_tokens' => :cache_read,
                  'cache_write_tokens' => :cache_write, 'output_tokens.reasoning' => :thinking}
        counts.each do |name, reader|
          value = tokens.respond_to?(reader) ? tokens.public_send(reader) : nil
          attributes["gen_ai.usage.#{name}"] = value if value.is_a?(Integer)
        end
        input = attributes['gen_ai.usage.input_tokens']; output = attributes['gen_ai.usage.output_tokens']
        attributes['gen_ai.usage.total_tokens'] = input + output if input && output
      end

      def tool_attributes(attributes, payload, event, outcome)
        arguments = payload[:tool_arguments]
        unless arguments.nil?
          json = Codec.generate(arguments)
          attributes['gen_ai.tool.call.arguments'] = json
          attributes['input.value'] = json; attributes['input.mime_type'] = 'application/json'
          attributes['raw.input'] = json
        end
        return unless outcome == :ok && event.key?(:result)
        result = event[:result]
        result = result.content if result.respond_to?(:content) && result.class.name.to_s.end_with?('Halt')
        output = text(result)
        return unless output
        attributes['gen_ai.tool.call.result'] = output
        attributes['output.value'] = output
        attributes['output.mime_type'] = %w[Hash Array].include?(Codec.type_name(result)) ? 'application/json' : 'text/plain'
        attributes['raw.output'] = Codec.generate(output)
      end
    end

    # RubyLLM hands the caller's instrumentation context to the threads and
    # fibers that run concurrent tool calls: capture_context collects one value
    # per subscriber and with_context restores each on the worker. This adds the
    # current context, so a tool span made on a worker nests under the span the
    # caller was in.
    module RubyLLMContextCarrier
      module_function

      def with_context(context, &block) = Auto.with_context(context, &block)
    end
    module RubyLLMContextHook
      def capture_context(*arguments, **options)
        captured = super
        return captured unless Auto.active?
        Array(captured) + [[RubyLLMContextCarrier, Auto.current_context]]
      rescue StandardError => e
        Auto.debug(e)
        captured
      end
    end

    # Prepended to RubyLLM::Support::Instrumentation's singleton class. Only the
    # chat and tool events that carry work (a block) get a span.
    module RubyLLMHook
      def instrument(name, payload = nil, **options, &block)
        opened = block && RubyLLMInstrumentor.event?(name) ? safely { RubyLLMInstrumentor.open(name, payload) } : nil
        return super unless opened
        event = nil
        outcome = :aborted
        begin
          result = super(name, payload, **options) { |yielded| event = yielded; block.call(yielded) }
          outcome = :ok
          result
        rescue Exception => e # rubocop:disable Lint/RescueException
          outcome = e
          raise
        ensure
          safely { RubyLLMInstrumentor.close(opened, payload, event, outcome) }
        end
      end

      private

      def safely
        yield
      rescue StandardError => e
        Auto.debug(e)
        nil
      end
    end
  end
end
