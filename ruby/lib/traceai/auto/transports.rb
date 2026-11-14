# frozen_string_literal: true
require_relative 'upload'
module TraceAI
  module Auto
    # One capture owns at most max_body_bytes; binaries own only a counter.
    class Capture
      MAX_ARRIVALS = 4096
      attr_reader :request, :response, :complete
      attr_accessor :upload_reader, :net_request, :native_arrival
      def initialize(method, url, headers, body, metadata = {}, started: Time.now, clock: Process.clock_gettime(Process::CLOCK_MONOTONIC))
        @started, @clock = started, clock
        @parent = Auto.current_context
        @limit = Auto.config['wire_max_body_bytes']
        reset_request!(method, url, headers, body, metadata)
      end
      # Redirects are part of one logical call. Use the final recognized route's
      # metadata without changing its original parent/start or emitting twice.
      def reset_request!(method, url, headers, body, metadata = {})
        # The session id of the request's own context (using_session): a body
        # value equal to it is not learned as a credential.
        @request = {method: method, url: url, headers: headers, session_id: Auto.session_id(@parent)}.merge(metadata)
        if body.is_a?(String)
          ctype = Wire.content_type(headers)
          if ctype.start_with?('multipart/')
            @request[:fields], @request[:files] = Wire.multipart(body, Wire.header(headers, 'content-type'))
          elsif Wire.binary?(ctype)
            @request[:binary] = true; @request[:body_size] = body.bytesize
          elsif body.bytesize > @limit
            @request[:truncated] = true
          else
            @request[:body] = body
          end
        elsif !@request[:fields] && !@request[:binary] && method != 'GET'
          @request[:unavailable] = true
        end
        Privacy.observe_body_credentials(@request[:fields], @request[:session_id]) if @request[:fields]
        reset_response!
      end
      # A retry is a new response, not another chunk of the previous attempt.
      # Request start/parent are retained; all attempt-specific state is dropped.
      def reset_response!
        @response = {headers: {}, size: 0, body: ''.b, arrivals: []}
        @native_arrival = nil
        @binary = false
        @complete = false
        @consumer_error = nil
        @framing_set = false
        @expected_bytes = nil
        @last_byte = @observed_end = nil
      end
      def consumer_abort(error)
        @consumer_error = error
        @complete = false
      end
      def consumer_error?(error) = @consumer_error.equal?(error)
      def headers(status, headers, decoded: false)
        @observed_end ||= Time.now
        @response[:status], @response[:headers], @response[:decoded] = status, headers, decoded
        unless @framing_set
          @framing_set = true
          length = Wire.header(headers, 'content-length')
          encoding = Wire.header(headers, 'content-encoding').downcase
          # Compare only like-for-like bytes. A decoder can rewrite Content-Length
          # while unwinding a partial read, and Transfer-Encoding overrides it.
          if Wire.header(headers, 'transfer-encoding').empty? && ['', 'identity'].include?(encoding) && length.match?(/\A\d+\z/)
            @expected_bytes = length.to_i
          end
        end
        @binary = Wire.binary?(Wire.content_type(headers))
        @response[:body] = nil if @binary
      rescue StandardError => e
        Auto.debug(e)
      end
      # Record raw-body arrival before a native decoder runs. Some decoders emit
      # nothing for an initial header; it still establishes first-chunk time.
      def transport_chunk(bytes, observed: Time.now, clock: Process.clock_gettime(Process::CLOCK_MONOTONIC))
        return if bytes.nil? || bytes.empty? || @finished
        @last_byte = @observed_end = observed
        @response[:first_chunk_seconds] ||= clock - @clock
      rescue StandardError => e
        Auto.debug(e)
      end
      def chunk(bytes, observed: Time.now, clock: Process.clock_gettime(Process::CLOCK_MONOTONIC), transport_observed: false)
        return if bytes.nil? || bytes.empty? || @finished
        observed, clock = @native_arrival if @native_arrival
        transport_chunk(bytes, observed: observed, clock: clock) unless transport_observed || @native_arrival
        @response[:size] += bytes.bytesize
        return if @binary || @response[:truncated]
        if @response[:body].bytesize + bytes.bytesize > @limit
          @response[:body] = nil; @response[:truncated] = true; @response[:incomplete] = true
          @response[:arrivals].clear
        else
          @response[:body] << bytes.b
          if @response[:arrivals].size < MAX_ARRIVALS
            @response[:arrivals] << [@response[:body].bytesize, clock - @clock]
          else
            @response[:incomplete] = true
          end
        end
      rescue StandardError => e
        Auto.debug(e)
      end
      def complete!
        @observed_end ||= @last_byte || Time.now
        @complete = true unless @consumer_error
      end
      def error(error)
        unless consumer_error?(error) || @response[:error]
          @observed_end = Time.now
          @response[:error] = error
        end
      end
      def finish
        return if @finished
        @finished = true
        Privacy.observe_body_credentials(@upload_reader.fields, @request[:session_id]) if @upload_reader
        if @upload_reader&.complete
          @request[:fields] = @upload_reader.fields; @request[:files] = @upload_reader.files; @request[:unavailable] = false
        end
        @complete ||= !@expected_bytes.nil? && @response[:size] == @expected_bytes
        @response[:incomplete] = true if @response[:status] && !@complete && !@response[:error]
        Auto.emit(@request, @response, @parent, @started, @observed_end || Time.now)
        @response[:body] = nil
        @response[:arrivals] = nil
        @request[:body] = nil
        @net_request = nil
      rescue StandardError => e
        Auto.debug(e)
      end
    end
    class Tee
      attr_reader :capture
      def initialize(destination, capture)
        @destination, @capture = destination, capture
      end
      def <<(chunk)
        @capture.chunk(chunk)
        begin
          @destination << chunk
        rescue Exception => e # Consumer exceptions retain their exact identity/backtrace.
          @capture.consumer_abort(e)
          raise
        end
      end
      def respond_to_missing?(name, include_private = false)
        @destination.respond_to?(name, include_private) || super
      end
      def method_missing(name, *args, **kwargs, &block)
        return super unless @destination.respond_to?(name)
        @destination.public_send(name, *args, **kwargs, &block)
      end
    end
    # Native Net::HTTP inflation is downstream of this adapter. Timestamp the
    # compressed bytes before passing the same object to the decoder; the decoded
    # Tee uses that arrival for all output from this chunk. No extra decode/read.
    class NetInflateArrival
      def initialize(destination, capture)
        @destination, @capture = destination, capture
      end
      def <<(bytes)
        observed = Time.now; clock = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        previous = @capture.native_arrival
        @capture.transport_chunk(bytes, observed: observed, clock: clock)
        @capture.native_arrival = [observed, clock]
        @destination << bytes
      ensure
        @capture.native_arrival = previous
      end
    end
    module NetInflater
      def inflate_adapter(destination)
        adapter = super
        destination.is_a?(Tee) ? NetInflateArrival.new(adapter, destination.capture) : adapter
      end
    end
    module NetResponse
      # Observe the native lifecycle, including buffered responses and retries,
      # without inventing a request block (which disables Net::HTTP retries).
      def reading_body(socket, permitted, &block)
        capture = Auto.current_capture
        return super unless capture.is_a?(Capture)
        begin
          capture.reset_response!
          capture.headers(code.to_i, each_header.to_h)
          @traceai_capture = capture
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      ensure
        if capture.is_a?(Capture)
          begin
            capture.headers(code.to_i, each_header.to_h)
            capture.complete! if @read
            remove_instance_variable(:@traceai_capture) if instance_variable_defined?(:@traceai_capture)
          rescue StandardError => e
            Auto.debug(e)
          end
        end
      end
      private
      # Wrap the writer, not #read_body: Net::HTTP keeps the original destination
      # as @body, preserving return values, cached body, chunk objects and blocks.
      def read_body_0(destination)
        capture = instance_variable_get(:@traceai_capture)
        return super unless capture
        result = super(Tee.new(destination, capture))
        capture.complete!
        result
      rescue StandardError => e
        capture.error(e) if capture
        raise
      end
    end
    module NetRequestBody
      private
      def send_request_with_body_stream(socket, version, path, stream)
        reader = instance_variable_get(:@traceai_upload_reader)
        source = reader ? UploadTee.new(stream, reader) : stream
        super(socket, version, path, source)
      end
    end
    # CONNECT is serialized inside Net::HTTP#connect, not on the application
    # request. Observe its actual header block at the native write boundary;
    # never reconstruct authentication by calling proxy credential getters again.
    module NetConnectHeaders
      def write(*strings)
        begin
          if Thread.current[:traceai_net_connect]
            strings.each do |data|
              next unless Codec.type(data) == String && data.start_with?('CONNECT ')
              headers = {}
              data.each_line("\r\n").with_index do |line, index|
                next if index.zero?
                break if line == "\r\n"
                name, value = line.split(':', 2)
                headers[name] = value.strip if value
              end
              Privacy.observe_request(headers, '')
            end
          end
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      end
    end
    module NetHTTP
      def connect(...)
        previous = Thread.current[:traceai_net_connect]
        Thread.current[:traceai_net_connect] = Auto.active?
        super
      ensure
        Thread.current[:traceai_net_connect] = previous
      end
      private :connect
      # A response block may itself make another HTTP call. Its response must
      # not overwrite the outer capture. Native request recursion, in contrast,
      # uses the same request object and must retain the capture and block shape.
      def transport_request(req, &block)
        begin
          # Native #request adds proxy authentication after our entry hook.
          # Observe the finalized headers even with wire capture suppressed;
          # never consume bodies or change Net::HTTP's retries/block shape.
          Privacy.observe_request(req.each_header.to_h, req.path) if Auto.active?
          capture = Auto.current_capture
          token = Auto.attach_context(Auto.capture_context) if capture.is_a?(Capture) && !capture.net_request.equal?(req)
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      ensure
        Auto.detach_context(token)
      end
      private :transport_request
      def request(req, body = nil, &block)
        started, clock = Thread.current[:traceai_lazy_timing] || [Time.now, Process.clock_gettime(Process::CLOCK_MONOTONIC)]
        Thread.current[:traceai_lazy_timing] = nil
        capture = nil; token = nil
        begin
          Privacy.observe_request(req.each_header.to_h, req.path) if Auto.active?
          if Auto.capture_allowed? && Auto.transport_enabled?('net_http')
            scheme = use_ssl? ? 'https' : 'http'
            host = address.include?(':') ? "[#{address}]" : address
            url = req.path.start_with?('http://', 'https://') ? req.path : "#{scheme}://#{host}:#{port}#{req.path}"
            if Wire.match(req.method, url)
              metadata = {}
              # Net::HTTP#set_form stores fields separately until send time. Read only
              # scalar fields; files/IO are never read, rewound, or stringified.
              form = req.instance_variable_get(:@body_data)
              if form.is_a?(Array) && Wire.content_type(req.to_hash.transform_values { |v| v.join(', ') }).start_with?('multipart/')
                metadata[:fields] = {}; metadata[:files] = []
                form.first(128).each do |name, value, opts|
                  opts ||= {}
                  if value.is_a?(String) && !opts[:filename]
                    metadata[:fields][Wire.utf8(name.to_s, 256)] = Wire.utf8(value, 65_536)
                  else
                    metadata[:files] << {name: name.to_s, filename: opts[:filename] || (value.respond_to?(:path) ? File.basename(value.path.to_s) : nil), content_type: opts[:content_type] || 'application/octet-stream', size: value.is_a?(File) ? value.stat.size - value.pos : nil}
                  end
                end
              end
              # Official OpenAI/Anthropic wrap already-serialized strings in a
              # ReadIOAdapter backed by StringIO. Snapshot only that in-memory
              # storage (native accessors), never read/rewind an IO or enumerator.
              payload = body || req.body
              unless payload
                stream = req.body_stream
                if %w[OpenAI::Internal::Util::ReadIOAdapter Anthropic::Internal::Util::ReadIOAdapter].include?(stream.class.name)
                  stream = stream.instance_variable_get(:@stream)
                end
                if defined?(StringIO) && stream.instance_of?(StringIO)
                  storage = StringIO.instance_method(:string).bind_call(stream)
                  position = StringIO.instance_method(:pos).bind_call(stream)
                  if storage.bytesize - position > Auto.config['wire_max_body_bytes'] && !Wire.content_type(req.each_header.to_h).start_with?('multipart/')
                    metadata[:truncated] = true
                  else
                    payload = position.zero? ? storage : storage.byteslice(position..)
                  end
                end
              end
              capture = Capture.new(req.method, url, req.each_header.to_h, payload, metadata, started: started, clock: clock)
              capture.net_request = req
              if !payload && !metadata[:fields] && Wire.content_type(req.each_header.to_h).start_with?('multipart/') && req.body_stream
                capture.upload_reader = MultipartReader.new(Wire.header(req.each_header.to_h, 'content-type'))
                req.instance_variable_set(:@traceai_upload_reader, capture.upload_reader)
              end
              token = Auto.attach_context(Auto.capture_context(capture))
            end
          end
        rescue StandardError => e
          Auto.debug(e)
        end
        return super unless capture
        begin
          if block
            super(req, body) do |res|
              begin
                block.call(res)
              rescue Exception => e
                # read_body_0 has already identified socket/parser failures.
                capture.consumer_abort(e) unless capture.response[:error].equal?(e)
                raise
              end
            end
          else
            super(req, body, &nil)
          end
        rescue StandardError => e
          capture.error(e) if e.is_a?(IOError) || e.is_a?(SystemCallError) || e.is_a?(Timeout::Error) || e.is_a?(SocketError) || Auto.named_instance?(e, 'OpenSSL::SSL::SSLError') || e.is_a?(Net::HTTPBadResponse) || e.is_a?(Net::ProtocolError)
          raise
        ensure
          begin
            capture.finish
            req.remove_instance_variable(:@traceai_upload_reader) if req.instance_variable_defined?(:@traceai_upload_reader)
          rescue StandardError => e
            Auto.debug(e)
          ensure
            Auto.detach_context(token)
          end
        end
      end
    end
    # The official SDK creates an Enumerator Fiber in #execute. Bridge only that
    # request's context, never all Fibers or a shared requester instance. Net::HTTP
    # attaches it inside the Fiber before deciding suppression or taking a parent;
    # the attachment lives there until the request finishes, even across yields.
    module SDKRequestContext
      def execute(...)
        previous = Thread.current.thread_variable_get(:traceai_sdk_context)
        begin
          Thread.current.thread_variable_set(:traceai_sdk_context, [Fiber.current, OpenTelemetry::Context.current])
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      ensure
        Thread.current.thread_variable_set(:traceai_sdk_context, previous)
      end
    end
    module SDKTransportContext
      def request(...)
        begin
          bridge = Thread.current.thread_variable_get(:traceai_sdk_context)
          # Claim only the SDK's first transport Fiber. Restoring again in a
          # request-body callback (possibly in another Enumerator Fiber) would
          # erase its suppression, active capture/span and context attributes.
          if bridge && !bridge[0].equal?(Fiber.current) && !bridge[2]
            bridge[2] = Fiber.current
            token = OpenTelemetry::Context.attach(bridge[1]) if OpenTelemetry::Context.current.equal?(OpenTelemetry::Context::ROOT)
          end
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      ensure
        Auto.detach_context(token)
      end
    end
    module HTTPXConsumer
      def on_chunk(...)
        super
      rescue Exception => e
        @traceai_capture&.consumer_abort(e)
        raise
      end
    end
    # Header access (status/headers/etc.) starts HTTPX's #each Enumerator in a
    # private Fiber. Save the caller context before that Fiber is created and
    # restore it in #each, rather than propagating contexts into arbitrary Fibers.
    module HTTPXStreamContext
      def each(&block)
        return super unless block
        context = @traceai_context || Auto.current_context
        Auto.with_context(context) do
          begin
            super do |chunk|
              begin
                block.call(chunk)
              rescue Exception => e
                @traceai_capture&.consumer_abort(e)
                raise
              end
            end
          ensure
            # Header-first iteration yields in the caller, outside send_requests.
            # A consumer break/raise must finalize even if its Fiber is suspended.
            @traceai_capture&.finish
          end
        end
      end
      private
      def response(...)
        @traceai_context ||= Auto.current_context
        super
      end
    end
    # The proxy plugin adds authentication in its protocol serializer, including
    # CONNECT requests that never pass through Session#send_request. Observe the
    # final header enumeration, after all plugin additions, without re-enumerating,
    # mutating native headers, reading bodies or depending on wire-span eligibility.
    module HTTPXProtocolHeaders
      private
      def join_headers2(request, headers)
        return super unless Auto.active?
        observed = Enumerator.new do |output|
          headers.each do |name, value|
            begin
              Privacy.observe_request({name => value}, '')
            rescue StandardError => e
              Auto.debug(e)
            end
            output.yield(name, value)
          end
        end
        super(request, observed)
      end
    end
    HTTPXBatch = Struct.new(:enabled, :captures, :registrations, :token)
    HTTPXRegistration = Struct.new(:request, :started, :response, :idle, :body, :stream)
    module HTTPXSession
      private
      # The batch owns cleanup; send_request observes actual outbound requests,
      # including ones created below send_requests by follow_redirects. State is
      # Fiber-local, never shared on a Session used by concurrent callers.
      def send_requests(*requests)
        previous = Thread.current[:traceai_httpx_batch]
        batch = HTTPXBatch.new(nil, {}.compare_by_identity, {}.compare_by_identity, nil)
        Thread.current[:traceai_httpx_batch] = batch
        begin
          super
        rescue StandardError => e
          batch.captures.each_value { |capture| capture.error(e) }
          raise
        ensure
          batch.captures.each_value(&:finish)
          batch.registrations.each_value do |registration|
            begin
              req = registration.request
              req.send(:callbacks, :idle).delete(registration.idle)
              req.send(:callbacks, :response_started).delete(registration.started)
              req.send(:callbacks, :response).delete(registration.response)
              clear_traceai_httpx_body(registration.body)
              stream = registration.stream
              stream.remove_instance_variable(:@traceai_capture) if stream&.instance_variable_defined?(:@traceai_capture)
            rescue StandardError => e
              Auto.debug(e)
            end
          end
          Auto.detach_context(batch.token)
          Thread.current[:traceai_httpx_batch] = previous
        end
      end
      def send_request(req, ...)
        started = Time.now; clock = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        begin
          Privacy.observe_request(req.headers.to_h, req.uri.to_s) if Auto.active?
          batch = Thread.current[:traceai_httpx_batch]
          batch.enabled = Auto.capture_allowed? && Auto.transport_enabled?('httpx') if batch && batch.enabled.nil?
          if batch&.enabled
            root = (req.root_request if req.respond_to?(:root_request)) || req
            capture = batch.captures[root]
            unless batch.registrations.key?(req)
              if Wire.match(req.verb.to_s, req.uri.to_s)
                raw = req.body.instance_variable_get(:@body)
                body = if Auto.named_instance?(raw, 'HTTPX::Transcoder::JSON::Encoder')
                         raw.instance_variable_get(:@raw)
                       elsif Auto.named_instance?(raw, 'HTTPX::Transcoder::Body::Encoder') && raw.instance_variable_get(:@body).is_a?(String)
                         raw.instance_variable_get(:@body)
                       end
                metadata = HTTPXMultipart.metadata(raw)
                if capture
                  capture.reset_request!(req.verb.to_s.upcase, req.uri.to_s, req.headers.to_h, body, metadata)
                else
                  capture = Capture.new(req.verb.to_s.upcase, req.uri.to_s, req.headers.to_h, body, metadata, started: started, clock: clock)
                  batch.captures[root] = capture
                end
              elsif capture
                capture.reset_response!
              end
              if capture
                register_traceai_httpx(req, root, capture, batch)
                batch.token ||= Auto.attach_context(Auto.capture_context)
              end
            end
          end
        rescue StandardError => e
          Auto.debug(e)
        end
        super
      end
      def clear_traceai_httpx_body(body)
        return unless body
        body.remove_instance_variable(:@traceai_capture) if body.instance_variable_defined?(:@traceai_capture)
        body.remove_instance_variable(:@traceai_stream) if body.instance_variable_defined?(:@traceai_stream)
      end
      def register_traceai_httpx(req, root, capture, batch)
        registration = HTTPXRegistration.new(req)
        batch.registrations[req] = registration
        # Retries reuse their request and reset only attempt-specific state.
        # ErrorResponse may be a mid-stream error, not the start of a retry.
        registration.idle = req.on(:idle) { capture.reset_response! }
        registration.started = req.on(:response_started) do |res|
          if Auto.named_instance?(res, 'HTTPX::ErrorResponse')
            capture.error(res.error)
          else
            capture.headers(res.status, res.headers.to_h, decoded: req.options.decompress_response_body && res.body.encodings.length == Wire.header(res.headers.to_h, 'content-encoding').split(',').length)
            clear_traceai_httpx_body(registration.body)
            registration.body = res.body
            # DecodeChunk observes both buffered and stream-plugin payloads once.
            res.body.instance_variable_set(:@traceai_capture, capture)
            res.body.singleton_class.prepend(HTTPXDecodeTee)
            stream = (req.stream if req.respond_to?(:stream)) || (root.stream if root.respond_to?(:stream))
            if stream
              registration.stream = stream
              stream.instance_variable_set(:@traceai_capture, capture)
              res.body.instance_variable_set(:@traceai_stream, stream)
              stream.singleton_class.prepend(HTTPXConsumer) unless stream.singleton_class.ancestors.include?(HTTPXConsumer)
            end
          end
        rescue StandardError => e
          Auto.debug(e)
        end
        registration.response = req.on(:response) do |res|
          Auto.named_instance?(res, 'HTTPX::ErrorResponse') ? capture.error(res.error) : capture.complete!
        rescue StandardError => e
          Auto.debug(e)
        end
      end
    end
    module HTTPXMultipart
      module_function
      def metadata(encoder)
        return {} unless encoder.class.name == 'HTTPX::Transcoder::Multipart::Encoder'
        fields = {}; files = []
        Array(encoder.instance_variable_get(:@parts)).each_slice(3).take(128).each do |head, value, _delimiter|
          next unless head.instance_of?(StringIO)
          header = StringIO.instance_method(:string).bind_call(head).byteslice(0, 8192)
          name = header[/(?:^|[;\s])name="([^"]*)"/i, 1]
          next unless name
          filename = header[/filename="([^"]*)"/i, 1]
          part_type = header[/content-type:\s*([^\r\n;]+)/i, 1]
          if filename || Wire.binary?(part_type.to_s.downcase)
            size = if value.instance_of?(StringIO)
                     StringIO.instance_method(:size).bind_call(value) - StringIO.instance_method(:pos).bind_call(value)
                   elsif value.instance_of?(File)
                     value.stat.size - value.pos
                   end
            files << {name: Wire.utf8(name, 256), filename: Wire.utf8(filename, 1024), content_type: Wire.utf8(part_type, 256), size: size}
          elsif value.instance_of?(StringIO)
            fields[Wire.utf8(name, 256)] = Wire.utf8(StringIO.instance_method(:string).bind_call(value), 65_536)
          end
        end
        {fields: fields, files: files}
      rescue StandardError => e
        Auto.debug(e); {}
      end
    end
    module HTTPXDecodeTee
      # Header-first streams can leave send_requests suspended forever. Observe
      # the native close without resuming it, but not HTTPX's internal cleanup
      # between retry attempts (which must keep the capture open).
      def close(...)
        super
      ensure
        stream = instance_variable_get(:@traceai_stream)
        instance_variable_get(:@traceai_capture)&.finish if stream&.instance_variable_get(:@response_enum)
      end
      private
      def decode_chunk(chunk)
        observed = Time.now; clock = Process.clock_gettime(Process::CLOCK_MONOTONIC)
        capture = instance_variable_get(:@traceai_capture)
        capture&.transport_chunk(chunk, observed: observed, clock: clock)
        result = super
        capture&.chunk(result, observed: observed, clock: clock, transport_observed: true)
        result
      end
    end
  end
end
