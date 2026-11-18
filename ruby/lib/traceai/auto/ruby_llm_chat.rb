# frozen_string_literal: true
module TraceAI
  module Auto
    # Identity of a span made by the dependency-free pipeline, kept in a
    # LocalContext so that spans started below it (wire chat spans, tool spans)
    # join its trace and follow its sampling decision, as ParentBased does.
    LocalSpan = Struct.new(:trace_id, :span_id, :sampled, :ratio)
    # An internal span the agent opened around application code.
    OpenSpan = Struct.new(:name, :context, :span, :local, :parent_id, :started, :attributes, :room, :dropped)
    # RubyLLM runs tools inside Chat#ask/#complete through the private
    # Chat#execute_tool(tool_call), on every supported version (1.2 to 2.x).
    # Prepended once RubyLLM::Chat is defined; each call asks the agent whether
    # to trace, so a disabled or inactive agent leaves the call untouched.
    module RubyLLMChat
      def complete(...)
        Auto.ruby_llm_complete(self) { super }
      end

      private

      def execute_tool(tool_call)
        Auto.ruby_llm_tool(self, tool_call) { super }
      end
    end
    class << self
      RUBY_LLM_TARGET = 'RubyLLM::Chat'
      def ruby_llm_wanted? = !!@config&.enabled?('ruby_llm') && !@config.truthy?('sdk_disabled')
      # Installed on the deferred path: when the agent starts, after Bundler
      # boot, and whenever RubyLLM::Chat is defined later (Zeitwerk loads it at
      # first use). Never resolves an autoload. `settled` callers (an AI request,
      # a report) run when no class body is still being defined, so only they
      # may conclude that this RubyLLM has no Chat#execute_tool; they also find
      # a Chat that was loaded after the startup hooks closed.
      def install_ruby_llm(settled: false)
        return unless ruby_llm_wanted?
        chat = target(RUBY_LLM_TARGET) || (settled && loaded_ruby_llm_chat)
        return unless chat
        unless chat.private_method_defined?(:execute_tool) || chat.method_defined?(:execute_tool)
          if settled && !@ruby_llm_skipped
            @ruby_llm_skipped = true
            note('RubyLLM::Chat#execute_tool not found; RubyLLM tool spans skipped')
          end
          return
        end
        prepend_once(chat, RubyLLMChat)
        @ruby_llm_installed = true if active?
      rescue StandardError => e
        debug(e)
      end
      # RubyLLM::Chat if it is already loaded; a pending autoload stays pending.
      def loaded_ruby_llm_chat
        return unless Object.const_defined?(:RubyLLM, false) && !Object.autoload?(:RubyLLM)
        namespace = Object.const_get(:RubyLLM, false)
        return unless namespace.is_a?(Module) && !namespace.autoload?(:Chat) && namespace.const_defined?(:Chat, false)
        chat = namespace.const_get(:Chat, false)
        return unless chat.is_a?(Class)
        observe_target(chat)
        chat
      rescue StandardError, NameError => e
        debug(e); nil
      end
      def note(message)
        warn "traceai-auto: #{message}" if @config&.truthy?('auto_debug')
      rescue StandardError
        nil
      end
      def ruby_llm_status
        install_ruby_llm(settled: true) if active? && !@ruby_llm_installed
        return 'instrumented' if @ruby_llm_installed
        @ruby_llm_skipped ? 'unsupported' : 'pending'
      end
      def ruby_llm_tracing? = active? && @ruby_llm_installed && ruby_llm_wanted?
      def ruby_llm_key = @sdk ? (@ruby_llm_sdk_key ||= OpenTelemetry::Context.create_key('traceai.ruby_llm.chat')) : :traceai_ruby_llm
      # Contexts of the asks in progress, by chat, for tools that RubyLLM runs
      # on its own threads or fibers (concurrent tool execution). An entry lives
      # only while its ask runs: a tool run later (Chat#step, #run_tools) never
      # joins a finished ask.
      def ruby_llm_contexts = (@ruby_llm_contexts ||= ObjectSpace::WeakMap.new)
      CONTENT_SWITCHES = %w[hide_inputs hide_outputs hide_input_text hide_output_text hide_input_messages hide_output_messages].freeze

      # The outermost complete of a chat with tools opens one invoke_agent span
      # when no span is current, so the chat requests and tool executions of the
      # ask share one trace. With an application span already current that span
      # is the parent. Nested completes (RubyLLM 1.x recursion) add nothing.
      def ruby_llm_complete(chat)
        # A RubyLLM completion is an AI request: it starts a waiting agent, as
        # the first wire-matched request would a moment later.
        begin
          start({}, :request) if !active? && lazy_ready?
        rescue StandardError => e
          debug(e)
        end
        plan = ruby_llm_scope(chat)
        return yield unless plan
        scoped, open, previous = plan
        begin
          return internal_span(scoped, 'invoke_agent', {'gen_ai.span.kind' => 'AGENT', 'gen_ai.operation.name' => 'invoke_agent'}, remember: chat) { yield } if open
          remember_ruby_llm(chat, scoped)
          with_context(scoped) { yield }
        ensure
          remember_ruby_llm(chat, previous)
        end
      end
      # [scoped context, whether to open invoke_agent, the entry to restore].
      def ruby_llm_scope(chat)
        return unless ruby_llm_tracing?
        # Includes what a context helper kept before the agent started.
        context = request_context
        key = ruby_llm_key
        return if context.value(key).equal?(chat)
        scoped = context.set_value(key, chat)
        untraced = @sdk && OpenTelemetry::Common::Utilities.untraced?(context)
        [scoped, !untraced && ruby_llm_tools?(chat) && !parent_span?(scoped), ruby_llm_contexts[chat]]
      rescue StandardError => e
        debug(e); nil
      end
      # Whether the chat offers the model any tool: its own (@tools) and, on
      # RubyLLM 2.x, MCP servers, code execution, skills and file tools. Reads
      # instance state only; Chat#tools could list remote MCP servers.
      def ruby_llm_tools?(chat)
        read = ->(object, name) { Kernel.instance_method(:instance_variable_get).bind_call(object, name) }
        filled = ->(value) { [Hash, Array].include?(Codec.type(value)) && !value.empty? }
        return true if %i[@tools @file_tools @skills].any? { |name| filled.call(read.call(chat, name)) }
        return true unless read.call(chat, :@code_execution_tool).nil?
        mcp = read.call(chat, :@mcp)
        !mcp.nil? && filled.call(read.call(mcp, :@servers))
      rescue StandardError => e
        debug(e); false
      end
      def remember_ruby_llm(chat, context)
        ruby_llm_contexts[chat] = context
      rescue StandardError => e
        debug(e)
      end
      def parent_span?(context)
        return OpenTelemetry::Trace.current_span(context).context.valid? if @sdk
        !local_parent(context).nil?
      end
      def local_parent(context)
        return if @sdk || !context.instance_of?(LocalContext)
        span = context.value(:traceai_span)
        span if span.instance_of?(LocalSpan)
      end

      def ruby_llm_tool(chat, tool_call)
        plan = ruby_llm_tool_plan(chat, tool_call)
        return yield unless plan
        internal_span(*plan, result: true) { yield }
      end
      def ruby_llm_tool_plan(chat, tool_call)
        return unless ruby_llm_tracing?
        attributes = ruby_llm_tool_attributes(tool_call)
        return unless attributes
        context = request_context
        # A tool RubyLLM runs on another thread or fiber starts without the
        # ask's context; parent it under the ask it belongs to.
        context = ruby_llm_contexts[chat] || context unless parent_span?(context)
        return if @sdk && OpenTelemetry::Common::Utilities.untraced?(context)
        [context, "execute_tool #{attributes['gen_ai.tool.name']}".rstrip, attributes]
      rescue StandardError => e
        debug(e); nil
      end
      def ruby_llm_tool_attributes(tool_call)
        name = tool_call.name
        id = tool_call.respond_to?(:provider_call_id) ? tool_call.provider_call_id : nil
        id ||= tool_call.id
        attributes = {'gen_ai.span.kind' => 'TOOL', 'gen_ai.operation.name' => 'execute_tool', 'gen_ai.tool.type' => 'function'}
        attributes['gen_ai.tool.name'] = attributes['tool.name'] = name.to_s if name
        attributes['gen_ai.tool.call.id'] = id.to_s if id
        attributes.merge(tool_content(tool_call.arguments, %w[gen_ai.tool.call.arguments input.value], 'input.mime_type'))
      rescue StandardError => e
        debug(e); nil
      end
      # Tool results: text as it is, data as JSON. Halt and similar wrappers
      # expose their text through #content. Never calls application #to_s.
      def ruby_llm_result_attributes(result)
        value = result
        value = value.content if !(Codec.type(value) <= String) && Codec.type_name(value).end_with?('::Halt') && value.respond_to?(:content)
        tool_content(value, %w[gen_ai.tool.call.result output.value], 'output.mime_type')
      rescue StandardError => e
        debug(e); {}
      end
      # Tool arguments and results are message content: the model wrote the
      # arguments, and the result goes back to it as a message. Any content
      # hide switch hides both, so no copy is exported raw. A value over the
      # wire body limit (at most 512 KiB here) is left out, as wire bodies are,
      # never cut: a cut JSON text would fail privacy scrubbing and take the
      # span's other attributes with it.
      def tool_content(value, keys, mime_key)
        return keys.to_h { |key| [key, '__REDACTED__'] } if CONTENT_SWITCHES.any? { |switch| @config.hidden?(switch) }
        text = Codec.type(value) == String ? value : Codec.generate(value)
        cap = [@config['wire_max_body_bytes'].to_i, 1 << 20].min / 2
        return {'traceai.wire.truncated' => true} if text.bytesize > cap
        keys.to_h { |key| [key, text] }.merge(mime_key => Codec.type(value) == String ? 'text/plain' : 'application/json')
      rescue StandardError => e
        debug(e); {}
      end

      # Run the block inside a new INTERNAL span, current for the block. The
      # block always runs exactly once; its result and exception are passed on
      # unchanged. Attributes are scrubbed and masked before the span exists,
      # as wire spans are, and an exception's message is never recorded raw.
      def internal_span(parent, name, attributes, result: false, remember: nil)
        open = begin
          open_internal_span(parent, name, attributes)
        rescue StandardError => e
          debug(e); nil
        end
        return yield unless open
        remember_ruby_llm(remember, open.context) if remember
        error = nil; value = nil; finished = false
        begin
          value = with_context(open.context) { yield }
          finished = true
          value
        rescue Exception => e # rubocop:disable Lint/RescueException
          error = e
          raise
        ensure
          extra = finished && result ? ruby_llm_result_attributes(value) : {}
          close_internal_span(open, extra, error)
        end
      end
      def open_internal_span(parent, name, raw)
        context_attributes = parent.value(@attributes_key) || {}
        attributes = Privacy.mask(Privacy.scrub_attributes(context_attributes.merge(raw)), @config)
        name = Privacy.best_effort_scrub(name)
        name = 'execute_tool' if name.empty?
        limit = projection_limit
        if @sdk
          sampler = @provider.sampler if @provider.equal?(@own_provider)
          ratio = sampler.ratio_for(OpenTelemetry::Trace.current_span(parent).context) if sampler.is_a?(RatioSampler)
        else
          ref = local_parent(parent)
          parent_id = ref ? ref.span_id : "\0".b * 8
          trace_id, sampled, ratio = ref ? [ref.trace_id, ref.sampled, ref.ratio] : local_root
        end
        budget = ratio && limit.positive? ? limit - 1 : limit
        projection = AttributeProjection.project(attributes, budget)
        room = budget - projection.attributes.size
        if @sdk
          span = @tracer.start_span(name, with_parent: parent, kind: :internal, attributes: projection.attributes)
          context = OpenTelemetry::Trace.context_with_span(span, parent_context: parent)
          OpenSpan.new(name, context, span, nil, nil, nil, nil, room, projection.dropped)
        else
          local = LocalSpan.new(trace_id, Random.urandom(8), sampled, ratio)
          OpenSpan.new(name, parent.set_value(:traceai_span, local), nil, local, parent_id, Time.now, projection.attributes, room, projection.dropped)
        end
      end
      def close_internal_span(open, extra, error)
        finished = Time.now
        extra = Privacy.mask(Privacy.scrub_attributes(extra), @config) unless extra.empty?
        extra = extra.merge('error.type' => Codec.type_name(error)) if error
        projection = AttributeProjection.project(extra, [open.room, 0].max)
        dropped = open.dropped + projection.dropped
        description = if error.nil? then nil
                      elsif Privacy.hide_errors?(@config) then '__REDACTED__'
                      else Privacy.best_effort_scrub(error.message.to_s)
                      end
        if open.span
          span = open.span
          span.add_attributes(projection.attributes) unless projection.attributes.empty?
          span.status = OpenTelemetry::Trace::Status.error(description) if error
          if dropped.positive? && span.respond_to?(:to_span_data)
            span.instance_variable_set(:@traceai_projection_dropped, dropped)
            span.singleton_class.prepend(ProjectedSpan)
          end
          span.finish(end_timestamp: finished)
        elsif open.local.sampled
          local = open.local
          attributes = open.attributes.merge(projection.attributes)
          tracestate = ''
          if local.ratio
            attributes = attributes.merge(SamplingRatio::ATTRIBUTE => local.ratio) if projection_limit.positive?
            tracestate = "fi=#{SamplingRatio.state_value(local.ratio)}"
          end
          record = ExportRecord.new(open.name, :internal, LocalStatus.new(error ? 2 : 0, description.to_s), open.parent_id,
                                    attributes.size + dropped, 0, 0, (open.started.to_r * 1_000_000_000).to_i, (finished.to_r * 1_000_000_000).to_i,
                                    attributes, [], [], @provider.resource, ExportScope.new('traceai-auto', VERSION, {}),
                                    local.span_id, local.trace_id, 1, tracestate, false)
          @processor.enqueue(record)
        end
      rescue StandardError => e
        debug(e)
      end
    end
  end
end
