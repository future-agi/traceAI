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

      def should_sample?(**arguments)
        result = @delegate.should_sample?(**arguments)
        samplers = OpenTelemetry::SDK::Trace::Samplers
        return result unless result.sampled?

        parent = OpenTelemetry::Trace.current_span(arguments[:parent_context]).context
        state = result.tracestate
        if @parent_based && parent.valid?
          # A sampled parent's probability belongs to that trace, not to this
          # process's root sampler. Missing/invalid metadata stays unknown.
          return result unless parent.trace_flags.sampled?
          rate = SamplingRatio.from_tracestate(parent.tracestate)
          return result unless rate
        else
          rate = @rate
          state = state.set_value('fi', SamplingRatio.state_value(rate))
        end
        samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE,
                             attributes: (result.attributes || {}).merge(SamplingRatio::ATTRIBUTE => rate),
                             tracestate: state)
      end
    end
  end
end
