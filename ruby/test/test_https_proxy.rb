# frozen_string_literal: true
require_relative 'test_helper'
require 'minitest/mock'
require 'openssl'
require 'tempfile'
require 'traceai/auto/agent'
require 'traceai/auto/wire'
require 'traceai/auto/export'

# An export may go through an HTTPS proxy with CONNECT. The tunnelled TLS session is verified against the
# destination host, plaintext key-carrying sends are never proxied, and no refusal falls back to a direct send.
class HttpsProxyTest < Minitest::Test
  A = TraceAI::Auto
  KEY = 'fixture-api-key-12345678'
  SECRET = 'fixture-secret-key-12345678'
  TARGET_HOST = 'collector.test'
  PROXY_ENV = %w[FI_ALLOW_INSECURE_HTTP HTTP_PROXY http_proxy HTTPS_PROXY https_proxy NO_PROXY no_proxy SSL_CERT_FILE].freeze

  # A certificate authority and the certificates it issues, generated once per process.
  module Pki
    module_function

    def issue(subject, san, issuer: nil, ca: false)
      key = OpenSSL::PKey::RSA.new(2048)
      cert = OpenSSL::X509::Certificate.new
      cert.version = 2; cert.serial = rand(1..2**30)
      cert.subject = OpenSSL::X509::Name.parse(subject)
      cert.issuer = issuer ? issuer[:cert].subject : cert.subject
      cert.public_key = key.public_key
      cert.not_before = Time.now - 60; cert.not_after = Time.now + 3600
      factory = OpenSSL::X509::ExtensionFactory.new
      factory.subject_certificate = cert
      factory.issuer_certificate = issuer ? issuer[:cert] : cert
      cert.add_extension(factory.create_extension('basicConstraints', ca ? 'CA:TRUE' : 'CA:FALSE', true))
      cert.add_extension(factory.create_extension('subjectAltName', san)) if san
      cert.sign(issuer ? issuer[:key] : key, OpenSSL::Digest.new('SHA256'))
      {cert: cert, key: key}
    end

    def ca = @ca ||= issue('/CN=test-ca', nil, ca: true)
    def other_ca = @other_ca ||= issue('/CN=untrusted-ca', nil, ca: true)
    def target = @target ||= issue("/CN=#{TARGET_HOST}", "DNS:#{TARGET_HOST}", issuer: ca)
    def wrong_name = @wrong_name ||= issue('/CN=elsewhere.test', 'DNS:elsewhere.test', issuer: ca)
    def untrusted = @untrusted ||= issue("/CN=#{TARGET_HOST}", "DNS:#{TARGET_HOST}", issuer: other_ca)
    def ip_target = @ip_target ||= issue('/CN=ip-target', 'IP:10.1.2.3', issuer: ca)
    def proxy = @proxy ||= issue('/CN=localhost', 'DNS:localhost,IP:127.0.0.1', issuer: ca)
    def proxy_untrusted = @proxy_untrusted ||= issue('/CN=localhost', 'DNS:localhost,IP:127.0.0.1', issuer: other_ca)
  end

  def self.tls_context(pair)
    context = OpenSSL::SSL::SSLContext.new
    context.cert = pair[:cert]; context.key = pair[:key]
    context
  end

  # A TLS server that records the decrypted requests it receives and the handshakes that fail.
  class TlsTarget
    attr_reader :port, :requests, :failed_handshakes
    def initialize(pair)
      @server = TCPServer.new('127.0.0.1', 0); @port = @server.addr[1]
      @requests = Queue.new; @failed = Queue.new; @context = HttpsProxyTest.tls_context(pair)
      @thread = Thread.new do
        loop do
          socket = @server.accept
          Thread.new(socket) { |client| serve(client) }
        end
      rescue IOError, SystemCallError
        nil
      end
      @thread.report_on_exception = false
    end

    def serve(client)
      ssl = OpenSSL::SSL::SSLSocket.new(client, @context); ssl.sync_close = true
      ssl.accept
      line = ssl.gets
      headers = {}
      while (header = ssl.gets) && header != "\r\n"
        name, value = header.split(':', 2); headers[name.downcase] = value.strip
      end
      body = ssl.read(headers.fetch('content-length', '0').to_i)
      @requests << {line: line, headers: headers, body: body}
      ssl.write "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    rescue OpenSSL::SSL::SSLError, IOError, SystemCallError
      @failed << true
    ensure
      (ssl || client).close rescue nil
    end

    def failed_handshakes = @failed.size
    def stop
      @server.close; @thread.kill; @thread.join
    end
  end

  # A CONNECT proxy. It records the CONNECT request, relays the tunnelled bytes to the real target and keeps the
  # bytes the client sent after CONNECT, exactly as they crossed the wire.
  class ConnectProxy
    attr_reader :port, :connects, :tunnelled
    def initialize(target_port:, tls: nil, status: 200)
      @server = TCPServer.new('127.0.0.1', 0); @port = @server.addr[1]
      @target_port = target_port; @tls = tls && HttpsProxyTest.tls_context(tls); @status = status
      @connects = Queue.new; @accepted = Queue.new; @tunnelled = +''.b; @mutex = Mutex.new
      @thread = Thread.new do
        loop do
          socket = @server.accept
          @accepted << true
          Thread.new(socket) { |client| serve(client) }
        end
      rescue IOError, SystemCallError
        nil
      end
      @thread.report_on_exception = false
    end

    def scheme = @tls ? 'https' : 'http'
    def accepted = @accepted.size
    def tunnelled_bytes = @mutex.synchronize { @tunnelled.dup }

    def serve(raw)
      client = raw
      if @tls
        client = OpenSSL::SSL::SSLSocket.new(raw, @tls); client.sync_close = true
        client.accept
      end
      head = +''
      head << client.readpartial(1) until head.end_with?("\r\n\r\n")
      lines = head.split("\r\n")
      @connects << {line: lines.first, headers: lines.drop(1).to_h { |l| k, v = l.split(':', 2); [k.downcase, v.to_s.strip] }, raw: head}
      unless @status == 200
        client.write "HTTP/1.1 #{@status} Refused\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        return
      end
      upstream = TCPSocket.new('127.0.0.1', @target_port)
      client.write "HTTP/1.1 200 Connection established\r\n\r\n"
      relay(client, raw, upstream)
    rescue OpenSSL::SSL::SSLError, IOError, SystemCallError, EOFError
      nil
    ensure
      begin; client&.close; rescue StandardError; nil; end
      begin; raw&.close; rescue StandardError; nil; end
      begin; upstream&.close; rescue StandardError; nil; end
    end

    def relay(client, raw, upstream)
      loop do
        ready = client.respond_to?(:pending) && client.pending.to_i.positive? ? [raw] : IO.select([raw, upstream], nil, nil, 5)&.first
        return unless ready
        if ready.include?(raw)
          data = client.read_nonblock(16_384, exception: false)
          next if data == :wait_readable || data == :wait_writable
          return if data.nil?
          @mutex.synchronize { @tunnelled << data.b }
          upstream.write(data)
        end
        if ready.include?(upstream)
          data = upstream.read_nonblock(16_384, exception: false)
          next if data == :wait_readable
          return if data.nil?
          client.write(data)
        end
      end
    end

    def stop
      @server.close; @thread.kill; @thread.join
    end
  end

  # A proxy that answers CONNECT as the block says, and keeps every byte the client sends.
  class RawProxy
    attr_reader :port
    def initialize(&behaviour)
      @server = TCPServer.new('127.0.0.1', 0); @port = @server.addr[1]
      @behaviour = behaviour; @received = +''.b; @mutex = Mutex.new; @accepted = Queue.new
      @thread = Thread.new do
        loop do
          client = @server.accept
          @accepted << true
          Thread.new(client) { |io| serve(io) }
        end
      rescue IOError, SystemCallError
        nil
      end
      @thread.report_on_exception = false
    end

    def accepted = @accepted.size
    def received = @mutex.synchronize { @received.dup }

    def serve(io)
      head = +''.b
      head << io.readpartial(1) until head.end_with?("\r\n\r\n")
      @mutex.synchronize { @received << head }
      @behaviour.call(io) { |data| @mutex.synchronize { @received << data.b } }
    rescue IOError, SystemCallError, EOFError
      nil
    ensure
      io.close rescue nil
    end

    def stop
      @server.close; @thread.kill; @thread.join
    end
  end

  def setup
    PROXY_ENV.each { |name| ENV.delete(name) }
    A::KeyTransport.reset_warning
    @cleanup = []
  end

  def teardown
    PROXY_ENV.each { |name| ENV.delete(name) }
    @cleanup.reverse_each { |thing| thing.respond_to?(:stop) ? thing.stop : thing.call }
  end

  def track(thing) = thing.tap { @cleanup << thing }

  # The exporter builds its TLS context from OpenSSL's default store, which is created when OpenSSL loads, so a
  # test CA is added to that store; only certificates this file issues chain to it.
  def trust(*pairs)
    pairs.each do |pair|
      next if TRUSTED.include?(pair[:cert].serial)
      file = Tempfile.new('proxy-test-ca', File.join(ROOT, '.bundle/tmp'))
      file.write(pair[:cert].to_pem); file.close
      OpenSSL::SSL::SSLContext::DEFAULT_CERT_STORE.add_file(file.path)
      file.unlink
      TRUSTED << pair[:cert].serial
    end
  end
  TRUSTED = []

  # Exports one span to https://collector.test:<port>; the name does not resolve, so the only way to reach the
  # server is the address the test maps it to. Returns [result, tcp attempts, stderr].
  def export_to(base, timeout: 3)
    config = {'base_url' => base, 'api_key' => KEY, 'secret_key' => SECRET}
    attempts = []; result = nil
    err = capture_io do
      A.stub(:config, config) do
        tracer = OpenTelemetry::SDK::Trace::TracerProvider.new.tracer('app')
        span = tracer.start_span('chat'); span.finish
        wire = A::WireExporter.new
        exporter = A::ResourceExporter.new(wire, A::ExportResource.new({'project_name' => 'proxy'}))
        socket_class = wire.send(:load_socket)
        real = socket_class.method(:tcp)
        mapped = lambda do |host, port, **options|
          attempts << [host, port]
          real.call([TARGET_HOST, '10.1.2.3'].include?(host) ? '127.0.0.1' : host, port, **options)
        end
        socket_class.stub(:tcp, mapped) { result = exporter.export([span.to_span_data], timeout: timeout) }
      end
    end.last
    [result, attempts, err]
  end

  def target_server(pair = Pki.target) = track(TlsTarget.new(pair))
  def proxy_for(target, **options) = track(ConnectProxy.new(target_port: target.port, **options))

  def refute_leaked(text, *values)
    values.each { |value| refute_includes text, value }
    refute_includes text, KEY
    refute_includes text, SECRET
  end

  def test_https_target_is_tunnelled_through_https_proxy_variable_with_connect
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result, err
    assert_equal [['127.0.0.1', proxy.port]], attempts, 'only the proxy is dialled'
    connect = proxy.connects.pop(true)
    assert_equal "CONNECT #{TARGET_HOST}:#{target.port} HTTP/1.1", connect[:line]
    request = target.requests.pop(true)
    assert_equal KEY, request[:headers]['x-api-key']
    assert_equal SECRET, request[:headers]['x-secret-key']
    assert_match(%r{\APOST /tracer/v1/traces HTTP/1\.1}, request[:line])
    refute_empty request[:body]
    refute_leaked err
  end

  def test_the_proxy_sees_only_tls_bytes_after_connect
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['https_proxy'] = "http://127.0.0.1:#{proxy.port}"
    result, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result
    seen = proxy.tunnelled_bytes
    refute_empty seen
    assert_equal 0x16, seen.getbyte(0), 'the first tunnelled byte starts a TLS handshake'
    refute_includes seen, KEY
    refute_includes seen, SECRET
    refute_includes seen, 'X-Api-Key'
    refute_includes seen, 'POST '
    refute_includes proxy.connects.pop(true)[:raw], KEY
  end

  def test_a_bad_destination_certificate_through_the_proxy_is_refused_with_no_key_bytes
    { 'wrong name' => Pki.wrong_name, 'untrusted issuer' => Pki.untrusted }.each do |label, pair|
      trust(Pki.ca)
      target = target_server(pair)
      proxy = proxy_for(target)
      ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
      result, _attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
      assert_equal 1, result, label
      assert_equal 1, proxy.connects.size, "#{label}: the tunnel was opened"
      assert_equal 0, target.requests.size, "#{label}: a request reached the target"
      refute_includes proxy.tunnelled_bytes, KEY, label
      refute_includes proxy.tunnelled_bytes, 'X-Api-Key', label
      refute_leaked err
    end
  end

  def test_the_proxy_certificate_is_never_taken_for_the_destination
    # The proxy answers CONNECT and then presents a certificate valid for the proxy's own name only.
    trust(Pki.ca)
    target = target_server(Pki.proxy)
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    result, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_equal 0, target.requests.size
  end

  def test_an_http_proxy_variable_is_not_used_for_an_http_loopback_target
    collector = track(MockHTTP.new { |_| [200, {}, ''] })
    proxy = proxy_for(collector)
    ENV['HTTP_PROXY'] = ENV['http_proxy'] = "http://127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("http://127.0.0.1:#{collector.port}")
    assert_equal 0, result, err
    assert_equal [['127.0.0.1', collector.port]], attempts
    assert_equal 1, collector.requests.size
    assert_equal 0, proxy.accepted, 'the proxy saw no connection'
  end

  def test_a_https_proxy_variable_is_not_used_for_a_loopback_https_target
    trust(Pki.ca)
    loopback = track(TlsTarget.new(Pki.proxy))
    proxy = proxy_for(loopback)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://localhost:#{loopback.port}")
    assert_equal 0, result, err
    assert_equal [['localhost', loopback.port]], attempts
    assert_equal 0, proxy.accepted
  end

  def test_a_no_proxy_match_goes_direct
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    ENV['NO_PROXY'] = "other.test,.test"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result, err
    assert_equal [[TARGET_HOST, target.port]], attempts
    assert_equal 0, proxy.accepted
    assert_equal 1, target.requests.size
  end

  def test_a_lower_case_no_proxy_match_with_a_port_goes_direct_and_another_port_does_not
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['https_proxy'] = "http://127.0.0.1:#{proxy.port}"
    ENV['no_proxy'] = "#{TARGET_HOST}:#{target.port}"
    result, attempts, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result
    assert_equal [[TARGET_HOST, target.port]], attempts
    assert_equal 0, proxy.accepted
    ENV['no_proxy'] = "#{TARGET_HOST}:1"
    result, attempts, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result
    assert_equal [['127.0.0.1', proxy.port]], attempts
    assert_equal 1, proxy.accepted
  end

  def test_proxy_userinfo_is_sent_to_the_proxy_only_and_is_in_no_log_line
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://proxy-user:p%40ss-word-9@127.0.0.1:#{proxy.port}"
    result, _attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result, err
    connect = proxy.connects.pop(true)
    expected = "Basic #{['proxy-user:p@ss-word-9'].pack('m0')}"
    assert_equal expected, connect[:headers]['proxy-authorization']
    refute_includes connect[:raw], KEY
    refute_includes connect[:raw], SECRET
    refute_includes connect[:raw].downcase, 'x-api-key'
    request = target.requests.pop(true)
    refute request[:headers].key?('proxy-authorization')
    refute_includes proxy.tunnelled_bytes, 'proxy-user'
    refute_leaked err, 'proxy-user', 'p%40ss', 'p@ss', "127.0.0.1:#{proxy.port}"
  end

  def test_a_refused_proxy_logs_nothing_from_the_proxy_url_and_does_not_send_direct
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, status: 407)
    ENV['HTTPS_PROXY'] = "http://proxy-user:secret-pass@127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_equal [['127.0.0.1', proxy.port]], attempts, 'no direct dial after the proxy refused'
    assert_equal 0, target.requests.size
    assert_includes err, 'refused by the key transport check'
    refute_leaked err, 'proxy-user', 'secret-pass', "127.0.0.1:#{proxy.port}", "#{proxy.port}"
  end

  def test_a_socks5_proxy_is_refused_value_free_and_nothing_is_dialled
    trust(Pki.ca)
    target = target_server
    ENV['HTTPS_PROXY'] = 'socks5://proxy-user:secret-pass@127.0.0.1:1080'
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_empty attempts, 'neither the proxy nor the target was dialled'
    assert_equal 0, target.requests.size
    assert_includes err, 'refused by the key transport check'
    refute_leaked err, 'socks5', 'proxy-user', 'secret-pass', '1080'
  end

  def test_other_proxy_schemes_and_malformed_proxy_values_are_refused_value_free
    trust(Pki.ca)
    target = target_server
    ['ftp://127.0.0.1:21', 'proxy.invalid:3128x', 'http://', 'socks4://127.0.0.1:1080'].each do |value|
      A::KeyTransport.reset_warning
      ENV['HTTPS_PROXY'] = value
      result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
      assert_equal 1, result, value
      assert_empty attempts, value
      refute_includes err, value, value
    end
    assert_equal 0, target.requests.size
  end

  def test_the_proxy_variables_are_read_once_when_the_exporter_starts
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    config = {'base_url' => "https://#{TARGET_HOST}:#{target.port}", 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    capture_io do
      A.stub(:config, config) do
        tracer = OpenTelemetry::SDK::Trace::TracerProvider.new.tracer('app')
        span = tracer.start_span('chat'); span.finish
        wire = A::WireExporter.new
        ENV.delete('HTTPS_PROXY'); ENV['NO_PROXY'] = TARGET_HOST
        exporter = A::ResourceExporter.new(wire, A::ExportResource.new({'project_name' => 'proxy'}))
        socket_class = wire.send(:load_socket)
        real = socket_class.method(:tcp)
        socket_class.stub(:tcp, ->(host, port, **o) { real.call(host == TARGET_HOST ? '127.0.0.1' : host, port, **o) }) do
          result = exporter.export([span.to_span_data], timeout: 3)
        end
      end
    end
    assert_equal 0, result
    assert_equal 1, proxy.accepted, 'the proxy chosen at start is still used'
  end

  def test_an_https_proxy_with_a_trusted_certificate_carries_the_tunnel
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, tls: Pki.proxy)
    ENV['HTTPS_PROXY'] = "https://127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result, err
    assert_equal [['127.0.0.1', proxy.port]], attempts
    assert_equal "CONNECT #{TARGET_HOST}:#{target.port} HTTP/1.1", proxy.connects.pop(true)[:line]
    assert_equal KEY, target.requests.pop(true)[:headers]['x-api-key']
    refute_includes proxy.tunnelled_bytes, KEY
  end

  def test_an_https_proxy_with_an_untrusted_certificate_is_refused_before_connect
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, tls: Pki.proxy_untrusted)
    ENV['HTTPS_PROXY'] = "https://proxy-user:secret-pass@127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_equal [['127.0.0.1', proxy.port]], attempts
    assert_equal 0, proxy.connects.size, 'no CONNECT, so no credentials, were sent to the unverified proxy'
    assert_equal 0, target.requests.size
    refute_leaked err, 'proxy-user', 'secret-pass'
  end

  def test_an_https_proxy_whose_name_does_not_match_is_refused
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, tls: Pki.wrong_name)
    ENV['HTTPS_PROXY'] = "https://127.0.0.1:#{proxy.port}"
    result, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_equal 0, proxy.connects.size
    assert_equal 0, target.requests.size
  end

  def test_a_redirect_through_the_tunnel_is_a_refusal
    trust(Pki.ca)
    redirecting = track(TlsTarget.new(Pki.target))
    redirecting.define_singleton_method(:serve) do |client|
      ssl = OpenSSL::SSL::SSLSocket.new(client, @context); ssl.sync_close = true; ssl.accept
      length = 0
      while (header = ssl.gets) && header != "\r\n"
        length = header.split(':', 2).last.to_i if header.downcase.start_with?('content-length')
      end
      ssl.read(length)
      @requests << {}
      ssl.write "HTTP/1.1 302 Found\r\nLocation: https://elsewhere.test/\r\nContent-Length: 0\r\n\r\n"
    rescue OpenSSL::SSL::SSLError, IOError, SystemCallError
      nil
    ensure
      (ssl || client).close rescue nil
    end
    proxy = proxy_for(redirecting)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{redirecting.port}")
    assert_equal 1, result
    assert_equal 1, proxy.accepted
    assert_equal 1, attempts.size
    assert_includes err, 'redirect'
    refute_leaked err
  end

  # ---- NO_PROXY as Ruby reads it (URI::Generic.use_proxy?), for the targets this exporter can name ----

  def export_via_proxy(target_base, pair, no_proxy: nil)
    trust(Pki.ca)
    target = track(TlsTarget.new(pair))
    proxy = proxy_for(target)
    ENV['HTTPS_PROXY'] = "http://127.0.0.1:#{proxy.port}"
    ENV['NO_PROXY'] = no_proxy if no_proxy
    result, attempts, err = export_to(target_base.call(target.port))
    [result, attempts, err, proxy, target]
  end

  def test_an_ip_target_inside_a_no_proxy_cidr_goes_direct
    result, attempts, err, proxy, = export_via_proxy(->(port) { "https://10.1.2.3:#{port}" }, Pki.ip_target, no_proxy: '10.0.0.0/8')
    assert_equal 0, result, err
    assert_equal [['10.1.2.3', attempts.first.last]], attempts
    assert_equal 0, proxy.accepted
  end

  def test_an_ip_target_equal_to_a_no_proxy_address_goes_direct_and_one_outside_a_cidr_does_not
    result, _attempts, err, proxy, = export_via_proxy(->(port) { "https://10.1.2.3:#{port}" }, Pki.ip_target, no_proxy: 'other.test, 10.1.2.3')
    assert_equal 0, result, err
    assert_equal 0, proxy.accepted
    result, attempts, err, proxy, = export_via_proxy(->(port) { "https://10.1.2.3:#{port}" }, Pki.ip_target, no_proxy: '192.168.0.0/16,10.1.0.0/24')
    assert_equal 0, result, err
    assert_equal [['127.0.0.1', proxy.port]], attempts
    assert_equal 1, proxy.accepted
  end

  def test_an_ip_target_is_not_matched_by_a_no_proxy_cidr_with_a_port_that_differs
    _result, attempts, _err, proxy, = export_via_proxy(->(port) { "https://10.1.2.3:#{port}" }, Pki.ip_target, no_proxy: '10.0.0.0/8:1')
    assert_equal [['127.0.0.1', proxy.port]], attempts
  end

  def test_a_leading_dot_matches_subdomains_only_and_a_star_is_not_special
    ['.collector.test', '*', '*.test', '.test.'].each do |entry|
      result, attempts, err, proxy, = export_via_proxy(->(port) { "https://#{TARGET_HOST}:#{port}" }, Pki.target, no_proxy: entry)
      assert_equal 0, result, "#{entry}: #{err}"
      assert_equal [['127.0.0.1', proxy.port]], attempts, "#{entry} was taken for a match"
    end
  end

  def test_a_bare_no_proxy_name_matches_the_host_and_its_subdomains
    result, attempts, = export_via_proxy(->(port) { "https://#{TARGET_HOST}:#{port}" }, Pki.target, no_proxy: 'test, collector.test')
    assert_equal 0, result
    assert_equal [[TARGET_HOST, attempts.first.last]], attempts
    result, attempts, = export_via_proxy(->(port) { "https://#{TARGET_HOST}:#{port}" }, Pki.target, no_proxy: 'ctor.test')
    assert_equal 0, result
    assert_equal '127.0.0.1', attempts.first.first, 'a name that only ends with the same letters is not a match'
  end

  # ---- a bad proxy value never disables the agent ----

  def test_a_proxy_value_that_is_not_valid_text_is_refused_value_free_and_the_exporter_still_starts
    trust(Pki.ca)
    target = target_server
    ["http://\xFF\xFE.invalid:3128", "\xC3\x28"].each do |value|
      A::KeyTransport.reset_warning
      ENV['HTTPS_PROXY'] = value.b.force_encoding('UTF-8')
      result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
      assert_equal 1, result, value.inspect
      assert_empty attempts, value.inspect
      assert_includes err, 'proxy unsupported', value.inspect
    end
    assert_equal 0, target.requests.size
  end

  def test_a_no_proxy_value_that_is_not_valid_text_never_disables_the_exporter
    trust(Pki.ca)
    target = target_server
    ENV['HTTPS_PROXY'] = 'http://127.0.0.1:9'
    ENV['NO_PROXY'] = "\xFF\xFE".b.force_encoding('UTF-8')
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_empty attempts
    assert_includes err, 'proxy unsupported'
  end

  # ---- every proxy failure is one value-free warning ----

  # The agent's own threads are found by where they run, so the test doubles' threads are not counted.
  def relay_threads
    Thread.list.select { |thread| thread.alive? && thread.backtrace.to_a.any? { |line| line.include?('proxy_tunnel.rb') } }
  end

  def assert_one_proxy_warning(err, *values)
    assert_equal 1, err.scan('refused by the key transport check').size, err
    assert_includes err, 'proxy refused'
    refute_leaked err, *values
  end

  def test_a_proxy_that_cannot_be_reached_is_one_warning
    trust(Pki.ca)
    target = target_server
    closed = TCPServer.new('127.0.0.1', 0); port = closed.addr[1]; closed.close
    ENV['HTTPS_PROXY'] = "http://proxy-user:secret-pass@127.0.0.1:#{port}"
    result, _attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_one_proxy_warning(err, 'proxy-user', 'secret-pass', port.to_s)
    assert_equal 0, target.requests.size
  end

  def malicious_proxy_run(timeout: 3, &behaviour)
    trust(Pki.ca)
    target = target_server
    proxy = track(RawProxy.new(&behaviour))
    ENV['HTTPS_PROXY'] = "http://proxy-user:secret-pass@127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}", timeout: timeout)
    assert_empty relay_threads, 'a relay thread was left running'
    assert_equal 1, result
    assert_equal [['127.0.0.1', proxy.port]], attempts, 'no direct dial'
    assert_equal 0, target.requests.size
    received = proxy.received
    refute_includes received, KEY
    refute_includes received, 'X-Api-Key'
    refute_includes received.downcase, 'x-secret-key'
    refute_includes err, 'proxy-user'
    refute_includes err, 'secret-pass'
    [err, proxy, received]
  end

  def test_a_proxy_that_closes_without_answering_is_one_warning
    err, = malicious_proxy_run { |_io| nil }
    assert_one_proxy_warning(err)
  end

  def test_a_redirect_answer_to_connect_is_a_redirect_refusal
    err, = malicious_proxy_run { |io| io.write "HTTP/1.1 302 Found\r\nLocation: http://elsewhere.test/\r\nContent-Length: 0\r\n\r\n" }
    assert_equal 1, err.scan('refused by the key transport check').size, err
    assert_includes err, 'redirect'
    refute_includes err, 'elsewhere'
  end

  def test_an_oversized_answer_head_is_one_warning
    err, = malicious_proxy_run do |io|
      io.write "HTTP/1.1 200 Connection established\r\n"
      20.times { io.write "X-Padding: #{'a' * 1000}\r\n" }
      sleep 0.3
    end
    assert_one_proxy_warning(err)
  end

  def test_a_slow_answer_head_times_out_with_one_warning_and_within_the_deadline
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    err, = malicious_proxy_run(timeout: 1) { |io| io.write 'HTTP/1.1 200 Conn'; sleep 5 }
    assert_operator Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 3
    assert_one_proxy_warning(err)
  end

  def test_extra_bytes_after_the_200_make_the_destination_handshake_fail_with_one_warning
    err, = malicious_proxy_run { |io| io.write "HTTP/1.1 200 OK\r\n\r\nGARBAGE-NOT-A-TLS-RECORD"; sleep 0.3 }
    assert_equal 1, err.scan('refused by the key transport check').size, err
  end

  def test_an_https_proxy_that_refuses_connect_leaves_no_thread_and_one_warning
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, tls: Pki.proxy, status: 407)
    ENV['HTTPS_PROXY'] = "https://proxy-user:secret-pass@127.0.0.1:#{proxy.port}"
    result, attempts, err = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 1, result
    assert_equal [['127.0.0.1', proxy.port]], attempts
    assert_empty relay_threads
    assert_one_proxy_warning(err, 'proxy-user', 'secret-pass')
  end

  def test_the_relay_of_an_https_proxy_is_stopped_and_joined_when_the_export_ends
    trust(Pki.ca)
    target = target_server
    proxy = proxy_for(target, tls: Pki.proxy)
    ENV['HTTPS_PROXY'] = "https://127.0.0.1:#{proxy.port}"
    result, = export_to("https://#{TARGET_HOST}:#{target.port}")
    assert_equal 0, result
    assert_empty relay_threads, 'the relay thread outlived the export'
  end
end
