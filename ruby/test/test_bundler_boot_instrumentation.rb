# frozen_string_literal: true
require_relative 'test_e2e'
require 'tmpdir'

class EndToEndTest
  # Expected BASE assertion REDs (not executed): boot_after_super_once,
  # boot_keeps_application_provider, runtime_forwarding_and_failed_super,
  # early_explicit_call_does_not_consume_destinationless_start.
  # The other new cases are compatibility controls, not claimed BASE REDs.
  # All children use ChildHelper's synthetic keys and loopback collector.
  def with_boot_bundle
    Dir.mktmpdir('bundler-boot', File.join(ROOT, '.bundle/tmp')) do |directory|
      probe = File.join(directory, 'boot_probe.rb')
      File.write(probe, "$boot_gems_complete = true\n")
      gemfile = File.join(directory, 'Gemfile')
      File.write(gemfile, "source 'https://rubygems.org'\ngem 'httpx', require: ['httpx', #{probe.inspect}]\ngem 'opentelemetry-sdk', require: false\n")
      overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('BUNDLE_GEMFILE' => gemfile)
      yield overrides
    end
  end

  def boot_httpx_request
    "print HTTPX.post('#{@provider.url}/v1/chat/completions', json: {model: 'gpt-4o-mini', messages: []}).to_s\n"
  end

  def test_bundler_design_boot_after_super_once
    with_boot_bundle do |overrides|
      code = <<~RUBY
        require 'bundler/setup'
        abort 'boot loaded a user gem early' if defined?(HTTPX)
        abort 'boot configured before Bundler.require' if TraceAI::Auto.provider
        TraceAI::Auto.close_startup_window
        $boot_initializations = 0
        TraceAI::Auto.singleton_class.prepend(Module.new do
          def initialize_once(arguments)
            abort 'configured before super completed' unless $boot_gems_complete && defined?(HTTPX::Session)
            $boot_initializations += 1
            super
          end
        end)
        Bundler.require
        first = TraceAI::Auto.provider
        puts 'ACTIVE=' + TraceAI::Auto.active?.to_s
        Bundler.require
        puts 'CONFIGURATIONS=' + $boot_initializations.to_s
        puts 'SAME_PROVIDER=' + first.equal?(TraceAI::Auto.provider).to_s
        puts 'WRAPPERS=' + HTTPX::Session.ancestors.count { |m| m.name == 'TraceAI::Auto::HTTPXSession' }.to_s
        #{boot_httpx_request}
      RUBY
      stdout, stderr, status = app(code, overrides)
      assert status.success?, stderr
      assert_equal "ACTIVE=true\nCONFIGURATIONS=1\nSAME_PROVIDER=true\nWRAPPERS=1\n" + CHAT, stdout
      refute_includes stderr, 'already initialized'
      assert_chat_span(export_spans)
    end
  end

  def test_bundler_design_boot_keeps_application_provider
    with_boot_bundle do |overrides|
      code = <<~RUBY
        require 'bundler/setup'
        TraceAI::Auto.close_startup_window
        require 'opentelemetry/sdk'
        host = OpenTelemetry::SDK::Trace::TracerProvider.new
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
        OpenTelemetry.tracer_provider = host
        Bundler.require
        puts 'HOST=' + TraceAI::Auto.provider.equal?(host).to_s
        processors = host.instance_variable_get(:@span_processors).dup
        Bundler.require
        abort 'application provider replaced' unless OpenTelemetry.tracer_provider.equal?(host)
        abort 'processors duplicated' unless processors == host.instance_variable_get(:@span_processors)
        #{boot_httpx_request}
        abort 'application exporter lost the span' unless memory.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
      RUBY
      stdout, stderr, status = app(code, overrides)
      assert status.success?, stderr
      assert_equal "HOST=true\n" + CHAT, stdout
      assert_chat_span(export_spans)
    end
  end

  # Provider-order regression: the SDK is selected but require:false, and the
  # bundle does not select every default gem needed for eager SDK activation.
  # Expected pre-repair RED: the first wire span misses the application exporter.
  # Exercise both normal startup observation and the no-hooks request path.
  def test_bundler_design_sdk_configured_after_boot_parents_and_exports_first_request
    [false, true].each do |close_window|
      with_boot_bundle do |overrides|
        code = <<~RUBY
          require 'bundler/setup'
          Bundler.require
          abort 'fixture loaded SDK during boot' if defined?(OpenTelemetry::SDK::Trace::TracerProvider)
          abort 'fixture did not exercise early fallback' unless TraceAI::Auto.active? && TraceAI::Auto.provider.class.name == 'TraceAI::Auto::LocalProvider'
          TraceAI::Auto.close_startup_window if #{close_window}
          require 'opentelemetry/sdk'
          memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
          OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)) }
          host = OpenTelemetry.tracer_provider
          host.tracer('application').in_span('application-parent') do
            #{boot_httpx_request}
          end
          parent = memory.finished_spans.find { |s| s.name == 'application-parent' }
          wire = memory.finished_spans.select { |s| s.attributes['traceai.capture.layer'] == 'wire' }
          abort 'first wire span missing from application exporter' unless parent && wire.length == 1
          abort 'first wire span lost application parent' unless wire.first.parent_span_id == parent.span_id && wire.first.trace_id == parent.trace_id
          abort 'application provider replaced' unless OpenTelemetry.tracer_provider.equal?(host) && TraceAI::Auto.provider.equal?(host)
          processors = host.instance_variable_get(:@span_processors).dup
          Bundler.require
          abort 'processors duplicated' unless processors == host.instance_variable_get(:@span_processors)
          abort 'startup hooks restarted' if #{close_window} && ObjectSpace.each_object(TracePoint).any?(&:enabled?)
          puts
          puts [parent.span_id, parent.trace_id, wire.first.span_id].map { |id| id.unpack1('H*') }.join(',')
        RUBY
        stdout, stderr, status = app(code, overrides)
        assert status.success?, stderr
        body, ids = stdout.split("\n", 2)
        assert_equal CHAT, body
        spans = export_spans
        assert_chat_span(spans) # Exactly one FI span; the ordinary parent stays local.
        parent_id, trace_id, span_id = ids.strip.split(',')
        wire = spans.first.first
        assert_equal parent_id, wire.parent_span_id.unpack1('H*')
        assert_equal trace_id, wire.trace_id.unpack1('H*')
        assert_equal span_id, wire.span_id.unpack1('H*')
        refute_includes stderr, 'already initialized'
      end
    end
  end

  def test_bundler_design_explicit_discovery_adopts_sdk_before_finalizing_local_boot
    with_boot_bundle do |overrides|
      code = <<~RUBY
        require 'bundler/setup'
        Bundler.require
        abort 'fixture did not start local fallback' unless TraceAI::Auto.provider.class.name == 'TraceAI::Auto::LocalProvider'
        TraceAI::Auto.close_startup_window
        require 'opentelemetry/sdk'
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)) }
        host = OpenTelemetry.tracer_provider
        TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
        TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
        abort 'explicit discovery retained local fallback' unless TraceAI::Auto.provider.equal?(host)
        host.tracer('application').in_span('application-parent') { #{boot_httpx_request.strip} }
        parent = memory.finished_spans.find { |span| span.name == 'application-parent' }
        wire = memory.finished_spans.select { |span| span.attributes['traceai.capture.layer'] == 'wire' }
        abort 'wire parent or exporter lost' unless wire.length == 1 && wire.first.parent_span_id == parent.span_id
      RUBY
      stdout, stderr, status = app(code, overrides)
      assert status.success?, stderr
      assert_equal CHAT, stdout
      assert_chat_span(export_spans)
    end
  end

  def test_bundler_design_preserves_api_watcher_until_late_sdk_configuration
    with_boot_bundle do |overrides|
      code = <<~RUBY
        require 'bundler/setup'
        require 'opentelemetry-api'
        abort 'fixture lacks early API watcher' unless TraceAI::Auto.instance_variable_get(:@provider_hook)&.enabled?
        Bundler.require
        abort 'fixture did not start local fallback' unless TraceAI::Auto.provider.class.name == 'TraceAI::Auto::LocalProvider'
        require 'opentelemetry/sdk'
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)) }
        host = OpenTelemetry.tracer_provider
        abort 'SDK not adopted before any wire call' unless TraceAI::Auto.provider.equal?(host)
        host.tracer('application').in_span('manual-ai', attributes: {'gen_ai.span.kind' => 'LLM'}) {}
        abort 'application exporter lost native span' unless memory.finished_spans.count { |span| span.name == 'manual-ai' } == 1
        puts 'EARLY_API_LATE_SDK_OK'
      RUBY
      stdout, stderr, status = app(code, overrides)
      assert status.success?, stderr
      assert_equal "EARLY_API_LATE_SDK_OK\n", stdout
      spans = export_spans
      assert_equal 1, spans.length
      assert_equal 'manual-ai', spans.first.first.name
    end
  end

  def test_bundler_design_late_sdk_adoption_keeps_explicit_init_options_available
    with_boot_bundle do |overrides|
      code = <<~RUBY
        require 'bundler/setup'
        Bundler.require
        abort 'fixture loaded SDK during boot' if defined?(OpenTelemetry::SDK::Trace::TracerProvider)
        require 'opentelemetry/sdk'
        memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry::SDK.configure { |c| c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory)) }
        host = OpenTelemetry.tracer_provider
        TraceAI::Auto.using_session('before-explicit') {}
        first = TraceAI::Auto.init(project_name: 'late-sdk-explicit', sample_rate: 1.0)
        abort 'explicit init lost host' unless first.equal?(host) && OpenTelemetry.tracer_provider.equal?(host)
        TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
        TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
        #{boot_httpx_request}
        abort 'application exporter duplicated or lost span' unless memory.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' } == 1
      RUBY
      stdout, stderr, status = app(code, overrides)
      assert status.success?, stderr
      assert_equal CHAT, stdout
      refute_includes stderr, 'already initialized'
      spans = export_spans
      assert_equal 1, spans.length
      assert_equal 'late-sdk-explicit', spans.first.last['project_name']
    end
  end

  # A stand-in Runtime makes keyword/block/return/error identity observable;
  # the real Bundler cases above exercise installation through normal setup.
  def test_bundler_design_runtime_forwarding_and_failed_super
    code = <<~RUBY
      module Bundler
        class Runtime
          attr_accessor :failure
          attr_reader :received
          def require(*groups, **options, &block)
            @received = [groups, options, block]
            raise failure if failure
            block.call
          end
        end
      end
      require 'traceai/auto'
      TraceAI::Auto.close_startup_window
      $configurations = 0
      TraceAI::Auto.singleton_class.prepend(Module.new do
        def initialize_once(arguments)
          $configurations += 1
          super
        end
      end)
      runtime = Bundler::Runtime.new
      failure = LoadError.new('synthetic application loader failure')
      runtime.failure = failure
      begin
        runtime.require(:default, option: :value) { abort 'failed load yielded' }
        abort 'load error swallowed'
      rescue LoadError => error
        abort 'load error replaced' unless error.equal?(failure)
        abort 'load error backtrace replaced' unless error.backtrace.first.include?('-e:')
      end
      abort 'configured after failed super' unless $configurations.zero?
      runtime.failure = nil
      sentinel = Object.new
      callback = proc { sentinel }
      result = runtime.require(:default, :jobs, option: sentinel, &callback)
      abort 'return changed' unless result.equal?(sentinel)
      abort 'arguments changed' unless runtime.received == [[:default, :jobs], {option: sentinel}, callback]
      runtime.require(&callback)
      puts 'CONFIGURATIONS=' + $configurations.to_s
    RUBY
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('RUBYOPT' => nil)
    stdout, stderr, status = app(code, overrides, chdir: File.dirname(ROOT))
    assert status.success?, stderr
    assert_equal "CONFIGURATIONS=1\n", stdout
    assert_empty export_spans
  end

  def test_bundler_design_explicit_late_client_before_after_and_twice
    code = <<~RUBY
      require 'bundler/setup'
      TraceAI::Auto.close_startup_window
      first = TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      abort 'explicit call loaded client' if defined?(HTTPX)
      require 'httpx'
      second = TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      third = TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      abort 'provider replaced' unless first.equal?(second) && second.equal?(third)
      abort 'wrapper duplicated' unless HTTPX::Session.ancestors.count { |m| m.name == 'TraceAI::Auto::HTTPXSession' } == 1
      abort 'startup hooks restarted' unless ObjectSpace.each_object(TracePoint).none?(&:enabled?)
      #{boot_httpx_request}
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_equal CHAT, stdout
    refute_includes stderr, 'already initialized'
    assert_chat_span(export_spans)
  end

  def test_bundler_design_early_explicit_call_does_not_consume_destinationless_start
    code = <<~RUBY
      require 'bundler/setup'
      TraceAI::Auto.close_startup_window
      TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      abort 'early call started without a destination' if TraceAI::Auto.active?
      require 'opentelemetry/sdk'
      host = OpenTelemetry::SDK::Trace::TracerProvider.new
      memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
      host.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
      OpenTelemetry.tracer_provider = host
      require 'httpx'
      TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      TraceAI::Auto.auto_instrument(instrumentations: ['httpx'])
      #{boot_httpx_request}
      puts
      puts 'HOST=' + TraceAI::Auto.provider.equal?(host).to_s
      puts 'SPANS=' + memory.finished_spans.count { |s| s.attributes['traceai.capture.layer'] == 'wire' }.to_s
    RUBY
    stdout, stderr, status = app(code, {'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil})
    assert status.success?, stderr
    assert_equal CHAT + "\nHOST=true\nSPANS=1\n", stdout
    assert_empty export_spans # Neutral destination must not create an FI route.
  end

  def test_bundler_design_without_credentials_or_when_disabled_is_inert
    with_boot_bundle do |overrides|
      [{'FI_API_KEY' => nil, 'FI_SECRET_KEY' => nil}, {'OTEL_SDK_DISABLED' => 'true'}].each do |settings|
        code = <<~RUBY
          require 'bundler/setup'
          TraceAI::Auto.close_startup_window
          Bundler.require
          Bundler.require
          TraceAI::Auto.auto_instrument
          TraceAI::Auto.auto_instrument
          puts 'ACTIVE=' + TraceAI::Auto.active?.to_s
          puts 'WRAPPED=' + HTTPX::Session.ancestors.any? { |m| m.name.to_s.start_with?('TraceAI') }.to_s
          #{boot_httpx_request}
        RUBY
        stdout, stderr, status = app(code, overrides.merge(settings))
        assert status.success?, stderr
        assert_equal "ACTIVE=false\nWRAPPED=false\n" + CHAT, stdout
        assert_operator stderr.scan('traceai-auto:').length, :<=, 1
        assert_empty export_spans
      end
    end
  end

  def test_bundler_design_kernel_and_zeitwerk_directory_autoload_are_unchanged
    Dir.mktmpdir('loader-compatibility', File.join(ROOT, '.bundle/tmp')) do |directory|
      FileUtils.mkdir_p(File.join(directory, 'support'))
      File.write(File.join(directory, 'support/item.rb'), "class BootFixture::Support::Item; def self.answer = 42; end\n")
      File.write(File.join(directory, 'relative.rb'), "$relative_loaded = true\n")
      entry = File.join(directory, 'entry.rb')
      File.write(entry, "require_relative 'relative'\n")
      code = <<~RUBY
        require 'bundler/setup'
        def kernel_shape
          names = [:require, :require_relative, :load]
          [Kernel.ancestors.dup, Kernel.singleton_class.ancestors.dup,
           names.map { |name| Kernel.instance_method(name) },
           names.map { |name| Kernel.method(name) },
           names.map { |name| Kernel.private_instance_methods.include?(name) }]
        end
        before = kernel_shape
        require 'traceai/auto'
        abort 'agent changed Kernel' unless before == kernel_shape
        require 'zeitwerk'
        loader_shape = kernel_shape
        module BootFixture; end
        loader = Zeitwerk::Loader.new
        loader.push_dir(#{directory.inspect}, namespace: BootFixture)
        loader.ignore(#{entry.inspect}, #{File.join(directory, 'relative.rb').inspect})
        loaded = []
        loader.on_load { |name, _value, _path| loaded << name }
        loader.setup
        TraceAI::Auto.auto_instrument
        abort 'directory autoload failed' unless BootFixture::Support::Item.answer == 42
        abort 'post-load callbacks lost' unless loaded.include?('BootFixture::Support') && loaded.include?('BootFixture::Support::Item')
        abort 'first require return changed' unless require(#{entry.inspect}) == true
        abort 'repeat require return changed' unless require(#{entry.inspect}) == false
        abort 'relative resolution changed' unless $relative_loaded
        eval("require_relative 'relative'", TOPLEVEL_BINDING, #{entry.inspect}, 1)
        TraceAI::Auto.auto_instrument
        abort 'agent changed Zeitwerk aliases' unless loader_shape == kernel_shape
        puts 'LOADERS_UNCHANGED'
      RUBY
      stdout, stderr, status = app(code, {'RUBYOPT' => nil})
      assert status.success?, stderr
      assert_equal "LOADERS_UNCHANGED\n", stdout
      assert_empty export_spans
    end
  end
end
