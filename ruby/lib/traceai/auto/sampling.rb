# frozen_string_literal: true

module TraceAI
  module Auto
    # Shared by SDK sampling and the SDK-free root-only export path. Loading this
    # file must not require OpenTelemetry or activate any gems.
    module SamplingRatio
      ATTRIBUTE = 'fi.sampling.ratio'

      def self.fractional?(rate)
        rate.finite? && rate > 0.0 && rate < 1.0
      end

      def self.state_value(rate) = "r:#{rate}"

      # W3C: a modified key MUST move to the left and a new key SHOULD be added
      # at the left; a full (32-member) list evicts its right-most member. The
      # SDK's set_value keeps an existing key in place and appends new keys at
      # the right, so fi would be the next member evicted downstream.
      def self.with_state(state, rate)
        others = state.to_h.reject { |key, _| key == 'fi' }.first(31).to_h
        OpenTelemetry::Trace::Tracestate.create({'fi' => state_value(rate)}.merge(others))
      end

      def self.from_tracestate(state)
        value = state['fi']
        return unless value&.start_with?('r:')
        rate = Float(value.delete_prefix('r:'))
        rate if fractional?(rate)
      rescue ArgumentError, TypeError
        nil
      end
    end

    # Decorate, never resample: the configured SDK sampler owns every decision.
    # Only providers created by this agent install this wrapper.
    class RatioSampler
      def initialize(delegate, rate, parent_based:)
        @delegate = delegate
        @rate = rate
        @parent_based = parent_based
      end

      def description = @delegate.description

      # Metadata eligibility, not a second sampling decision. Wire projection
      # reserves a slot only when this parent can supply a known probability.
      def ratio_for(parent)
        if @parent_based && parent.valid?
          return unless parent.trace_flags.sampled?
          SamplingRatio.from_tracestate(parent.tracestate)
        elsif SamplingRatio.fractional?(@rate)
          @rate
        end
      end

      def should_sample?(**arguments)
        result = @delegate.should_sample?(**arguments)
        samplers = OpenTelemetry::SDK::Trace::Samplers
        parent = OpenTelemetry::Trace.current_span(arguments[:parent_context]).context
        # SDK delegates can retain an invalid parent's entire state even when
        # allocating a new trace. None of that state belongs to this local root:
        # full-rate roots start empty; fractional roots add only their own fi.
        # Also clear inherited invalid state on DROP/RECORD_ONLY, without changing
        # those decisions or replacing an otherwise unchanged delegate result.
        reset_state = !parent.valid? && (result.sampled? || !parent.tracestate.empty?)
        state = reset_state ? OpenTelemetry::Trace::Tracestate::DEFAULT : result.tracestate
        # A valid sampled parent's probability belongs to that trace, not to this
        # process's root sampler. Missing/invalid metadata stays unknown.
        rate = ratio_for(parent) if result.sampled?
        return result unless rate || state != result.tracestate

        state = SamplingRatio.with_state(state, rate) if rate && !(@parent_based && parent.valid?)
        decision = if result.sampled?
                     samplers::Decision::RECORD_AND_SAMPLE
                   elsif result.recording?
                     samplers::Decision::RECORD_ONLY
                   else
                     samplers::Decision::DROP
                   end
        attributes = rate ? (result.attributes || {}).merge(SamplingRatio::ATTRIBUTE => rate) : result.attributes
        samplers::Result.new(decision: decision, attributes: attributes, tracestate: state)
      end
    end

    # Installed first on a private ratio-sampled provider, never transferred to
    # a host provider. SDK admission initially trims sampler attributes last;
    # restore the known sampled probability before any destination sees the span.
    class RatioSpanProcessor
      def initialize(provider)
        @provider = provider
      end

      def on_start(span, parent_context)
        return unless @provider.sampler.is_a?(RatioSampler) && span.context.trace_flags.sampled?
        # SDK delegates can preserve fi even from an invalid parent context.
        # Restore only metadata eligible for this decision, not arbitrary state.
        rate = @provider.sampler.ratio_for(OpenTelemetry::Trace.current_span(parent_context).context)
        return unless rate
        span.instance_variable_set(:@traceai_sampling_ratio, rate)
        span.singleton_class.prepend(RatioSpan)
        span.send(:traceai_restore_ratio)
      rescue StandardError => e
        Auto.debug(e)
      end

      def on_finish(_span); end
      def force_flush(timeout: nil) = 0
      def shutdown(timeout: nil) = 0
    end

    # Per-span, not a global SDK patch: all setters keep the authoritative ratio
    # first while the SDK still enforces its configured count and value limits.
    module RatioSpan
      private

      def traceai_restore_ratio
        # These candidates have already passed media admission and SDK trimming.
        # Reordering metadata must not reinterpret truncated content as new input.
        @traceai_ratio_restoring = true
        trim_span_attributes(@attributes)
      ensure
        @traceai_ratio_restoring = false
      end

      def trim_span_attributes(attributes)
        if attributes
          # Classification is a bounded boolean sidecar, not an extra SDK tag.
          # Preserve it before the reserved ratio slot can displace its marker.
          @traceai_ratio_ai ||= Auto.ai_marked?(attributes, @instrumentation_scope)
          @traceai_covered ||= MODEL_KINDS.include?(attributes['gen_ai.span.kind'])
          attributes.delete(SamplingRatio::ATTRIBUTE)
          attributes.replace({SamplingRatio::ATTRIBUTE => @traceai_sampling_ratio}.merge(attributes))
        end
        super
      end
    end
  end
end
