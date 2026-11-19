# frozen_string_literal: true
require_relative 'test_helper'
require 'tmpdir'
require 'digest'

# Unlike ChildHelper#app, these consumers have no checkout on their load path,
# no inherited bundle, and no SDK gems. Only the parent decodes OTLP protobuf.
class InstalledAutoPackageTest < Minitest::Test
  include ChildHelper

  CHAT = JSON.generate(id: 'installed-chat', object: 'chat.completion', model: 'gpt-4o-mini',
                       choices: [{index: 0, message: {role: 'assistant', content: 'installed reply'}, finish_reason: 'stop'}],
                       usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5}).freeze
  PROMPT = 'installed package question'
  APP = <<~'RUBY'
    require 'net/http'
    require 'json'
    uri = URI(ENV.fetch('PROVIDER_URL') + '/v1/chat/completions')
    request = Net::HTTP::Post.new(uri)
    request['Content-Type'] = 'application/json'
    request.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'installed package question'}])
    response = Net::HTTP.start(uri.host, uri.port, open_timeout: 3, read_timeout: 3, write_timeout: 3) { |http| http.request(request) }
    abort 'provider response changed' unless response.code == '200' && response['Content-Type'] == 'application/json'
    STDOUT.write(response.body)
  RUBY
  # Separate, observational test preload: it never loads or calls the tracer,
  # changes a provider, flushes, or waits for export. app.rb stays uninstrumented.
  PROBE = <<~'RUBY'
    at_exit do
      require 'json'
      spec = Gem.loaded_specs['traceai-auto']
      File.write(ENV.fetch('LOAD_REPORT'), JSON.generate(
        features: $LOADED_FEATURES.grep(%r{/traceai/}),
        gem_root: spec&.full_gem_path, version: spec&.version&.to_s,
        gem_home: Gem.dir, gem_path: Gem.path, rubylib: ENV['RUBYLIB'],
        sdk_loaded: Gem.loaded_specs.key?('opentelemetry-sdk'),
        bundle_setup: $LOADED_FEATURES.any? { |path| path.end_with?('/bundler/setup.rb') },
        bundler: defined?(Bundler::VERSION) ? Bundler::VERSION : nil
      ))
    end
  RUBY

  def setup
    @scratch = Dir.mktmpdir('traceai-installed-', ENV['TRACEAI_PACKAGE_TMPDIR'])
    @home = File.join(@scratch, 'gems')
    @consumer = File.join(@scratch, 'consumer')
    FileUtils.mkdir_p([@home, @consumer, File.join(@scratch, 'home')])
    @environment = {
      'PATH' => [File.dirname(RbConfig.ruby), '/usr/bin', '/bin'].join(File::PATH_SEPARATOR),
      'HOME' => File.join(@scratch, 'home'), 'TMPDIR' => @scratch,
      'LANG' => 'C', 'LC_ALL' => 'C', 'GEM_HOME' => @home, 'GEM_PATH' => @home,
      'BUNDLE_IGNORE_CONFIG' => '1'
    }
    @package = ENV['TRACEAI_PACKAGE_GEM']
    unless @package
      @package = File.join(@scratch, 'traceai-auto.gem')
      run_checked(@environment, RbConfig.ruby, '-S', 'gem', 'build', 'traceai-auto.gemspec',
                  '--output', @package, chdir: ROOT, timeout: 60)
    end
    assert File.file?(@package), 'built package is missing'
    run_checked(@environment, RbConfig.ruby, '-S', 'gem', 'install', '--local', '--no-document',
                '--no-user-install', '--install-dir', @home, '--bindir', File.join(@home, 'bin'),
                @package, chdir: @consumer, timeout: 60)
    specs = Dir[File.join(@home, 'specifications', '*.gemspec')]
    assert_equal 1, specs.length, 'clean GEM_HOME must contain only the installed agent'
    assert_match(%r{/traceai-auto-[^/]+\.gemspec\z}, specs.first)
    @installed = File.join(@home, 'gems', File.basename(specs.first, '.gemspec'))
    assert File.file?(File.join(@installed, 'lib', 'traceai', 'auto.rb'))
    refute File.realpath(@consumer).start_with?(File.realpath(ROOT) + File::SEPARATOR)
    File.write(File.join(@consumer, 'app.rb'), APP)
    File.write(File.join(@consumer, 'probe.rb'), PROBE)
    @collector = MockHTTP.new { |_request| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    @provider = MockHTTP.new { |_request| [200, {'Content-Type' => 'application/json'}, CHAT] }
  end

  def teardown
    @provider&.stop
    @collector&.stop
    FileUtils.remove_entry(@scratch) if @scratch && File.directory?(@scratch)
  end

  # Read both pipes concurrently. Timeout cleanup kills/reaps this exact process
  # group, not a shell pattern. Natural exit is the only export drain in the test.
  def run_process(environment, *command, chdir:, timeout: 15)
    stdin, stdout, stderr, waiter = Open3.popen3(environment, *command, chdir: chdir,
                                               unsetenv_others: true, pgroup: true)
    stdin.close
    readers = [stdout, stderr].map do |io|
      Thread.new { io.read }.tap { |thread| thread.report_on_exception = false }
    end
    completed = false
    begin
      result = Timeout.timeout(timeout) do
        status = waiter.value
        [readers[0].value, readers[1].value, status]
      end
      completed = true
      result
    ensure
      unless completed
        begin
          Process.kill('KILL', -waiter.pid)
        rescue Errno::ESRCH
          nil
        end
      end
      waiter.join(5)
      [stdout, stderr].each { |io| io.close unless io.closed? }
      readers.each { |reader| reader.kill if reader.alive?; reader.join(1) }
    end
  end

  def run_checked(environment, *command, **options)
    stdout, stderr, status = run_process(environment, *command, **options)
    assert status.success?, "#{command.join(' ')}\n#{stdout}\n#{stderr}"
    [stdout, stderr, status]
  end

  def consumer(activate: true, hide: false, bundled: false, installed_preload: false)
    report = File.join(@consumer, 'loaded.json')
    environment = @environment.merge(
      'FI_API_KEY' => 'fixture-api-12345678', 'FI_SECRET_KEY' => 'fixture-secret-12345678',
      'FI_BASE_URL' => @collector.url, 'FI_PROJECT_NAME' => 'ruby-installed-package',
      'FI_CONFIG_FILE' => File.join(@consumer, 'absent.toml'),
      'FI_HIDE_INPUTS' => hide.to_s, 'FI_HIDE_OUTPUTS' => hide.to_s,
      'PROVIDER_URL' => @provider.url, 'LOAD_REPORT' => report
    )
    if activate
      # An absolute installed entrypoint stays resolvable after Bundler restricts
      # gem lookup to the application bundle. No checkout path or RUBYLIB.
      preload = installed_preload ? File.join(@installed, 'lib', 'traceai', 'auto.rb') : 'traceai/auto'
      environment['RUBYOPT'] = "-r#{preload}"
    end
    command = [RbConfig.ruby, '-r', File.join(@consumer, 'probe.rb'), File.join(@consumer, 'app.rb')]
    command = [RbConfig.ruby, '-S', 'bundle', 'exec', *command] if bundled
    stdout, stderr, = run_checked(environment, *command, chdir: @consumer)
    assert_equal CHAT, stdout, 'instrumentation must not change the response bytes'
    # Process exit has completed the supported at_exit shutdown; no sleep/flush.
    assert_equal 1, @provider.requests.size, 'the application must send only one request'
    request = @provider.requests.pop(true)
    assert_equal 'POST /v1/chat/completions HTTP/1.1', request[:line].strip
    assert_equal PROMPT, JSON.parse(request[:body]).dig('messages', 0, 'content')
    loaded = JSON.parse(File.read(report))
    assert_empty loaded['rubylib'].to_s
    assert_equal @home, loaded['gem_home']
    assert_equal [@home], loaded['gem_path']
    assert_equal false, loaded['sdk_loaded'], 'this proof must use the dependency-free installed pipeline'
    if activate
      if installed_preload
        # Absolute require loads the installed files, not a Gemfile member.
        assert_nil loaded['gem_root']
        assert_nil loaded['version']
      else
        assert_equal File.realpath(@installed), File.realpath(loaded.fetch('gem_root'))
      end
      refute_empty loaded['features']
      assert loaded['features'].any? { |path| path.end_with?('/traceai/auto.rb') }
      loaded['features'].each do |path|
        assert File.realpath(path).start_with?(File.realpath(@installed) + File::SEPARATOR), path
      end
    else
      assert_nil loaded['gem_root']
      assert_empty loaded['features']
    end
    [stderr, loaded]
  end

  def assert_export(hidden: false)
    spans = export_spans
    assert_equal 1, spans.length
    span, resource = spans.first
    assert_equal 'chat gpt-4o-mini', span.name
    assert_equal :SPAN_KIND_CLIENT, span.kind
    assert_equal :STATUS_CODE_OK, span.status.code
    assert_operator span.end_time_unix_nano, :>=, span.start_time_unix_nano
    assert_equal 16, span.trace_id.bytesize
    assert_equal 8, span.span_id.bytesize
    assert_equal 'ruby-installed-package', resource['project_name']
    values = attrs(span.attributes)
    assert_equal 'wire', values['traceai.capture.layer']
    assert_equal 'gpt-4o-mini', values['gen_ai.request.model']
    assert_equal 5, values['gen_ai.usage.total_tokens']
    if hidden
      assert_equal '__REDACTED__', values['input.value']
      assert_equal '__REDACTED__', values['output.value']
      exported_text = JSON.generate(attributes: values, resource: resource, name: span.name, status: span.status.message)
      refute_includes exported_text, PROMPT
      refute_includes exported_text, 'installed reply'
    else
      assert_includes values.fetch('input.value'), PROMPT
      assert_equal 'installed reply', values['output.value']
    end
  end

  def prepare_bundle
    # No path/git dependency and no network install: Ruby's default gems plus
    # the .gem just installed above. A default Bundler is a proof prerequisite.
    version = File.basename(@installed).delete_prefix('traceai-auto-')
    File.write(File.join(@consumer, 'Gemfile'), <<~GEMFILE)
      source 'https://rubygems.org'
      gem 'traceai-auto', '= #{version}', require: false
      gem 'net-http'
      gem 'json'
    GEMFILE
    run_checked(@environment, RbConfig.ruby, '-S', 'bundle', 'lock', '--local', chdir: @consumer, timeout: 30)
  end

  def application_bundle_files
    %w[Gemfile Gemfile.lock app.rb].to_h { |name| [name, File.binread(File.join(@consumer, name))] }
  end

  def prepare_application_bundle
    # Only application dependencies: the installed agent must stay outside the
    # Gemfile and lockfile. No dependency install, path source or Bundler patch.
    File.write(File.join(@consumer, 'Gemfile'), <<~GEMFILE)
      source 'https://rubygems.org'
      gem 'net-http'
      gem 'json'
    GEMFILE
    run_checked(@environment, RbConfig.ruby, '-S', 'bundle', 'lock', '--local', chdir: @consumer, timeout: 30)
    files = application_bundle_files
    refute_includes files.fetch('Gemfile'), 'traceai-auto'
    refute_includes files.fetch('Gemfile.lock'), 'traceai-auto'
    assert_equal APP, files.fetch('app.rb'), 'the application must contain no tracing code'
    files
  end

  def test_installed_gem_bundle_exec_absolute_preload_without_gemfile_membership_exports
    files = prepare_application_bundle
    _stderr, loaded = consumer(bundled: true, installed_preload: true)
    refute_nil loaded['bundler']
    assert_equal true, loaded['bundle_setup']
    assert_equal files, application_bundle_files, 'activation must not edit the application or its bundle'
    assert_equal 1, @collector.requests.size, 'one AI call must produce exactly one export'
    assert_export
  end

  def test_installed_gem_bundle_exec_without_membership_or_activation_exports_nothing
    files = prepare_application_bundle
    _stderr, loaded = consumer(activate: false, bundled: true)
    refute_nil loaded['bundler']
    assert_equal true, loaded['bundle_setup']
    assert_equal files, application_bundle_files, 'the control must leave the application and bundle unchanged'
    assert_empty @collector.requests, 'installing the gem alone must not send any export'
    assert_empty export_spans
  end

  def test_installed_gem_rubyopt_exports_on_normal_exit_without_app_instrumentation
    consumer
    assert_export
  end

  def test_installed_gem_without_activation_preserves_response_and_exports_nothing
    consumer(activate: false)
    assert_empty export_spans
  end

  def test_installed_gem_hide_switches_redact_export_not_response
    consumer(hide: true)
    assert_export(hidden: true)
  end

  def test_installed_gem_bundle_exec_with_gemfile_membership_exports
    prepare_bundle
    _stderr, loaded = consumer(bundled: true)
    refute_nil loaded['bundler']
    assert_export
  end

  def test_installed_gem_gemfile_without_bundler_setup_warns_and_exports_nothing
    prepare_bundle
    stderr, = consumer
    assert_equal 1, stderr.scan('application did not run Bundler.setup').length
    assert_empty export_spans
  end
end

# Optional evidence for the cloud script, including the full existing suite.
# Nothing prints PASS optimistically: this reporter records actual test results.
if ENV['TRACEAI_PROOF_RESULT']
  class InstalledAutoProofReporter < Minitest::AbstractReporter
    def initialize
      super
      @cases = []
    end

    def record(result)
      @cases << {name: "#{result.class_name}##{result.name}", assertions: result.assertions,
                 status: result.skipped? ? 'skip' : (result.passed? ? 'pass' : 'fail'),
                 failures: result.failures.map { |failure| failure.class.name }}
    end

    def report
      errors = @cases.count { |item| item[:failures].include?('Minitest::UnexpectedError') }
      failed = @cases.count { |item| item[:status] == 'fail' }
      evidence = {ruby: RUBY_DESCRIPTION, platform: RUBY_PLATFORM,
                  bundler: defined?(Bundler::VERSION) ? Bundler::VERSION : nil,
                  gem_sha256: ENV['TRACEAI_PACKAGE_GEM'] && Digest::SHA256.file(ENV.fetch('TRACEAI_PACKAGE_GEM')).hexdigest,
                  counts: {tests: @cases.length, assertions: @cases.sum { |item| item[:assertions] },
                           failures: failed - errors, errors: errors, skips: @cases.count { |item| item[:status] == 'skip' }},
                  cases: @cases}
      File.write(ENV.fetch('TRACEAI_PROOF_RESULT'), JSON.pretty_generate(evidence) + "\n")
    end
  end

  Minitest.extensions << 'installed_auto_proof'
  def Minitest.plugin_installed_auto_proof_init(_options)
    reporter << InstalledAutoProofReporter.new
  end
end
