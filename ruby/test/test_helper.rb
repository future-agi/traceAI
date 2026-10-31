# frozen_string_literal: true
require 'minitest/autorun'
require 'socket'
require 'json'
require 'open3'
require 'timeout'
require 'rbconfig'
require 'zlib'
require 'stringio'
require 'opentelemetry/sdk'
require 'opentelemetry/exporter/otlp'
ROOT = File.expand_path('..', __dir__)
class MockHTTP
  attr_reader :requests, :port
  def initialize(&handler)
    @server = TCPServer.new('127.0.0.1', 0); @port = @server.addr[1]; @requests = Queue.new; @raw_handler = nil
    @thread = Thread.new do
      loop do
        io = @server.accept
        begin
          line = io.gets; next unless line
          headers = {}; while (h = io.gets) && h != "\r\n"; k, v = h.split(':', 2); headers[k.downcase] = v.to_s.strip; end
          body = io.read(headers.fetch('content-length', '0').to_i) || ''
          request = {line: line, headers: headers, body: body}; @requests << request
          if @raw_handler
            @raw_handler.call(io, request)
            next
          end
          status, extra, payload = handler.call(request)
          io.write "HTTP/1.1 #{status} OK\r\nConnection: close\r\n"
          extra.each { |k, v| io.write "#{k}: #{v}\r\n" }
          if payload.is_a?(Array)
            io.write "Transfer-Encoding: chunked\r\n\r\n"
            payload.each { |chunk| io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\n"; io.flush }
            io.write "0\r\n\r\n"
          else
            io.write "Content-Length: #{payload.bytesize}\r\n\r\n#{payload}"
          end
        rescue IOError, SystemCallError
          nil
        ensure
          io.close
        end
      end
    rescue IOError, SystemCallError
      nil
    end
    @thread.report_on_exception = false
  end
  def raw(&handler) = @raw_handler = handler
  def url = "http://127.0.0.1:#{@port}"
  def stop
    @server.close; @thread.kill; @thread.join
  end
end
module ChildHelper
  def child_env(overrides = {})
    env = ENV.to_h.select { |k, _| k.start_with?('FI_', 'OTEL_') }.to_h { |k, _| [k, nil] }
    env.merge({'FI_API_KEY' => 'fixture-api-12345678', 'FI_SECRET_KEY' => 'fixture-secret-12345678', 'FI_BASE_URL' => @collector.url,
               'FI_PROJECT_NAME' => 'ruby-e2e', 'RUBYOPT' => '-rtraceai/auto',
               'http_proxy' => 'http://127.0.0.1:9', 'https_proxy' => 'http://127.0.0.1:9', 'HTTP_PROXY' => 'http://127.0.0.1:9', 'HTTPS_PROXY' => 'http://127.0.0.1:9', 'no_proxy' => 'localhost,127.0.0.1', 'NO_PROXY' => 'localhost,127.0.0.1', 'RUBYLIB' => File.join(ROOT, 'lib'),
               'GEM_HOME' => File.join(ROOT, '.bundle/vendor/ruby/3.2.0'), 'GEM_PATH' => File.join(ROOT, '.bundle/vendor/ruby/3.2.0'),
               'FI_CONFIG_FILE' => File.join(ROOT, '.bundle/nonexistent.toml')}).merge(overrides)
  end
  def app(code, overrides = {}, bundled: false, chdir: ROOT)
    args = bundled ? [RbConfig.ruby, '/opt/homebrew/opt/ruby/bin/bundle', 'exec', RbConfig.ruby, '-e', code] : [RbConfig.ruby, '-e', code]
    Timeout.timeout(15) { Open3.capture3(child_env(overrides), *args, chdir: chdir) }
  end
  def export_spans
    requests = []
    requests << @collector.requests.pop(true) until @collector.requests.empty?
    requests.flat_map do |req|
      assert_equal '/tracer/v1/traces', req[:line].split[1]
      assert_equal 'fixture-api-12345678', req[:headers]['x-api-key']
      assert_equal 'fixture-secret-12345678', req[:headers]['x-secret-key']
      data = req[:body]
      data = Zlib::GzipReader.new(StringIO.new(data)).read if req[:headers]['content-encoding'] == 'gzip'
      proto = Opentelemetry::Proto::Collector::Trace::V1::ExportTraceServiceRequest.decode(data)
      proto.resource_spans.flat_map { |rs| rs.scope_spans.flat_map { |ss| ss.spans.map { |span| [span, attrs(rs.resource.attributes)] } } }
    end
  end
  def attrs(values)
    values.to_h do |kv|
      v = kv.value
      result = case v.value
               when :array_value then v.array_value.values.map { |item| item.public_send(item.value) }
               when :kvlist_value then v.kvlist_value.to_h
               else v.public_send(v.value)
               end
      [kv.key, result]
    end
  end
end
