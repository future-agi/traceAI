# frozen_string_literal: true
module TraceAI
  module Auto
    module Wire
      # Work on byte offsets, decoding only complete frames. The closing byte (or
      # SSE/NDJSON delimiter) ties each frame to its transport arrival, not parse
      # time. Partial UTF-8 in a later frame cannot corrupt an earlier one.
      module Frames
        module_function
        def parse(data, kind)
          bytes = data.b
          frames = []
          case kind
          when :sse
            offset = 0
            while (delimiter = /\r\n\r\n|\n\n|\r\r/.match(bytes, offset))
              block = bytes.byteslice(offset, delimiter.begin(0) - offset)
              offset = delimiter.end(0)
              lines = block.split(/\r\n|\n|\r/)
              payload = lines.filter_map { |line| line.sub(/\Adata: ?/, '') if line.start_with?('data:') }.join("\n")
              event = Wire.json(payload.dup.force_encoding('UTF-8'))
              next unless event.is_a?(Hash)
              name = lines.find { |line| line.start_with?('event:') }&.sub(/\Aevent: ?/, '')
              event['type'] ||= name if name && !event.key?('event_type')
              frames << [event, offset]
            end
          when :ndjson
            offset = 0
            while (newline = bytes.index("\n", offset))
              event = Wire.json(bytes.byteslice(offset, newline - offset).force_encoding('UTF-8'))
              offset = newline + 1
              frames << [event, offset] if event.is_a?(Hash)
            end
          when :json
            # Recognize complete top-level objects / array members without
            # requiring the array's closing bracket. Strings and escape sequences
            # are tracked at byte level; brackets in strings are not structural.
            depth = 0; quoted = false; escaped = false; start = nil; array = nil
            bytes.each_byte.with_index do |byte, index|
              if quoted
                if escaped then escaped = false
                elsif byte == 92 then escaped = true
                elsif byte == 34 then quoted = false
                end
                next
              end
              if array.nil?
                next if [9, 10, 13, 32].include?(byte)
                break unless [91, 123].include?(byte)
                array = byte == 91
              end
              case byte
              when 34 then quoted = true
              when 91, 123
                start = index if start.nil? && (array ? depth == 1 : depth.zero?)
                depth += 1
              when 93, 125
                depth -= 1
                if start && depth == (array ? 1 : 0)
                  event = Wire.json(bytes.byteslice(start, index - start + 1).force_encoding('UTF-8'))
                  frames << [event, index + 1] if event.is_a?(Hash)
                  start = nil
                end
                break if depth.zero?
              end
            end
          end
          frames
        end
      end
    end
  end
end
