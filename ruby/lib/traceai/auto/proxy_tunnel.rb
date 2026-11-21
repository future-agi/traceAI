# frozen_string_literal: true
require_relative 'key_transport'
module TraceAI
  module Auto
    # Exporting through an HTTPS proxy with CONNECT.
    #
    # * Only an https target is ever proxied; a plaintext key-carrying send, loopback included, goes direct.
    # * The settings are read once, when the exporter starts, and decided for the one target the exporter was built for.
    # * Proxy credentials come only from the proxy URL's userinfo and go only to the proxy.
    # * The proxy URL is never printed; every refusal names a reason from a short fixed list.
    # * A refused proxy is a refused send. It never turns into a direct one.
    module ProxyTunnel
      # `authorization` is the complete Proxy-Authorization value, or nil.
      Config = Struct.new(:scheme, :host, :port, :authorization)
      # A proxy setting that cannot be used. It carries no part of the setting.
      Unsupported = Struct.new(:reason)
      DIRECT = :direct
      LOOPBACK = KeyTransport::LOOPBACK
      PROXY_URL = %r{\A(https?)://(?:([^:@/?#\s]*)(?::([^@/?#\s]*))?@)?(\[[0-9a-fA-F:.]+\]|[^\s/:?#@\[\]]+)(?::(\d{1,5}))?/?\z}i

      class << self
        # Decides the route for `target` (a written https or http URL) from the environment given. Returns DIRECT, a
        # Config or an Unsupported.
        def route(target, env)
          route_for(target, env)
        rescue StandardError
          # Whatever is wrong with a setting (bytes that are not text, for one), the exporter still starts and the
          # send is refused with the one value-free warning.
          Unsupported.new('proxy unsupported')
        end

        def route_for(target, env)
          scheme = target.to_s[/\A(https?):\/\//i, 1].to_s.downcase
          return DIRECT unless scheme == 'https'
          written = KeyTransport.written_host(target.to_s) or return DIRECT
          port = target.to_s[%r{\Ahttps?://[^/?#]*?:(\d+)(?:[/?#]|\z)}i, 1]
          return DIRECT if loopback?(written)
          value = setting(env, 'https_proxy')
          return DIRECT if value.nil? || value.empty?
          return DIRECT if no_proxy?(setting(env, 'no_proxy').to_s, written, port ? Integer(port, 10) : 443)
          parse(value)
        end

        def parse(value)
          found = PROXY_URL.match(value) or return Unsupported.new('proxy unsupported')
          scheme, user, password, host, port = found.captures
          scheme = scheme.downcase
          port = port ? Integer(port, 10) : (scheme == 'https' ? 443 : 80)
          return Unsupported.new('proxy unsupported') unless (1..65_535).cover?(port)
          authorization = nil
          unless user.nil?
            credentials = "#{decode(user)}:#{decode(password.to_s)}"
            return Unsupported.new('proxy unsupported') if credentials.match?(/[\r\n\0]/)
            authorization = "Basic #{[credentials].pack('m0')}"
          end
          Config.new(scheme, host.delete_prefix('[').delete_suffix(']'), port, authorization)
        rescue ArgumentError
          Unsupported.new('proxy unsupported')
        end

        def open(config, host, port, socket, tls_class:, context_class:, unix_class:, deadline:, &wait)
          raise KeyTransport::Refused.new('proxy unusable', reason: 'proxy refused') unless host.to_s.match?(/\A[^\s\r\n\0]+\z/)
          outer = socket
          if config.scheme == 'https'
            outer = handshake(socket, config.host, tls_class, context_class, &wait)
          end
          begin
            connect(config, host, port, outer, socket, unix_class, deadline, &wait)
          rescue Exception # rubocop:disable Lint/RescueException
            # A refused or failed CONNECT closes the proxy TLS session; the raw socket is closed by the caller.
            outer.close if outer.respond_to?(:sync_close=) && !outer.closed?
            raise
          end
        end

        def connect(config, host, port, outer, socket, unix_class, deadline, &wait)
          label = host.include?(':') ? "[#{host}]:#{port}" : "#{host}:#{port}"
          lines = ["CONNECT #{label} HTTP/1.1", "Host: #{label}"]
          lines << "Proxy-Authorization: #{config.authorization}" if config.authorization
          write_all(outer, (lines.join("\r\n") + "\r\n\r\n").b, &wait)
          status = read_head(outer, &wait)
          raise KeyTransport::Refused.new('redirect answered a key-carrying send', reason: 'redirect') if KeyTransport.refuses_redirect?(status)
          raise KeyTransport::Refused.new('proxy refused the tunnel', reason: 'proxy refused') unless status.match?(/\AHTTP\/1\.[01] 2\d\d(?: |\r)/)
          return Tunnel.new(outer, nil, nil, deadline) if config.scheme == 'http'
          inner, far = unix_class.pair
          relay = Thread.new { pump(outer, socket, far, deadline) }
          relay.report_on_exception = false
          Tunnel.new(inner, [outer, far], relay, deadline)
        end

        private

        def setting(env, name)
          env[name] || env[name.upcase]
        end

        def loopback?(host)
          name = host.downcase
          LOOPBACK.include?(name) || name.match?(/\A127\.\d+\.\d+\.\d+\z/)
        end

        # Ruby's URI::Generic.use_proxy? for the one address this code can know without a lookup: entries are
        # `host`, `host:port`, `.domain` (subdomains only), and for an IPv4 literal target an address or CIDR. A bare
        # name matches the host and its subdomains. `*` and `*.domain` are ordinary text, as in Ruby. Ruby also
        # resolves a name to an address to test IP entries and loopback; no lookup runs on the export path, so a name
        # that resolves to an address in an IP entry is proxied, and a name that resolves to loopback is proxied
        # unless it is written as a loopback name.
        def no_proxy?(list, host, port)
          name = host.downcase
          dotted = ".#{name}"
          address = ipv4(name)
          list.scan(/([^:,\s]+)(?::(\d+))?/) do |entry, entry_port|
            next if entry_port && Integer(entry_port, 10) != port
            if entry.start_with?('.')
              return true if name.end_with?(entry.downcase)
            elsif dotted.end_with?(".#{entry.downcase}")
              return true
            end
            return true if address && cidr_includes?(entry, address)
          end
          false
        end

        def ipv4(text)
          found = /\A(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\z/.match(text) or return nil
          octets = found.captures.map { |part| Integer(part, 10) }
          return nil if octets.any? { |octet| octet > 255 }
          octets.inject(0) { |sum, octet| (sum << 8) | octet }
        end

        def cidr_includes?(entry, address)
          base, mask = entry.split('/', 2)
          network = ipv4(base) or return false
          bits = if mask.nil? then 32
                 elsif mask.match?(/\A\d{1,2}\z/) then Integer(mask, 10)
                 end
          return false if bits.nil? || bits > 32
          shift = 32 - bits
          (network >> shift) == (address >> shift)
        end

        def decode(text) = text.gsub(/%\h\h/) { |hex| hex[1..].to_i(16).chr }.b

        # TLS to the proxy itself: full verification against the proxy's own name.
        def handshake(socket, host, tls_class, context_class, &wait)
          context = context_class.new
          context.set_params
          io = tls_class.new(socket, context); io.sync_close = false; io.hostname = host
          loop do
            state = io.connect_nonblock(exception: false)
            break unless %i[wait_readable wait_writable].include?(state)
            wait.call(io, state)
          end
          io.post_connection_check(host)
          io
        rescue StandardError
          raise KeyTransport::Refused.new('proxy TLS identity was not verified', reason: 'proxy refused')
        end

        def write_all(io, data, &wait)
          offset = 0
          while offset < data.bytesize
            count = io.write_nonblock(data.byteslice(offset..), exception: false)
            if count.is_a?(Integer)
              offset += count
            else
              wait.call(io, count)
            end
          end
        end

        # One byte at a time, so nothing past the proxy's answer is consumed before the destination handshake.
        def read_head(io, &wait)
          head = +''.b
          until head.end_with?("\r\n\r\n")
            raise IOError if head.bytesize > 8192
            part = io.read_nonblock(1, exception: false)
            if part.is_a?(String)
              head << part
            elsif part.nil?
              raise EOFError
            else
              wait.call(io, part)
            end
          end
          head
        end

        # Copies bytes both ways between the proxy connection and the end the destination handshake runs on.
        def pump(outer, raw, far, deadline)
          loop do
            pending = outer.respond_to?(:pending) && outer.pending.to_i.positive?
            remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
            break if remaining <= 0
            ready = pending ? [raw] : IO.select([raw, far], nil, nil, remaining)&.first
            break unless ready
            if ready.include?(raw)
              data = outer.read_nonblock(16_384, exception: false)
              break if data.nil?
              far.write(data) if data.is_a?(String)
            end
            if ready.include?(far)
              data = far.read_nonblock(16_384, exception: false)
              break if data.nil?
              outer.write(data) if data.is_a?(String)
            end
          end
        rescue StandardError
          nil
        ensure
          [far, raw].each { |io| io.close unless io.closed? } rescue nil
        end
      end

      # The IO the destination handshake runs on, and what must be closed with it.
      class Tunnel
        attr_reader :io
        def initialize(io, extras, relay, deadline)
          @io = io; @extras = extras; @relay = relay; @deadline = deadline
        end

        # The relay is stopped and has finished before the TLS object it shares is closed. The wait is bounded by the
        # export deadline, and a stopped thread ends at once.
        def close
          if @relay
            @relay.kill
            @relay.join([@deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC), 0.05].max)
          end
          [@io, *@extras].each do |io|
            io.close unless io.nil? || io.closed?
          rescue StandardError
            nil
          end
        end
      end
    end
  end
end
