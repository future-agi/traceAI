# frozen_string_literal: true
require_relative 'test_helper'
require 'minitest/mock'
require 'traceai/auto/agent'
require 'traceai/auto/wire'
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

  # Exports through the same resource wrapper production uses, so records are encodable.
  def send_span(config)
    tracer = OpenTelemetry::SDK::Trace::TracerProvider.new.tracer('app')
    s = tracer.start_span('chat'); s.finish
    wire = A::WireExporter.new
    exporter = A::ResourceExporter.new(wire, A::ExportResource.new({'project_name' => 'kt'}))
    [wire, ->(timeout) { exporter.export([s.to_span_data], timeout: timeout) }]
  end

  # Runs one export and reports whether any connection was attempted and to whom.
  def export_to(base, opt_in: nil)
    opt_in ? ENV['FI_ALLOW_INSECURE_HTTP'] = opt_in : ENV.delete('FI_ALLOW_INSECURE_HTTP')
    config = {'base_url' => base, 'api_key' => KEY, 'secret_key' => SECRET}
    attempts = []
    result = nil
    err = capture_io do
      A.stub(:config, config) do
        wire, send = send_span(config)
        socket_class = wire.send(:load_socket)
        fake = ->(host, port, **) { attempts << [host, port]; raise IOError, 'stopped before any bytes' }
        socket_class.stub(:tcp, fake) { result = send.call(1) }
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
    'empty host with port' => 'http://:4318',
    'bracketed name' => 'http://[localhost]:4318',
    'bracketed ipv4' => 'http://[127.0.0.1]:4318',
    'empty port' => 'http://collector.test:'
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

  # The gate decides on the endpoint the socket is opened to, not on later config.
  def test_gate_uses_the_endpoint_fixed_at_construction
    config = {'base_url' => 'http://collector.test:4318', 'api_key' => KEY, 'secret_key' => SECRET}
    attempts = []
    A.stub(:config, config) do
      wire, send = send_span(config)
      config['base_url'] = 'http://localhost:4318'
      wire.send(:load_socket).stub(:tcp, ->(h, pt, **) { attempts << h; raise IOError }) { send.call(1) }
    end
    assert_empty attempts
  end

  def test_exactly_one_trailing_dot_marks_a_future_agi_host
    _, attempts, = export_to('http://api.futureagi.com.', opt_in: '1')
    assert_empty attempts
    _, attempts, = export_to('http://api.futureagi.com..', opt_in: '1')
    assert_equal 1, attempts.length
    _, attempts, = export_to('http://api.futureagi.com..')
    assert_empty attempts
  end

  def test_loopback_names_match_without_regard_to_case
    %w[http://LOCALHOST:4318 http://Localhost:4318].each do |base|
      _, attempts, = export_to(base)
      assert_equal 1, attempts.length, base
    end
    peer = Struct.new(:ip) { def remote_address = Addrinfo.tcp(ip, 4318) }
    refute A::KeyTransport.peer_agrees?('LOCALHOST', peer.new('10.1.2.3'))
    assert A::KeyTransport.peer_agrees?('LOCALHOST', peer.new('127.0.0.1'))
  end

  # The check of the once-flag and its write are one step under the lock. A read that yields between the two lets an
  # unlocked pair of threads both see "not yet warned"; with the lock only one gets past.
  def with_slow_flag_reads
    original = Object.instance_method(:instance_variable_get)
    A::KeyTransport.define_singleton_method(:instance_variable_get) do |name|
      value = original.bind_call(self, name)
      sleep 0.01
      value
    end
    yield
  ensure
    A::KeyTransport.singleton_class.send(:remove_method, :instance_variable_get)
  end

  def test_concurrent_exports_warn_once
    ENV['FI_ALLOW_INSECURE_HTTP'] = '1'
    out = capture_io do
      with_slow_flag_reads do
        16.times.map { Thread.new { A::KeyTransport.check('http://collector.test:4318') } }.each(&:join)
      end
    end.last
    assert_equal 1, out.scan('FI_ALLOW_INSECURE_HTTP').length, out
  end

  def test_concurrent_refusals_warn_once
    out = capture_io do
      with_slow_flag_reads do
        16.times.map { Thread.new { A::KeyTransport.warn_refused(A::KeyTransport::Refused.new) } }.each(&:join)
      end
    end.last
    assert_equal 1, out.scan('refused by the key transport check').length, out
  end

  def test_opt_in_is_fixed_at_construction
    ENV['FI_ALLOW_INSECURE_HTTP'] = '1'
    config = {'base_url' => 'http://collector.test:4318', 'api_key' => KEY, 'secret_key' => SECRET}
    attempts = []
    capture_io do
      A.stub(:config, config) do
        wire, send = send_span(config)
        ENV.delete('FI_ALLOW_INSECURE_HTTP')
        wire.send(:load_socket).stub(:tcp, ->(h, pt, **) { attempts << h; raise IOError }) { send.call(1) }
      end
    end
    assert_equal ['collector.test'], attempts
  end

  # A loopback name that connects to another peer: refused, socket closed, nothing written.
  def test_export_refuses_a_non_loopback_peer_and_closes_the_socket
    written = []
    sock = Object.new
    sock.define_singleton_method(:remote_address) { Addrinfo.tcp('10.1.2.3', 4318) }
    sock.define_singleton_method(:close) { @closed = true }
    sock.define_singleton_method(:closed?) { !!@closed }
    %i[write_nonblock write].each { |m| sock.define_singleton_method(m) { |*a, **| written << a; a.first.to_s.bytesize } }
    config = {'base_url' => 'http://localhost:4318', 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    capture_io do
      A.stub(:config, config) do
        wire, send = send_span(config)
        wire.send(:load_socket).stub(:tcp, ->(*_a, **) { sock }) { result = send.call(1) }
      end
    end
    assert_equal 1, result
    assert sock.closed?
    assert_empty written
  end

  def test_a_refused_send_warns_once_without_values
    A::KeyTransport.reset_warning
    errors = 3.times.map { export_to('http://secret-host.test:4318').last }
    joined = errors.join
    assert_equal 1, joined.scan('FI_ALLOW_INSECURE_HTTP').length, errors.inspect
    refute_includes joined, 'secret-host'
    refute_includes joined, KEY
  end

  # The base URL cannot be parsed: refused before any connection, with the one value-free warning.
  def test_an_unparseable_base_url_is_refused_with_one_value_free_warning
    errors = 2.times.map { export_to('http:///tracer').last }
    attempts = export_to('http:///tracer').then { |_, tried, _| tried }
    assert_empty attempts
    joined = errors.join
    assert_equal 1, joined.scan('refused by the key transport check').length, errors.inspect
    assert_includes joined, 'unparseable URL'
    refute_includes joined, 'tracer'
    refute_includes joined, KEY
    refute_includes joined, SECRET
  end

  # The warning does not claim the target is the problem for a redirect or a peer refusal, and names a reason from a short list.
  def test_the_refusal_warning_is_neutral_and_names_a_reason_from_a_fixed_list
    reasons = {'http://secret-host.test:4318' => 'insecure target', 'http://api.futureagi.com' => 'insecure target',
               'http://user:pass@127.0.0.1:4318' => 'unparseable URL'}
    reasons.each do |base, reason|
      A::KeyTransport.reset_warning
      err = export_to(base).last
      assert_includes err, 'Future AGI export refused by the key transport check', base
      assert_includes err, reason, base
      refute_includes err, 'not allowed to receive keys', base
      refute_includes err, 'secret-host'
      refute_includes err, 'pass'
    end
    A::KeyTransport.reset_warning
    out = capture_io { A::KeyTransport.warn_refused(A::KeyTransport::Refused.new('redirect answered a key-carrying send', reason: 'redirect')) }.last
    assert_includes out, 'redirect'
    refute_includes out, 'not allowed to receive keys'
    A::KeyTransport.reset_warning
    out = capture_io { A::KeyTransport.warn_refused(A::KeyTransport::Refused.new('connected peer is not loopback', reason: 'peer not loopback')) }.last
    assert_includes out, 'peer not loopback'
  end

  def test_decision_function_is_the_single_gate
    assert A.const_defined?(:KeyTransport)
    assert_equal true, A::KeyTransport.allowed?('http://127.0.0.1:4318')
    assert_equal false, A::KeyTransport.allowed?('http://api.futureagi.com')
  end

  # A plaintext send never uses a proxy; the opt-in send goes direct.
  def test_opt_in_plaintext_send_goes_direct_when_a_proxy_is_in_the_environment
    proxy = MockHTTP.new { |_| [200, {}, ''] }
    collector = MockHTTP.new { |_| [200, {}, ''] }
    ENV['HTTP_PROXY'] = ENV['http_proxy'] = proxy.url
    ENV['FI_ALLOW_INSECURE_HTTP'] = '1'
    config = {'base_url' => "http://127.1:#{collector.port}", 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    A.stub(:config, config) { _, send = send_span(config); result = send.call(3) }
    assert_equal 0, result
    assert_equal 1, collector.requests.size
    assert_equal 0, proxy.requests.size
  ensure
    proxy&.stop; collector&.stop
  end

  # An https export goes to the proxy named by the https variable (CONNECT, see test_https_proxy.rb); the plaintext
  # variable is ignored for it.
  def test_https_export_dials_the_https_proxy_and_ignores_the_plaintext_variable
    ENV['HTTPS_PROXY'] = 'http://127.0.0.1:9'
    ENV['HTTP_PROXY'] = ENV['http_proxy'] = 'http://127.0.0.1:8'
    _, attempts, = export_to('https://api.futureagi.com')
    assert_equal [['127.0.0.1', 9]], attempts
    ENV.delete('HTTPS_PROXY')
    _, attempts, = export_to('https://api.futureagi.com')
    assert_equal [['api.futureagi.com', 443]], attempts
  end

  # Rule 6: a 3xx answer is a refusal; the redirect target is never contacted.
  def test_redirect_is_a_refusal_and_the_target_is_never_contacted
    target = MockHTTP.new { |_| [200, {}, ''] }
    collector = MockHTTP.new { |_| [302, {'Location' => "#{target.url}/sink"}, ''] }
    config = {'base_url' => collector.url, 'api_key' => KEY, 'secret_key' => SECRET}
    result = nil
    A.stub(:config, config) { _, send = send_span(config); result = send.call(3) }
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
