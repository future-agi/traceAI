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
          real.call(host == TARGET_HOST ? '127.0.0.1' : host, port, **options)
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
      while (header = ssl.gets) && header != "\r\n"; end
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
end
