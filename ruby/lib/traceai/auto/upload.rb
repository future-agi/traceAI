# frozen_string_literal: true
module TraceAI
  module Auto
    # Parses bytes only as the application/transport sends them. Retains at most
    # 64 KiB per text field, an 8 KiB header and a boundary tail, never a file.
    class MultipartReader
      attr_reader :fields, :files, :complete
      def initialize(content_type)
        boundary = content_type[/boundary="?([^";]+)"?/i, 1]
        raise ArgumentError, 'invalid multipart boundary' unless boundary && boundary.bytesize <= 200
        @marker = "--#{boundary}".b; @delimiter = "\r\n#{@marker}".b
        @buffer = ''.b; @state = :boundary; @fields = {}; @files = []; @parts = 0; @complete = false
      end
      def feed(chunk)
        return unless chunk.is_a?(String)
        offset = 0
        while offset < chunk.bytesize && @state != :done
          part = chunk.byteslice(offset, 16_384); offset += part.bytesize
          @buffer << part.b
          process
        end
      rescue StandardError => e
        @state = :done; @buffer.clear; Auto.debug(e)
      end
      def process
        loop do
          case @state
          when :boundary
            return if @buffer.bytesize < @marker.bytesize + 2
            unless @buffer.start_with?(@marker)
              @state = :done; @buffer.clear; return
            end
            suffix = @buffer.byteslice(@marker.bytesize, 2)
            @buffer = @buffer.byteslice((@marker.bytesize + 2)..) || ''.b
            if suffix == '--'
              @complete = true; @state = :done; @buffer.clear; return
            end
            unless suffix == "\r\n"
              @state = :done; @buffer.clear; return
            end
            @state = :headers
          when :headers
            index = @buffer.index("\r\n\r\n")
            unless index
              if @buffer.bytesize > 8192
                @state = :done; @buffer.clear
              end
              return
            end
            @parts += 1
            if index > 8192 || @parts > 128
              @state = :done; @buffer.clear; return
            end
            headers = @buffer.byteslice(0, index)
            @name = Wire.utf8(headers[/(?:^|[;\s])name="([^"]*)"/i, 1], 256)
            @filename = Wire.utf8(headers[/filename="([^"]*)"/i, 1], 1024)
            @mime = Wire.utf8(headers[/content-type:\s*([^\r\n;]+)/i, 1], 256)
            @file = !@filename.nil? || Wire.binary?(@mime.to_s.downcase)
            @text = ''.b; @size = 0
            @buffer = @buffer.byteslice((index + 4)..) || ''.b; @state = :body
          when :body
            index = @buffer.index(@delimiter)
            if index
              consume(index)
              @buffer = @buffer.byteslice((index + 2)..) || ''.b
              if @name
                if @file
                  @files << {name: @name, filename: @filename, content_type: @mime, size: @size}
                else
                  @fields[@name] = @text.force_encoding('UTF-8').scrub
                end
              end
              @state = :boundary
            else
              available = @buffer.bytesize - @delimiter.bytesize - 2
              if available.positive?
                consume(available)
                @buffer = @buffer.byteslice(available..) || ''.b
              end
              return
            end
          else return
          end
        end
      end
      def consume(length)
        @size += length
        return if @file || !@name || @text.bytesize >= 65_536
        @text << @buffer.byteslice(0, [length, 65_536 - @text.bytesize].min)
      end
    end
    # IO.copy_stream reads native IOs without calling their Ruby #read methods.
    # Use a non-IO source at the copy site, without IO/path coercions (copy_stream
    # can also reopen #to_path from offset zero). Preserve the original stream's
    # position and observe native files only as the transport reads them.
    class UploadTee
      def initialize(stream, reader)
        @stream, @reader = stream, reader
      end
      def read(*args, **kwargs, &block)
        observe(@stream.read(*args, **kwargs, &block))
      end
      def respond_to_missing?(name, include_private = false)
        ![:to_io, :to_path, :to_str].include?(name) && (@stream.respond_to?(name, include_private) || super)
      end
      def method_missing(name, *args, **kwargs, &block)
        return super if [:to_io, :to_path, :to_str].include?(name) || !@stream.respond_to?(name)
        result = @stream.public_send(name, *args, **kwargs, &block)
        name == :readpartial ? observe(result) : result
      end
      private
      def observe(result)
        begin
          @reader.feed(result)
        rescue StandardError => e
          Auto.debug(e)
        end
        result
      end
    end
  end
end
