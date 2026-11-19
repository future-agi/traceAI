# frozen_string_literal: true
module TraceAI
  module Auto
    # Observe complete SDK candidates at its admission boundaries, without
    # changing host values, return values, locking, exceptions or configured
    # limits. Only agent-attached providers participate. Sidecars contain bounded
    # omission decisions and loss flags, not a second copy of application media.
    module MediaAdmission
      def to_span_data
        record = super
        if traceai_media_enabled?
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
        return super unless traceai_media_enabled?
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
        return super if @traceai_ratio_restoring
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
        if traceai_media_enabled? && links
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
