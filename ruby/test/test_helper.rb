# frozen_string_literal: true
require 'minitest/autorun'
require 'socket'
require 'json'
require 'open3'
require 'timeout'
require 'rbconfig'
require 'zlib'
require 'stringio'
require 'fileutils'
require 'opentelemetry/sdk'
require 'opentelemetry/exporter/otlp'
ROOT = File.expand_path('..', __dir__)
# Scratch Gemfiles, configs and multipart bodies live here; .bundle/ is ignored.
FileUtils.mkdir_p(File.join(ROOT, '.bundle/tmp'))
# Child processes use the gems this test process uses, wherever Bundler or
# RubyGems installed them. Nothing assumes one machine's directory layout.
CHILD_GEM_HOME = Gem.dir
CHILD_GEM_PATH = Gem.path.join(File::PATH_SEPARATOR)
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
               'GEM_HOME' => CHILD_GEM_HOME, 'GEM_PATH' => CHILD_GEM_PATH,
               'FI_CONFIG_FILE' => File.join(ROOT, '.bundle/nonexistent.toml')}).merge(overrides)
  end
  # Child code for an application that configures its own OpenTelemetry SDK
  # provider. The agent never makes a provider of its own global, so child code
  # that records application spans sets one up first, as a real application does.
  def app_sdk
    "require 'opentelemetry/sdk'\n" \
      "OpenTelemetry.tracer_provider = OpenTelemetry::SDK::Trace::TracerProvider.new unless OpenTelemetry.tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)\n"
  end
  # The bundle executable of the running Bundler (set by `bundle exec`).
  def bundle_command
    ENV['BUNDLE_BIN_PATH'] || Gem.bin_path('bundler', 'bundle')
  end
  def app(code, overrides = {}, bundled: false, chdir: ROOT)
    args = bundled ? [RbConfig.ruby, bundle_command, 'exec', RbConfig.ruby, '-e', code] : [RbConfig.ruby, '-e', code]
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

# On an OpenTelemetry SDK whose span hooks the agent cannot verify (1.3 and 1.4), the spans the agent exports (AI and server spans, and every span sent to a supplied exporter) export only
# identity, timing and usage metadata from the application SDK path, so tests that read exported content
# do not apply there. test_media_sdk_verification.rb covers that fail-closed behaviour.
#
# A probe that cannot run is not "unproven": a child that exits non-zero, writes to stderr or prints
# anything but exactly true or false fails the suite. With TRACEAI_EXPECT_MEDIA_PROVEN=1 an SDK whose span
# data reports remote parents (1.10 and later, which the suite is built for) must prove the hooks, so
# a regression there fails the suite instead of skipping the content tests.
module SkipWhenMediaHooksUnproven
  def self.interpret(out, err, status)
    unless status.success?
      raise "media hook probe failed to run: #{err.to_s.strip[0, 300]}"
    end
    raise "media hook probe wrote to stderr: #{err.to_s.strip[0, 300]}" unless err.to_s.empty?
    case out
    when 'true' then true
    when 'false' then false
    else raise "media hook probe printed unexpected output: #{out.to_s[0, 80].inspect}"
    end
  end

  def self.unproven_outcome(env, span_data_members)
    expected = env['TRACEAI_EXPECT_MEDIA_PROVEN'] == '1' && span_data_members.include?(:parent_span_is_remote)
    expected ? :fail : :skip
  end

  def self.proven?
    return @proven unless @proven.nil?
    script = "require 'opentelemetry/sdk'; require 'traceai/auto/agent'; require 'traceai/auto/media_admission'; " \
             "print TraceAI::Auto::MediaAdmission::Probe.run"
    out, err, status = Open3.capture3(RbConfig.ruby, '-I', File.join(ROOT, 'lib'), '-e', script)
    @proven = interpret(out, err, status)
  end

  def before_setup
    super
    return if SkipWhenMediaHooksUnproven.proven?
    members = OpenTelemetry::SDK::Trace::SpanData.members
    if SkipWhenMediaHooksUnproven.unproven_outcome(ENV, members) == :fail
      flunk 'the media hooks are unproven on this OpenTelemetry SDK, which reports remote parents; AI spans would lose their content'
    end
    skip 'media hooks are unproven on this OpenTelemetry SDK; AI spans export metadata only'
  end
end
