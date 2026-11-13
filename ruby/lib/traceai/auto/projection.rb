# frozen_string_literal: true
module TraceAI
  module Auto
    # A7: operate on sanitized candidates BEFORE SDK admission. Never change the
    # host's limits or the attributes of application/library spans.
    module AttributeProjection
      COUNT = 'traceai.capture.dropped_content_count'
      IDENTITY = %w[gen_ai.span.kind gen_ai.response.id project_name project_type project_version_name project_version_id].freeze
      OUTCOME = %w[error.type http.response.status_code gen_ai.response.finish_reasons].freeze
      MODEL = %w[gen_ai.provider.name gen_ai.request.model gen_ai.response.model gen_ai.operation.name].freeze
      PROVENANCE = %w[traceai.capture.layer traceai.wire.endpoint server.address server.port http.request.method gen_ai.request.stream gen_ai.response.time_to_first_chunk gen_ai.response.time_to_first_text traceai.audio.output_bytes gen_ai.audio.mime_type].freeze
      DEGRADATION = %w[traceai.wire.truncated traceai.wire.incomplete traceai.wire.body_unavailable traceai.wire.undecoded_encoding].freeze
      Result = Struct.new(:attributes, :dropped)
      ZERO_MUTEX = Mutex.new
      @zero_limit_spans = 0
      module_function

      def zero_limit_spans = @zero_limit_spans
      def protected_family(key)
        return 0 if key == COUNT || DEGRADATION.include?(key)
        return 1 if IDENTITY.include?(key)
        return 2 if OUTCOME.include?(key)
        return 3 if MODEL.include?(key)
        return 4 if key.start_with?('gen_ai.usage.')
        return 5 if PROVENANCE.include?(key)
      end

      # [priority, numeric order, group identity]. A whole message (including all
      # tool calls) is one atomic group, with its original message index intact.
      def content_group(key, operation)
        if (match = /\Agen_ai\.(output|input)\.messages\.(\d+)(?:\.|\z)/.match(key))
          side = match[1]; index = match[2].to_i
          return [side == 'output' ? 0 : 2, side == 'output' ? index : -index, "gen_ai.#{side}.messages.#{match[2]}"]
        end
        return [1, 0, 'output.value'] if %w[output.value output.mime_type].include?(key)
        return [4, 0, 'input.value'] if %w[input.value input.mime_type].include?(key)
        if key == 'gen_ai.audio.transcript' || key.start_with?('gen_ai.audio.transcript.')
          return [operation == 'text_to_speech' ? 3 : 1, 0, 'gen_ai.audio.transcript']
        end
        if (match = /\Agen_ai\.tool\.definitions\.(\d+)(?:\.|\z)/.match(key))
          return [3, match[1].to_i, "gen_ai.tool.definitions.#{match[1]}"]
        end
        return [3, 0, key] if key.start_with?('gen_ai.request.', 'anthropic.request.', 'anthropic.thinking.', 'gen_ai.tool.', 'gen_ai.system_instructions')
        return [1, 0, key] if key == 'output' || key.start_with?('output.', 'gen_ai.output.')
        return [4, 0, key] if key == 'input' || key.start_with?('input.', 'gen_ai.input.')
      end

      # MIME/type hints bind to their exact indexed parent, just as they do in
      # the privacy catalog. Join the affected content groups before admission;
      # a payload and its hint cannot occupy independently evictable slots. Whole
      # message groups remain atomic even when they contain several media parts.
      def coalesce_media(groups, attributes)
        roots = {}
        attributes.each do |key, value|
          parts = Privacy.path_parts(key)
          parts.pop while parts.last && Privacy.numeric_part?(parts.last)
          name = Privacy.normalize(parts.last || '')
          next unless Privacy::MIME['names'].include?(name) || (name == 'type' && Privacy.hint(key, value).any?)
          parts.pop
          node = roots
          parts.each { |part| node = (node[part] ||= {}) }
          node[nil] = parts.join('.')
        end
        return groups if roots.empty?
        parents = groups.keys.to_h { |group| [group, group] }
        find = lambda do |group|
          root = group
          root = parents[root] while parents[root] != root
          parents[group] = root
        end
        owners = {}
        groups.each do |group, values|
          values.each_key do |key|
            node = roots; context = node[nil]
            Privacy.path_parts(key).each do |part|
              break if context || !(node = node[part])
              context = node[nil]
            end
            next unless context
            if (owner = owners[context])
              left = find.call(group); right = find.call(owner)
              canonical = [left, right].min
              parents[left] = parents[right] = canonical
            else
              owners[context] = group
            end
          end
        end
        groups.each_with_object({}) do |(group, values), merged|
          (merged[find.call(group)] ||= {}).merge!(values)
        end
      end

      def select(protected, groups, optional, limit)
        selected = protected.sort_by { |key, _| [protected_family(key), key] }.first(limit).to_h
        return selected if protected.size > limit
        output_blocked = false; input_blocked = false
        groups.sort_by(&:first).each do |group, values|
          priority = group[0]
          next if priority >= 2 && output_blocked
          next if priority == 2 && input_blocked
          # An aggregate input must not restore an omitted older conversation.
          next if priority == 4 && input_blocked
          if selected.size + values.size <= limit
            values.sort_by(&:first).each { |key, value| selected[key] = value }
          else
            output_blocked = true if priority < 2
            input_blocked = true if priority == 2
          end
        end
        optional.sort_by(&:first).each do |key, value|
          break if selected.size >= limit
          selected[key] = value
        end
        selected
      end

      def project(attributes, limit)
        candidate_count = attributes.size
        had_count = attributes.key?(COUNT)
        attributes = Privacy.scrub_attributes(attributes)
        limit = [Integer(limit), 0].max
        if limit.zero?
          ZERO_MUTEX.synchronize { @zero_limit_spans = [@zero_limit_spans + 1, 0x7fffffffffffffff].min }
        end
        protected = {}; groups = {}; optional = {}; content = []
        previous = attributes[COUNT]
        previous = 0 unless previous.instance_of?(Integer) && previous.positive?
        attributes.each do |key, value|
          next if key == COUNT
          if protected_family(key)
            protected[key] = value
          elsif (group = content_group(key, attributes['gen_ai.operation.name']))
            (groups[group] ||= {})[key] = value
            content << key
          else
            optional[key] = value
          end
        end
        groups = coalesce_media(groups, attributes)
        protected[COUNT] = previous if previous.positive?
        selected = select(protected, groups, optional, limit)
        omitted = content.count { |key| !selected.key?(key) }
        if omitted.positive? && !protected.key?(COUNT)
          # Reserve the marker before final selection; it may displace a whole
          # content group. The final count is computed only after that decision.
          protected[COUNT] = 0
          selected = select(protected, groups, optional, limit)
          omitted = content.count { |key| !selected.key?(key) }
        end
        selected[COUNT] = previous + omitted if selected.key?(COUNT)
        inserted_count = selected.key?(COUNT) && !had_count ? 1 : 0
        Result.new(selected, [candidate_count - selected.size + inserted_count, 0].max)
      end
    end

    # SDK admission sees only the projected keys. Retain pre-admission loss in
    # detached SpanData accounting too, without touching SDK-private counters or
    # affecting spans owned by the application. Each to_span_data call is fresh.
    module ProjectedSpan
      def to_span_data
        super.tap { |record| record.total_recorded_attributes += @traceai_projection_dropped }
      end
    end
  end
end
