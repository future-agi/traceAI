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

      # Metadata eligibility, not a second sampling decision. Wire projection
      # reserves a slot only when this parent can supply a known probability.
      def ratio_for(parent)
        if @parent_based && parent.valid?
          return unless parent.trace_flags.sampled?
          SamplingRatio.from_tracestate(parent.tracestate)
        else
          @rate
        end
      end

      def should_sample?(**arguments)
        result = @delegate.should_sample?(**arguments)
        samplers = OpenTelemetry::SDK::Trace::Samplers
        return result unless result.sampled?

        parent = OpenTelemetry::Trace.current_span(arguments[:parent_context]).context
        # A sampled parent's probability belongs to that trace, not to this
        # process's root sampler. Missing/invalid metadata stays unknown.
        rate = ratio_for(parent)
        return result unless rate
        state = result.tracestate
        state = state.set_value('fi', SamplingRatio.state_value(rate)) unless @parent_based && parent.valid?
        samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE,
                             attributes: (result.attributes || {}).merge(SamplingRatio::ATTRIBUTE => rate),
                             tracestate: state)
      end
    end

    # Installed first on a private ratio-sampled provider, never transferred to
    # a host provider. SDK admission initially trims sampler attributes last;
    # restore the known sampled probability before any destination sees the span.
    class RatioSpanProcessor
      def initialize(provider)
        @provider = provider
      end

      def on_start(span, _parent_context)
        return unless @provider.sampler.is_a?(RatioSampler) && span.context.trace_flags.sampled?
        rate = SamplingRatio.from_tracestate(span.context.tracestate)
        return unless rate
        span.instance_variable_set(:@traceai_sampling_ratio, rate)
        span.singleton_class.prepend(RatioSpan)
        span.send(:trim_span_attributes, span.instance_variable_get(:@attributes))
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

      def trim_span_attributes(attributes)
        if attributes
          attributes.delete(SamplingRatio::ATTRIBUTE)
          attributes.replace({SamplingRatio::ATTRIBUTE => @traceai_sampling_ratio}.merge(attributes))
        end
        super
      end
    end
  end
end
