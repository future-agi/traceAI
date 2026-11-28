# frozen_string_literal: true
require_relative 'test_helper'
require 'pp'
require 'traceai/auto/proxy_tunnel'

# The route chosen for an HTTPS proxy holds the Proxy-Authorization value, so printing it must not print the user, the
# password or that value, wherever the object ends up: a console, an error reporter, a crash dump, an exception message.
class ProxyConfigPrintingTest < Minitest::Test
  Tunnel = TraceAI::Auto::ProxyTunnel
  USER = 'proxy-user-fixture'
  PASSWORD_WRITTEN = 'p%40ss-word-fixture-9'
  PASSWORD = 'p@ss-word-fixture-9'
  BASIC = "Basic #{["#{USER}:#{PASSWORD}"].pack('m0')}"
  TARGET = 'https://collector.test:4318/v1/traces'

  def config(proxy = "http://#{USER}:#{PASSWORD_WRITTEN}@127.0.0.1:3128")
    Tunnel.route(TARGET, 'https_proxy' => proxy)
  end

  def secrets
    [USER, PASSWORD_WRITTEN, PASSWORD, BASIC, BASIC.delete_prefix('Basic '), [PASSWORD].pack('m0'), [USER].pack('m0')]
  end

  def refute_secret(text, label)
    secrets.each { |secret| refute_includes text, secret, "#{label} printed #{secret.length} characters of a credential" }
  end

  def test_the_route_is_a_config_that_carries_the_credentials_for_the_send
    assert_instance_of Tunnel::Config, config
    assert_equal BASIC, config.authorization
  end

  def test_inspect_prints_no_credential
    refute_secret config.inspect, 'inspect'
  end

  def test_to_s_prints_no_credential
    refute_secret config.to_s, 'to_s'
  end

  def test_pretty_print_prints_no_credential
    refute_secret PP.pp(config, +''), 'pretty_print'
    refute_secret PP.pp(config, +'', 10), 'pretty_print at a narrow width'
  end

  def test_an_exception_message_that_interpolates_the_config_prints_no_credential
    refute_secret "proxy failed: #{config}", 'interpolated to_s'
    refute_secret "proxy failed: #{config.inspect}", 'interpolated inspect'
    error = assert_raises(RuntimeError) { raise "proxy failed: #{config.inspect}" }
    refute_secret error.message, 'exception message'
    refute_secret error.full_message(highlight: false), 'full_message'
  end

  def test_a_container_holding_the_config_prints_no_credential
    refute_secret [config].inspect, 'array inspect'
    refute_secret({route: config}.inspect, 'hash inspect')
    refute_secret PP.pp({route: config}, +''), 'hash pretty_print'
  end

  def test_the_printed_proxy_names_the_redacted_userinfo_and_the_proxy
    assert_equal 'http://[redacted]@127.0.0.1:3128', config.to_s
    assert_includes config.inspect, 'http://[redacted]@127.0.0.1:3128'
    assert_includes PP.pp(config, +''), 'http://[redacted]@127.0.0.1:3128'
  end

  def test_a_proxy_without_credentials_prints_as_scheme_host_and_port
    plain = config('https://proxy.test:8443')
    assert_equal 'https://proxy.test:8443', plain.to_s
    assert_includes plain.inspect, 'https://proxy.test:8443'
    refute_includes plain.to_s, 'redacted'
  end

  def test_an_ipv6_proxy_prints_with_brackets
    assert_equal 'http://[redacted]@[::1]:3128', config("http://#{USER}:#{PASSWORD_WRITTEN}@[::1]:3128").to_s
  end

  def test_a_user_without_a_password_prints_no_credential
    refute_secret config("http://#{USER}@127.0.0.1:3128").inspect, 'inspect of a user-only proxy'
  end

  def test_no_field_of_the_config_holds_the_decoded_user_or_password
    held = config.to_a.compact.map(&:to_s) + config.instance_variables.map { |name| config.instance_variable_get(name).to_s }
    [USER, PASSWORD, PASSWORD_WRITTEN].each do |secret|
      held.each { |value| refute_includes value, secret, 'a field of the config holds a decoded credential' }
    end
    assert_equal %i[scheme host port authorization], config.members
  end

  def test_a_refused_setting_prints_no_part_of_the_setting
    refused = Tunnel.route(TARGET, 'https_proxy' => "socks5://#{USER}:#{PASSWORD_WRITTEN}@127.0.0.1:1080")
    assert_instance_of Tunnel::Unsupported, refused
    refute_secret refused.inspect, 'inspect of a refused setting'
    refute_secret refused.to_s, 'to_s of a refused setting'
  end
end
