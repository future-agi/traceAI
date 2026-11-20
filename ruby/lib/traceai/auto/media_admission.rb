# frozen_string_literal: true
require_relative 'export'
module TraceAI
  module Auto
    # Observe complete SDK candidates at its admission boundaries, without
    # changing host values, return values, locking, exceptions or configured
    # limits. Only agent-attached providers participate. Sidecars contain bounded
    # omission decisions and loss flags, not a second copy of application media.
    #
    # The bookkeeping depends on private SDK span internals, so it is armed only
    # after verify! has proven, by running the hooks on a real SDK span, that
    # they behave as the admission model assumes. Otherwise it fails closed: every
    # agent-covered span's sidecar is marked failed, so its export carries no
    # media, and tracing continues.
    module MediaAdmission
      PROBE_KEY = :traceai_media_probe
      PAYLOAD = 'AQIDBA=='
      @verified = nil
      @reported = false

      class << self
        # True only when the hooks were proven on the running SDK. Computed once
        # on first use; never guessed from a version string.
        def verified?
          return true if Thread.current[PROBE_KEY]
          verify! if @verified.nil?
          @verified
        end

        def verify!
          @verified = Probe.run
          report_unverified unless @verified
          @verified
        end

        private

        def report_unverified
          return if @reported
          @reported = true
          warn 'traceai-auto: this OpenTelemetry SDK could not be verified for media handling; media is omitted from exported spans'
        rescue StandardError
          nil
        end
      end

      # Runs the hooks on a real SDK span and checks every behaviour the model
      # relies on. Any mismatch, missing method or error means "not proven".
      module Probe
        module_function

        def run
          span_class = OpenTelemetry::SDK::Trace::Span
          return false unless hooks_present?(span_class)
          return false unless OpenTelemetry::SDK::Trace::SpanLimits.method_defined?(:event_attribute_length_limit)
          Thread.current[PROBE_KEY] = true
          begin
            behaves?(span_class)
          ensure
            Thread.current[PROBE_KEY] = nil
          end
        rescue StandardError, ScriptError
          false
        end

        # The SDK's own definition, behind anything prepended to the class.
        def sdk_method(span_class, name)
          method = span_class.instance_method(name)
          method = method.super_method while method && method.owner != span_class
          method
        rescue NameError
          nil
        end

        def hooks_present?(span_class)
          {trim_span_attributes: 1, append_event: 2, trim_links: 3}.all? do |name, arity|
            sdk_method(span_class, name)&.arity == arity
          end && %i[to_span_data add_event add_link].all? { |name| sdk_method(span_class, name) }
        end

        def probe_class(span_class)
          # A copy of the module, so the probe never doubles the live prepend.
          Class.new(span_class) do
            prepend(MediaAdmission.dup)
            prepend(Module.new { private def traceai_media_enabled? = true })
          end
        end

        def new_span(klass, name, attributes, links, limits)
          context = OpenTelemetry::Trace::SpanContext.new(trace_id: "\1" * 16, span_id: "\2" * 8)
          klass.new(context, OpenTelemetry::Context.empty, OpenTelemetry::Trace::Span::INVALID, name, :internal,
                    OpenTelemetry::Trace::INVALID_SPAN_ID, limits, [], attributes, links, nil,
                    OpenTelemetry::SDK::Resources::Resource.create, OpenTelemetry::SDK::InstrumentationScope.new('probe', '0'))
        end

        def media(prefix)
          {prefix + '.content.chunk' => PAYLOAD, prefix + '.mime_type' => 'audio/pcm'}
        end

        def behaves?(span_class)
          klass = probe_class(span_class)
          limits = OpenTelemetry::SDK::Trace::SpanLimits.new(attribute_count_limit: 3, event_attribute_count_limit: 3, link_attribute_count_limit: 3)
          link_context = OpenTelemetry::Trace::SpanContext.new(trace_id: "\3" * 16, span_id: "\4" * 8)
          link = OpenTelemetry::Trace::Link.new(link_context, media('link'))
          span = new_span(klass, 'probe', media('a').merge('b' => 'x', 'c' => 'y'), [link], limits)
          span.add_event('probe', attributes: media('event'))
          late = OpenTelemetry::Trace::Link.new(link_context, media('late'))
          span.add_link(late)
          span.finish
          record = span.to_span_data
          sidecar = record.instance_variable_get(:@traceai_media)
          return false unless sidecar.is_a?(Hash) && !sidecar[:failed]
          attributes = sidecar[:attributes]
          return false unless attributes.is_a?(Privacy::Admission) && !attributes.failed
          # Eviction keeps the earliest attributes, and the decision made before
          # eviction survives for the payload that remains.
          return false unless record.attributes.keys == ['a.content.chunk', 'a.mime_type', 'b'] && record.total_recorded_attributes == 4
          return false unless attributes.omitted == {'a.content.chunk' => true} && attributes.lossy
          return false unless record.events.size == 1 && record.links.size == 2
          events = sidecar[:events]
          return false unless events.is_a?(Hash) && events.keys == [record.events.first.object_id]
          return false unless events.values.first.omitted.key?('event.content.chunk') && !events.values.first.failed
          links = sidecar[:links]
          return false unless links.is_a?(Hash) && links.keys.sort == record.links.map(&:object_id).sort
          return false unless links.values.all? { |item| item.is_a?(Privacy::Admission) && !item.failed && item.omitted.keys.any? { |key| key.end_with?('.content.chunk') } }
          # The export view carries none of the payloads.
          copy = Privacy.snapshot(record, Auto::ExportResource.new({}), Auto::Config.new({}, env: {}))
          text = Auto::Codec.generate([copy.attributes, copy.events.map(&:attributes), copy.links.map(&:attributes)])
          !text.include?(PAYLOAD)
        end
      end

      def to_span_data
        record = super
        if traceai_media_enabled? && !MediaAdmission.verified?
          # Hooks unproven on this SDK: the record exports without media.
          record.instance_variable_set(:@traceai_media, {attributes: nil, events: {}, links: {}, failed: true})
        elsif traceai_media_enabled?
          record.instance_variable_set(:@traceai_media, {
            attributes: @traceai_attribute_media,
            events: (@traceai_event_media || {}).dup,
            links: (@traceai_link_media || {}).dup,
            failed: @traceai_media_failed
          })
        end
        record
      end

      # Bookkeeping runs inside the application's own span calls: a failure
      # here marks the record failed (its export then fails closed) and never
      # stops the application's call.
      def add_event(name, attributes: nil, timestamp: nil)
        return super unless MediaAdmission.verified? && traceai_media_enabled?
        previous = Thread.current[:traceai_media_event]
        begin
          evidence = Privacy.media_admission(attributes, count_limit: @span_limits.event_attribute_count_limit,
                                             length_limit: @span_limits.event_attribute_length_limit)
          Thread.current[:traceai_media_event] = [self, evidence]
        rescue StandardError
          @traceai_media_failed = true
        end
        begin
          super
        ensure
          Thread.current[:traceai_media_event] = previous
        end
      end

      private

      # Only agent-attached providers participate, and only spans an agent
      # export can receive: with the Future AGI route alone, AI spans and
      # server spans. Other application spans (database, queue, HTTP client)
      # skip the bookkeeping entirely.
      def traceai_media_enabled?
        return false unless @span_processors&.any? { |processor| processor.instance_of?(CoveredAncestry) }
        return true if @traceai_media_wanted
        unless Auto.media_wanted?(@kind, @attributes, @instrumentation_scope)
          @traceai_media_skipped = true
          return false
        end
        # A span can get its AI marker after other calls. Those calls were not
        # observed, so everything recorded so far counts as lossy and its
        # export omits whatever may be media.
        @traceai_attribute_media = Privacy::Admission.new({}, true, nil) if @traceai_media_skipped
        @traceai_media_wanted = true
      rescue StandardError
        @traceai_media_failed = true
        true
      end

      def trim_span_attributes(attributes)
        return super if @traceai_ratio_restoring || !MediaAdmission.verified?
        if traceai_media_enabled?
          begin
            @traceai_attribute_media = Privacy.media_admission(attributes,
              count_limit: @span_limits.attribute_count_limit, length_limit: @span_limits.attribute_length_limit,
              previous: @traceai_attribute_media)
          rescue StandardError
            @traceai_media_failed = true
          end
        end
        result = super
        begin
          @traceai_attribute_media.omitted.keep_if { |key, _| attributes.key?(key) } if @traceai_attribute_media && attributes
        rescue StandardError
          @traceai_media_failed = true
        end
        result
      end

      def append_event(events, event)
        result = super
        return result unless MediaAdmission.verified?
        if traceai_media_enabled?
          begin
            raise Privacy::Limit if result.size > Privacy::Budget::MAX_NODES
            pending = Thread.current[:traceai_media_event]
            evidence = pending && pending[0].equal?(self) ? pending[1] : Privacy::Admission.new({}, true, true)
            @traceai_event_media ||= {}
            @traceai_event_media[result.last.object_id] = evidence unless result.empty?
            retained = result.to_h { |item| [item.object_id, true] }
            @traceai_event_media.keep_if { |id, _| retained[id] }
          rescue StandardError
            @traceai_media_failed = true
          end
        end
        result
      end

      def trim_links(links, link_count_limit, link_attribute_count_limit)
        if MediaAdmission.verified? && traceai_media_enabled? && links
          begin
            raise Privacy::Limit if links.size > Privacy::Budget::MAX_NODES
            # SDK filtering keeps the first valid links, in original order.
            evidence = links.select { |link| link.span_context.valid? }.first(link_count_limit).map do |link|
              Privacy.media_admission(link.attributes, count_limit: link_attribute_count_limit,
                previous: (@traceai_link_media || {})[link.object_id])
            end
          rescue StandardError
            @traceai_media_failed = true
          end
        end
        result = super
        if evidence && result
          @traceai_link_media = result.each_with_index.to_h { |link, index| [link.object_id, evidence[index]] }
        end
        result
      end
    end
  end
end
