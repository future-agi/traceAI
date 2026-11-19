# frozen_string_literal: true

require "roast"
require "opentelemetry-api"
require_relative "roast/version"

module TraceAI
  module Roast
    # Roast 1.1+ has no public telemetry subscriber. TaskContext is the common
    # execution-fiber boundary for both synchronous and asynchronous cogs.
    # Never observe EventMonitor: it consumes events on a *different* fiber.
    module TaskContextHooks
      def begin_execution_manager(manager)
        path = super
        TraceAI::Roast.start_element(manager, "roast.workflow", { "roast.component" => "workflow" })
        path
      end

      def begin_cog(cog)
        path = super
        TraceAI::Roast.start_element(cog, "roast.cog #{cog.type}", {
          "roast.component" => "cog", "roast.cog.type" => cog.type.to_s,
          "roast.cog.name" => cog.name.to_s,
        })
        path
      end

      def end
        element = Fiber[:path]&.last
        result = super
        TraceAI::Roast.finish_element(element)
        result
      end
    end

    INSTALL_MUTEX = Mutex.new

    class << self
      def install!(tracer: OpenTelemetry.tracer_provider.tracer("traceai-roast", VERSION))
        INSTALL_MUTEX.synchronize do
          return false if @installed

          @tracer = tracer
          @contexts = {}
          @mutex = Mutex.new
          ::Roast::TaskContext.singleton_class.prepend(TaskContextHooks)
          @installed = true
        end
      end

      def start_element(object, name, attributes)
        # Fiber[:path] is inherited by Async child fibers. The OTel context
        # stack is not necessarily inherited; use the parent Roast element to
        # link concurrent child spans, while attaching in the child's fiber so
        # nested RubyLLM spans see the cog as their parent.
        path = Fiber[:path] || []
        parent_element = path[-2]
        parent = parent_element&.cog || parent_element&.execution_manager
        parent_context = @mutex.synchronize { @contexts[parent.object_id]&.first } if parent
        span = @tracer.start_span(name, with_parent: parent_context, attributes: attributes, kind: :internal)
        context = OpenTelemetry::Trace.context_with_span(span, parent_context: parent_context || OpenTelemetry::Context.current)
        token = OpenTelemetry::Context.attach(context)
        @mutex.synchronize { @contexts[object.object_id] = [context, span, token, Fiber.current] }
      end

      def finish_element(element)
        return unless element

        object = element.cog || element.execution_manager
        entry = @mutex.synchronize { @contexts.delete(object.object_id) }
        return unless entry

        _context, span, token, fiber = entry
        begin
          if object.respond_to?(:failed?) && object.failed?
            span.status = OpenTelemetry::Trace::Status.error("Roast cog failed")
          end
          # For exceptions raised inside a cog, Ruby keeps $! in the ensure
          # block that invokes TaskContext.end (including async task failures).
          if $!
            # Exception messages/backtraces may contain prompts or secrets.
            span.status = OpenTelemetry::Trace::Status.error($!.class.to_s)
          end
        ensure
          OpenTelemetry::Context.detach(token) if Fiber.current.equal?(fiber)
          span.finish
        end
      end
    end
  end
end
