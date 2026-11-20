# frozen_string_literal: true
require_relative 'test_helper'
require 'minitest/mock'
require 'traceai/auto/agent'
require 'traceai/auto/export'

# FI keys leave the process only over a checked transport: the decision runs on
# the target as written and again on the peer actually connected.
class KeyTransportTest < Minitest::Test
  A = TraceAI::Auto
  KEY = 'fixture-api-key-12345678'
  SECRET = 'fixture-secret-key-12345678'

  def setup
    %w[FI_ALLOW_INSECURE_HTTP HTTP_PROXY http_proxy HTTPS_PROXY https_proxy NO_PROXY no_proxy].each { |name| ENV.delete(name) }
    A::KeyTransport.reset_warning if A.const_defined?(:KeyTransport)
  end

  def teardown
    %w[FI_ALLOW_INSECURE_HTTP HTTP_PROXY http_proxy HTTPS_PROXY https_proxy].each { |name| ENV.delete(name) }
  end

  def span
    tracer = OpenTelemetry::SDK::Trace::TracerProvider.new.tracer('app')
    s = tracer.start_span('chat'); s.finish
    s.to_span_data
  end

  # Runs one export and reports whether any connection was attempted and to whom.
  def export_to(base, opt_in: nil)
    ENV['FI_ALLOW_INSECURE_HTTP'] = opt_in if opt_in
    config = {'base_url' => base, 'api_key' => KEY, 'secret_key' => SECRET}
    attempts = []
    result = nil
    err = capture_io do
      A.stub(:config, config) do
        exporter = A::WireExporter.new
        socket_class = exporter.send(:load_socket)
        fake = ->(host, port, **) { attempts << [host, port]; raise IOError, 'stopped before any bytes' }
        socket_class.stub(:tcp, fake) { result = exporter.export([span], timeout: 1) }
      end
    end.last
    [result, attempts, err]
  end

  ALLOWED = {
    'loopback ipv4' => 'http://127.0.0.1:4318',
    'loopback name' => 'http://localhost:4318',
    'loopback ipv6' => 'http://[::1]:4318',
    'FI https' => 'https://api.futureagi.com',
    'FI subdomain https' => 'https://eu.api.futureagi.com',
    'FI https trailing dot' => 'https://api.futureagi.com.',
    'other https' => 'https://collector.test',
    'lookalike https' => 'https://futureagi.com.example.test'
  }.freeze
  REFUSED = {
    'FI plaintext' => 'http://api.futureagi.com',
    'FI plaintext trailing dot' => 'http://api.futureagi.com.',
    'FI plaintext upper case' => 'http://API.FutureAGI.com',
    'FI bare plaintext' => 'http://futureagi.com',
    'other plaintext' => 'http://collector.test:4318',
    'trailing dot loopback' => 'http://127.0.0.1.:4318',
    'short loopback' => 'http://127.1:4318',
    'hex loopback' => 'http://0x7f.0.0.1:4318',
    'decimal loopback' => 'http://2130706433:4318',
    'backslash before at' => 'http://127.0.0.1\\@collector.test:4318',
    'backslash before at FI' => 'https://api.futureagi.com\\@collector.test',
    'percent-encoded backslash' => 'http://127.0.0.1%5C@collector.test:4318',
    'at after authority' => 'http://127.0.0.1#@collector.test',
    'userinfo' => 'http://user:pass@127.0.0.1:4318',
    'empty authority' => 'http:///tracer',
    'empty host with port' => 'http://:4318'
  }.freeze

  ALLOWED.each do |name, base|
    define_method("test_allowed_#{name.tr(' ', '_')}_reaches_the_connect") do
      _, attempts, = export_to(base)
      assert_equal 1, attempts.length, "#{base} was refused"
    end
  end

  REFUSED.each do |name, base|
    define_method("test_refused_#{name.tr(' ', '_')}_before_any_connect") do
      result, attempts, err = export_to(base)
      assert_equal 1, result
      assert_empty attempts, "#{base} was connected"
      refute_includes err, KEY
      refute_includes err, SECRET
    end
  end

  def test_future_agi_plaintext_is_refused_even_with_the_opt_in
    ['http://api.futureagi.com', 'http://api.futureagi.com.', 'http://API.FUTUREAGI.COM'].each do |base|
      _, attempts, = export_to(base, opt_in: '1')
      assert_empty attempts, base
    end
  end

  def test_only_exactly_1_enables_the_opt_in
    ['true', 'yes', ' 1', '1 ', '01', 'TRUE', ''].each do |value|
      _, attempts, = export_to('http://collector.test:4318', opt_in: value)
      assert_empty attempts, value.inspect
    end
    _, attempts, = export_to('http://collector.test:4318', opt_in: '1')
    assert_equal [['collector.test', 4318]], attempts
  end

  def test_other_spellings_of_loopback_need_the_opt_in_and_then_connect
    ['http://127.1:4318', 'http://2130706433:4318', 'http://127.0.0.1.:4318'].each do |base|
      _, attempts, = export_to(base, opt_in: '1')
      assert_equal 1, attempts.length, base
    end
  end

  def test_opt_in_warns_once_per_process_without_values
    errors = 3.times.map { export_to('http://secret-host.test:4318', opt_in: '1').last }
    warnings = errors.join.scan('FI_ALLOW_INSECURE_HTTP')
    assert_equal 1, warnings.length, errors.inspect
    refute_includes errors.join, 'secret-host'
    refute_includes errors.join, KEY
  end

  def test_refusal_is_silent_about_values_and_does_not_need_debug
    _, _, err = export_to('http://collector.test:4318')
    refute_includes err, 'collector.test'
  end

  def test_decision_function_is_the_single_gate
    assert A.const_defined?(:KeyTransport)
    assert_equal true, A::KeyTransport.allowed?('http://127.0.0.1:4318')
    assert_equal false, A::KeyTransport.allowed?('http://api.futureagi.com')
  end

  # Rule 5: no environment proxy is ever consulted; the opt-in send goes direct.
  def test_opt_in_plaintext_send_goes_direct_when_a_proxy_is_in_the_environment
    proxy = MockHTTP.new { |_| [200, {}, ''] }
    collector = MockHTTP.new { |_| [200, {}, ''] }
    ENV['HTTP_PROXY'] = ENV['http_proxy'] = proxy.url
    ENV['FI_ALLOW_INSECURE_HTTP'] = '1'
    config = {'base_url' => "http://127.1:#{collector.port}", 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    A.stub(:config, config) { result = A::WireExporter.new.export([span], timeout: 3) }
    assert_equal 0, result
    assert_equal 1, collector.requests.size
    assert_equal 0, proxy.requests.size
  ensure
    proxy&.stop; collector&.stop
  end

  # Rule 6: a 3xx answer is a refusal; the redirect target is never contacted.
  def test_redirect_is_a_refusal_and_the_target_is_never_contacted
    target = MockHTTP.new { |_| [200, {}, ''] }
    collector = MockHTTP.new { |_| [302, {'Location' => "#{target.url}/sink"}, ''] }
    config = {'base_url' => collector.url, 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    A.stub(:config, config) { result = A::WireExporter.new.export([span], timeout: 3) }
    assert_equal 1, result
    assert_equal 1, collector.requests.size
    assert_equal 0, target.requests.size
    assert A::KeyTransport.refuses_redirect?("HTTP/1.1 302 Found\r\n")
    assert A::KeyTransport.refuses_redirect?("HTTP/1.1 308 Permanent Redirect\r\n")
    refute A::KeyTransport.refuses_redirect?("HTTP/1.1 200 OK\r\n")
  ensure
    target&.stop; collector&.stop
  end

  # The connected peer must agree with a loopback claim.
  def test_a_loopback_name_connected_to_another_peer_is_refused
    peer = Struct.new(:ip) do
      def remote_address = Addrinfo.tcp(ip, 4318)
    end
    refute A::KeyTransport.peer_agrees?('localhost', peer.new('10.1.2.3'))
    assert A::KeyTransport.peer_agrees?('localhost', peer.new('127.0.0.1'))
    assert A::KeyTransport.peer_agrees?('localhost', peer.new('::1'))
    assert A::KeyTransport.peer_agrees?('collector.test', peer.new('10.1.2.3'))
  end
end
