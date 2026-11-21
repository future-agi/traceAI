# frozen_string_literal: true
require_relative 'privacy'
require_relative 'key_transport'
require_relative 'proxy_tunnel'
module TraceAI
  module Auto
    # Internal serialization records, shared by SDK forwarding and dependency-free
    # standalone capture. The only entry to the serializer is Privacy.snapshot.
    ExportScope = Struct.new(:name, :version, :attributes)
    ExportResource = Struct.new(:attributes) do
      def attribute_enumerator = attributes.each
    end
    ExportRecord = Struct.new(:name, :kind, :status, :parent_span_id, :total_recorded_attributes,
                              :total_recorded_events, :total_recorded_links, :start_timestamp,
                              :end_timestamp, :attributes, :links, :events, :resource,
                              :instrumentation_scope, :span_id, :trace_id, :trace_flags,
                              :tracestate, :parent_span_is_remote)
    # Minimal OTLP protobuf writer; no protobuf/json/default-gem activation. Only
    # sanitized internal records enter here. Unknown attribute values never reach it.
    module Protobuf
      module_function
      def varint(number)
        number &= 0xffffffffffffffff; bytes = +''.b
        while number > 127
          bytes << ((number & 127) | 128); number >>= 7
        end
        bytes << number
      end
      def bytes(tag, value) = varint((tag << 3) | 2) + varint(value.bytesize) + value.b
      def integer(tag, value) = varint(tag << 3) + varint(value)
      def fixed(tag, value) = varint((tag << 3) | 1) + [value].pack('Q<')
      def flags(tag, value) = varint((tag << 3) | 5) + [value].pack('L<')
      def any(value)
        case value
        when String then bytes(1, value)
        when true, false then integer(2, value ? 1 : 0)
        when Integer then integer(3, value)
        when Float then varint((4 << 3) | 1) + [value].pack('E')
        when Array then bytes(5, value.map { |v| bytes(1, any(v)) }.join)
        when Hash then bytes(6, attributes(value, 1))
        else bytes(1, 'NilClass')
        end
      end
      def attributes(values, tag)
        values.map { |key, value| bytes(tag, bytes(1, key) + bytes(2, any(value))) }.join
      end
      def span(record)
        data = bytes(1, record.trace_id) + bytes(2, record.span_id) + bytes(3, record.tracestate) + bytes(4, record.parent_span_id)
        data << bytes(5, record.name) << integer(6, {internal: 1, server: 2, client: 3, producer: 4, consumer: 5}.fetch(record.kind, 0))
        data << fixed(7, record.start_timestamp) << fixed(8, record.end_timestamp) << attributes(record.attributes, 9)
        data << integer(10, [record.total_recorded_attributes - record.attributes.size, 0].max)
        data << integer(12, [record.total_recorded_events - record.events.size, 0].max)
        data << integer(14, [record.total_recorded_links - record.links.size, 0].max)
        record.events.each do |event|
          data << bytes(11, fixed(1, event.timestamp) + bytes(2, event.name) + attributes(event.attributes, 3))
        end
        record.links.each do |link|
          ctx = link.span_context
          data << bytes(13, bytes(1, ctx.trace_id) + bytes(2, ctx.span_id) + bytes(3, ctx.tracestate) + attributes(link.attributes, 4))
        end
        status = record.status
        data << bytes(15, bytes(2, status.description) + integer(3, status.code))
        data << flags(16, record.trace_flags | 256 | (record.parent_span_is_remote ? 512 : 0))
        data
      end
      def encode(records)
        records.map do |record|
          scope = record.instrumentation_scope
          info = bytes(1, scope.name) + bytes(2, scope.version) + attributes(scope.attributes, 3)
          bytes(1, bytes(1, attributes(record.resource.attributes, 1)) + bytes(2, bytes(1, info) + bytes(2, span(record))))
        end.join
      end
    end
    class WireExporter
      def initialize
        @endpoint = Auto.config['base_url'].to_s.sub(%r{/+$}, '') + '/tracer/v1/traces'
        # Fixed with the endpoint: a later change of the environment does not change the decision.
        @insecure_opt_in = ENV['FI_ALLOW_INSECURE_HTTP']
        # The proxy settings are read the same way: once, for the one target this exporter sends to.
        @route = ProxyTunnel.route(@endpoint, ENV)
      end
      def export(records, timeout: nil)
        return 1 if @shutdown
        # The only gate for FI keys: decided on the written target before any key header exists, and before the
        # target is parsed for anything else, so a URL that cannot be parsed is refused with the same warning.
        KeyTransport.check(@endpoint, opt_in: @insecure_opt_in)
        url = Wire.parse_url(@endpoint)
        return 1 unless url && !url.path.match?(/[\r\n\0 ]/)
        socket_class = native_class('Socket') || load_socket
        return 1 unless socket_class
        # Use the application's already-loaded socket/TLS implementation. Even
        # requiring net/http here would select default gems in HTTPX-only apps.
        # An application that never loaded OpenSSL (one that only calls a local
        # http model server) gets it loaded here when no version can conflict.
        load_tls if url.scheme == 'https' && !native_class('OpenSSL::SSL::SSLSocket')
        tls_context = native_class('OpenSSL::SSL::SSLContext') if url.scheme == 'https'
        tls_socket = native_class('OpenSSL::SSL::SSLSocket') if tls_context
        return 1 if url.scheme == 'https' && !tls_socket
        timeout = [timeout || 4, 4].min.clamp(0.001, 4)
        deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + timeout
        payload = Protobuf.encode(records)
        host = url.host.include?(':') ? "[#{url.host}]" : url.host
        headers = {'Host' => "#{host}:#{url.port}", 'Connection' => 'close', 'Content-Type' => 'application/x-protobuf',
                   'Content-Length' => payload.bytesize.to_s, 'X-Api-Key' => Auto.config['api_key'], 'X-Secret-Key' => Auto.config['secret_key']}
        return 1 unless headers.values.all? { |value| Codec.type(value) == String && !value.match?(/[\r\n\0]/) }
        if @route.is_a?(ProxyTunnel::Unsupported)
          raise KeyTransport::Refused.new('proxy setting is not supported', reason: @route.reason)
        elsif @route.is_a?(ProxyTunnel::Config)
          # The proxy is the only peer dialled; a refusal by it is a refusal of the send, never a direct send.
          socket = socket_class.tcp(@route.host, @route.port, connect_timeout: timeout)
          tunnel = ProxyTunnel.open(@route, url.host, url.port, socket, tls_class: tls_socket, context_class: tls_context,
                                    unix_class: native_class('UNIXSocket')) { |waiting, state| wait_for(waiting, state, deadline) }
          io = tunnel.io
        else
          socket = socket_class.tcp(url.host, url.port, connect_timeout: timeout)
          unless KeyTransport.peer_agrees?(KeyTransport.written_host(@endpoint), socket)
            socket.close
            raise KeyTransport::Refused.new('connected peer is not loopback', reason: 'peer not loopback')
          end
          io = socket
        end
        if tls_socket
          context = tls_context.new
          context.set_params
          io = tls_socket.new(io, context); io.sync_close = true; io.hostname = url.host
          loop do
            state = io.connect_nonblock(exception: false)
            break unless %i[wait_readable wait_writable].include?(state)
            wait_for(io, state, deadline)
          end
          io.post_connection_check(url.host)
        end
        request = "POST #{url.path} HTTP/1.1\r\n" + headers.map { |key, value| "#{key}: #{value}\r\n" }.join + "\r\n"
        [request.b, payload].each do |part|
          offset = 0
          while offset < part.bytesize
            count = io.write_nonblock(part.byteslice(offset..), exception: false)
            if count.is_a?(Integer)
              offset += count
            else
              wait_for(io, count, deadline)
            end
          end
        end
        response = +''.b
        until response.include?("\r\n")
          raise IOError if response.bytesize > 8192
          part = io.read_nonblock(1024, exception: false)
          if part.is_a?(String)
            response << part
          elsif part.nil?
            raise EOFError
          else
            wait_for(io, part, deadline)
          end
        end
        raise KeyTransport::Refused.new('redirect answered a key-carrying send', reason: 'redirect') if KeyTransport.refuses_redirect?(response)
        response.match?(/\AHTTP\/1\.[01] 2\d\d(?: |\r)/) ? 0 : 1
      rescue KeyTransport::Refused => e
        KeyTransport.warn_refused(e); Auto.debug(e); 1
      rescue StandardError => e
        Auto.debug(e); 1
      ensure
        io&.close
        tunnel&.close
        socket&.close unless socket&.closed?
      end
      def load_socket
        # Ruby 3.2's socket is an unversioned standard library, not a default
        # gem. Newer Rubies may package it (and older ones need io-wait): require
        # those versions only after the application has selected them.
        return nil unless IO.method_defined?(:wait_writable, false) || Gem.loaded_specs.key?('io-wait')
        if Gem.loaded_specs.key?('socket')
          require 'socket'
        elsif Gem::Specification.find_all_by_name('socket').empty?
          path = File.join(RbConfig::CONFIG['rubylibdir'], 'socket.rb')
          require path if File.file?(path)
        end
        native_class('Socket')
      end
      # Load OpenSSL only where no gem version can conflict: the application or
      # its bundle already selected it, or only one version is installed.
      def load_tls
        return if @tls_attempted
        @tls_attempted = true
        selected = Gem.loaded_specs.key?('openssl') || Auto.bundle_ready? || Gem::Specification.find_all_by_name('openssl').size <= 1
        require 'openssl' if selected
      rescue LoadError, StandardError => e
        Auto.debug(e)
      end
      # Walking every class holds the GVL, so a class found once is remembered;
      # a missing one is looked up again on the next export.
      def native_class(name)
        (@native_classes ||= {})[name] ||= ObjectSpace.each_object(Class).find { |klass| Module.instance_method(:name).bind_call(klass) == name }
      end
      def wait_for(io, state, deadline)
        remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
        raise IOError if remaining <= 0
        readable = state == :wait_readable ? [io] : nil
        writable = state == :wait_writable ? [io] : nil
        raise IOError unless IO.select(readable, writable, nil, remaining)
      end
      def force_flush(timeout: nil) = 0
      def shutdown(timeout: nil)
        @shutdown = true; 0
      end
    end
    LocalStatus = Struct.new(:code, :description)
    LocalContext = Struct.new(:values) do
      def value(key) = values[key]
      def set_value(key, value) = self.class.new(values.merge(key => value))
    end
    class LocalProvider
      attr_reader :resource
      def initialize(resource) = @resource = resource
    end
    # A bounded standalone queue uses only core Ruby. It never selects any gem
    # versions. Once the app has selected OTel's SDK, the regular processor is used.
    class LocalProcessor
      def initialize(exporter)
        @exporter = exporter
        start_worker
      end
      def start_worker
        @pid = Process.pid; @queue = []; @inflight = 0; @last_result = 0; @shutdown = false
        @mutex = Mutex.new; @condition = ConditionVariable.new
        @worker = Thread.new do
          loop do
            records = @mutex.synchronize do
              @condition.wait(@mutex, 5) if @queue.length < 64 && !@shutdown
              batch = @queue.shift(64)
              @inflight += 1 unless batch.empty?
              batch
            end
            export_batch(records, 4) unless records.empty?
            break if @shutdown && @mutex.synchronize { @queue.empty? }
          rescue StandardError => e
            Auto.debug(e)
          end
        end
        @worker.report_on_exception = false
      end
      def enqueue(record)
        start_worker if @pid != Process.pid
        @mutex.synchronize do
          @queue << record if @queue.length < 512 && !@shutdown
          @condition.signal if @queue.length >= 64
        end
      end
      def export_batch(records, timeout)
        result = @exporter.export(records, timeout: timeout)
      ensure
        @mutex.synchronize do
          @last_result = result || 1; @inflight -= 1
          @condition.broadcast
        end
      end
      def force_flush(timeout: nil)
        start_worker if @pid != Process.pid
        deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + (timeout || 5)
        loop do
          records = @mutex.synchronize do
            batch = @queue.shift(64)
            @inflight += 1 unless batch.empty?
            batch
          end
          break if records.empty?
          remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
          export_batch(records, [remaining, 0.001].max)
        end
        @mutex.synchronize do
          until @inflight.zero?
            remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
            return 1 if remaining <= 0
            @condition.wait(@mutex, remaining)
          end
          @last_result
        end
      end
      def shutdown(timeout: nil)
        start_worker if @pid != Process.pid
        @mutex.synchronize { @shutdown = true; @condition.signal }
        result = force_flush(timeout: timeout)
        @worker.join(timeout || 5)
        result
      end
    end
  end
end
