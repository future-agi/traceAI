# frozen_string_literal: true
# Config loading must not activate gems before the application's Bundler.setup,
# especially when the agent is disabled or lacks credentials.
require_relative 'config'
require_relative 'secrets'
require_relative 'codec'
require_relative 'bundler_hook'
module TraceAI
  module Auto
    VERSION = '0.1.0'
    INIT_MUTEX = Mutex.new
    BUNDLER_BOOT_MUTEX = Mutex.new
    TARGETS = %w[Net::HTTP Net::HTTPResponse Net::HTTPResponse::Inflater Net::HTTPGenericRequest Net::BufferedIO HTTPX::Session HTTPX::StreamResponse HTTPX::Connection::HTTP1 OpenAI::Internal::Transport::PooledNetRequester].freeze
    MODEL_KINDS = %w[LLM EMBEDDING RERANKER GUARDRAIL].freeze
    AI_MARKERS = %w[gen_ai.span.kind traceai.capture.layer].freeze
    # Seconds the startup hooks may run after the agent loads (see load_hook).
    STARTUP_WINDOW = 60.0
    class << self
      attr_reader :config, :provider, :tracer, :processor, :exporter
      def warn_once(message)
        return if @warned
        @warned = true
        warn 'traceai-auto: ' + message
      rescue StandardError
        nil
      end
      # Preload and init each read the configuration; print each of its
      # warnings (such as an incomplete key pair) once per process.
      def announce_warnings(config)
        @announced ||= {}
        config.warnings.each do |message|
          next if @announced[message]
          @announced[message] = true
          warn 'traceai-auto: ' + message
        end
      rescue StandardError
        nil
      end
      def debug(error)
        warn "traceai-auto: internal #{error.class.name}" if @config&.truthy?('auto_debug')
      rescue StandardError
        nil
      end
      # RUBYOPT precedes Bundler. Do not require even default gems here: JSON,
      # URI, net-http, etc. may all be pinned by the application's bundle.
      def preload
        return @provider || self if @initialized
        @config ||= Config.new(announce: false)
        announce_warnings(@config)
        return self if @config.truthy?('sdk_disabled')
        # A neutral destination may be installed by the application after this
        # RUBYOPT shim. Keep dependency-free lazy hooks until it exists; do not
        # consume the once-only init or warn about missing FI keys in neutral mode.
        unless @config.credentials? || sdk_host_provider
          at_exit { warn_once('missing FI_API_KEY/FI_SECRET_KEY and application destination; instrumentation disabled') unless @initialized || sdk_host_provider }
        end
        # Never wait silently: a Gemfile in the working directory (or
        # BUNDLE_GEMFILE) without the application's Bundler.setup traces nothing.
        at_exit { warn_once("found a Gemfile but the application did not run Bundler.setup at startup; nothing was traced (start with 'bundle exec' or require 'bundler/setup')") if @config.credentials? && (@bundle_wait_expired || (@pending && waiting_for_bundle?)) }
        @pending = true
        open_startup_window
        observe_targets
        load_hook.enable unless load_hook.enabled?
        after_require
        self
      rescue StandardError => e
        debug(e)
        self
      end
      # Observe completed class and module definitions (:end events) only. Never
      # hook method calls, resolve application constants (including autoload
      # namespaces) or wrap Kernel loading. sync_hooks turns this off once nothing is
      # left to find, and the startup window bounds every other wait.
      def load_hook
        @load_hook ||= TracePoint.new(:end) do |event|
          observe_target(event.self)
          close_startup_window if startup_window_over?
        rescue StandardError => e
          debug(e)
        end
      end
      # The startup hooks wait for things that may never happen: Bundler.setup
      # in a tool such as bundle, gem or irb started with RUBYOPT exported, an
      # application provider, a first request, a client gem that is in the
      # bundle but never loaded. Each wait ends with the startup window. It is
      # checked when a class or module definition completes, so there is no
      # timer thread, and an idle process drops the hooks at its next
      # definition. Closing is final: nothing turns the hooks on again.
      def open_startup_window
        @watch_until ||= Process.clock_gettime(Process::CLOCK_MONOTONIC) + STARTUP_WINDOW
      end
      def startup_window_over?
        @window_closed || (!@watch_until.nil? && Process.clock_gettime(Process::CLOCK_MONOTONIC) > @watch_until)
      end
      # One clock read from paths that always run (a finished AI request, a
      # provider change), so that no hook outlives the window.
      def check_startup_window
        close_startup_window if !@window_closed && startup_window_over?
      end
      def close_startup_window
        @window_closed = true
        if @pending && waiting_for_bundle?
          # Bundler.setup did not run during startup: stop waiting for it. With
          # keys set, the exit warning says that nothing was traced.
          @pending = false
          @bundle_wait_expired = true
        end
        [@load_hook, @bundler_hook, @provider_hook].each { |hook| hook.disable if hook&.enabled? }
      rescue StandardError => e
        debug(e)
      end
      # One targeted hook on Bundler.setup itself, enabled once Bundler defines
      # it and turned off after its first return.
      def watch_bundler(bundler)
        return if @window_closed || @bundler_hook || @bundle_ready || @initialized || bundle_ready?
        setup = bundler.method(:setup)
        @bundler_hook = TracePoint.new(:return) do |_event|
          @bundle_ready = true
          after_require
        rescue StandardError => e
          debug(e)
        end
        @bundler_hook.enable(target: setup)
      rescue NameError, ArgumentError, TypeError => e
        @bundler_hook = nil
        debug(e)
      end
      # One targeted hook on OpenTelemetry.tracer_provider= so that the agent
      # attaches to the application's provider when the application sets it,
      # never before. Off once an application provider is in use.
      def watch_provider(api)
        return if @window_closed || @provider_hook || !provider_watch_needed?
        setter = api.method(:tracer_provider=)
        @provider_hook = TracePoint.new(:return) do |_event|
          provider_changed
        rescue StandardError => e
          debug(e)
        end
        @provider_hook.enable(target: setter)
      rescue NameError, ArgumentError, TypeError => e
        @provider_hook = nil
        debug(e)
      end
      def provider_watch_needed?
        return false if @config.nil? || @config.truthy?('sdk_disabled')
        return true if @pending || provisional_local_boot?
        !!(@active && @sdk && @own_provider && @provider.equal?(@own_provider))
      end
      def provider_changed
        if @active
          adopt_host_provider
          sync_hooks
        else
          after_require
        end
      end
      def load_hook_needed?
        return false if @window_closed || @config.nil? || @config.truthy?('sdk_disabled')
        return true if @pending || provisional_local_boot?
        return false unless @active
        seen = @targets || {}
        needed_targets.any? { |name| !seen.key?(name) }
      end
      # Classes the active agent wraps, limited to gems this process can load.
      def needed_targets
        names = []
        names.concat(%w[Net::HTTP Net::BufferedIO]) if @sdk || transport_enabled?('net_http')
        names.concat(%w[Net::HTTPResponse Net::HTTPResponse::Inflater Net::HTTPGenericRequest]) if transport_enabled?('net_http')
        names << 'OpenAI::Internal::Transport::PooledNetRequester' if @sdk && gem_possible?('openai')
        names.concat(%w[HTTPX::Session HTTPX::StreamResponse HTTPX::Connection::HTTP1]) if (@sdk || transport_enabled?('httpx')) && gem_possible?('httpx')
        names
      end
      def gem_possible?(name)
        return Gem.loaded_specs.key?(name) if bundle_ready?
        (@possible ||= {}).fetch(name) { @possible[name] = installed?(name) }
      end
      def sync_hooks
        return close_startup_window if startup_window_over?
        if load_hook_needed?
          open_startup_window
          load_hook.enable unless load_hook.enabled?
        elsif @load_hook&.enabled?
          @load_hook.disable
        end
        @bundler_hook.disable if @bundler_hook&.enabled? && (@bundle_ready || @initialized || !@pending)
        @provider_hook.disable if @provider_hook&.enabled? && !provider_watch_needed?
      rescue StandardError => e
        debug(e)
      end
      def waiting_for_bundle?
        return false if bundle_ready?
        return true unless ENV['BUNDLE_GEMFILE'].to_s.empty?
        # Bundler also discovers Gemfile/gems.rb without BUNDLE_GEMFILE. Respect
        # that startup order rather than activating newer default gems first.
        # Only the working directory counts: a Gemfile in a parent, such as a
        # monorepo root, must not stop scripts run below it from being traced.
        # The targeted Bundler.setup hook still sees a later setup either way.
        directory = Dir.pwd
        File.file?(File.join(directory, 'Gemfile')) || File.file?(File.join(directory, 'gems.rb'))
      rescue SystemCallError
        false
      end
      def bundle_ready?
        return true if @bundle_ready
        # The lazy wrappers ask on every request while the agent waits: skip the
        # feature scan when Bundler is not loaded, and remember a positive answer.
        return false unless Object.const_defined?(:Bundler, false)
        @bundle_ready = true if $LOADED_FEATURES.any? { |path| path.end_with?('/bundler/setup.rb') }
        !!@bundle_ready
      end
      def target(name) = (@targets ||= {})[name]
      def named_instance?(value, name)
        Module.instance_method(:ancestors).bind_call(Codec.type(value)).any? { |klass| Module.instance_method(:name).bind_call(klass) == name }
      end
      def observe_targets
        @scanning_targets = true
        ObjectSpace.each_object(Class) { |klass| observe_target(klass) }
        # Bundler and OpenTelemetry are modules, not classes. Never trigger an autoload.
        %i[Bundler OpenTelemetry].each do |name|
          observe_target(Object.const_get(name, false)) if Object.const_defined?(name, false) && !Object.autoload?(name)
        end
      ensure
        @scanning_targets = false
      end
      def observe_target(klass)
        name = Module.instance_method(:name).bind_call(klass)
        if name == 'Bundler'
          watch_bundler(klass)
        elsif name == 'Bundler::Runtime'
          # Observed during normal startup or an explicit discovery pass. Never
          # resolve Bundler's autoload or force setup just to install this hook.
          prepend_once(klass, BundlerHook) unless @config&.truthy?('sdk_disabled')
        elsif name == 'OpenTelemetry'
          watch_provider(klass)
        elsif TARGETS.include?(name)
          (@targets ||= {})[name] = klass
        else
          return
        end
        after_require unless @initializing || @scanning_targets
      rescue TypeError
        nil
      end
      def prepend_once(klass, wrapper)
        klass.prepend(wrapper) if klass && !klass.ancestors.include?(wrapper)
      end
      def after_require
        return if @initializing
        if @active
          adopt_host_provider
          install_transports
        elsif @pending && !waiting_for_bundle?
          # Only an AI request or an explicit init starts the agent for good.
          # When the application has its own SDK provider, the agent attaches
          # to it at once, so that the application's AI spans and the spans
          # around the first AI request reach Future AGI too. That start is
          # provisional: until the first AI request an explicit init still
          # applies its settings. Otherwise wait for the application's
          # provider, an explicit init or the first AI request.
          if sdk_host_provider
            start({}, :attached)
          elsif bundle_ready? && !@config.credentials? && !Gem.loaded_specs.key?('opentelemetry-sdk')
            # Nothing can become a destination: there is no FI key pair, and an
            # application destination needs the OpenTelemetry SDK, which this
            # bundle does not include. Stop watching; the exit warning remains.
            @pending = false
          else
            prepend_once(target('Net::HTTP'), LazyNetHTTP)
            prepend_once(target('HTTPX::Session'), LazyHTTPX)
            prepend_once(target('HTTPX::StreamResponse'), LazyHTTPXStream)
            prepend_once(target('OpenAI::Internal::Transport::PooledNetRequester'), LazySDKRequest)
          end
        end
        sync_hooks
      rescue LoadError, SyntaxError, StandardError => e
        debug(e)
      end
      def pending? = !!@pending
      # Whether a start could succeed now: a Future AGI key pair is configured,
      # or the application has its own SDK provider. Without either, the lazy
      # wrappers and the context helpers do no work at all.
      def startable? = !!(@config&.credentials? || sdk_host_provider)
      def lazy_ready? = !!@pending && !waiting_for_bundle? && startable?
      # The lazy wrappers call this for every outgoing request while the agent
      # waits. Only an AI request, one the wire catalog matches, starts the
      # agent. Any other request (a secrets fetch, a metadata service call, a
      # webhook) starts nothing and leaves a later explicit init free to apply
      # its settings. Its credential headers are still learned, so that a later
      # AI span cannot echo them.
      def start_on_request(method, url, headers = nil)
        return false unless lazy_ready?
        require_relative 'wire'
        url = url.to_s
        begin
          Privacy.observe_request(headers, url, current_session_id) if headers.instance_of?(Hash)
        rescue StandardError => e
          debug(e)
        end
        return false unless Wire.match(method.to_s, url)
        start({}, :request)
        active?
      rescue LoadError, SyntaxError, StandardError => e
        debug(e); false
      end
      def start_on_net_http(http, request)
        return false unless lazy_ready?
        address = http.address
        host = address.include?(':') ? "[#{address}]" : address
        path = request.path
        url = path.start_with?('http://', 'https://') ? path : "#{http.use_ssl? ? 'https' : 'http'}://#{host}:#{http.port}#{path}"
        start_on_request(request.method, url, request.each_header.to_h)
      rescue StandardError => e
        debug(e); false
      end
      def start_on_sdk_request(request)
        return false unless lazy_ready? && request.instance_of?(Hash)
        start_on_request(request[:method], request[:url], request[:headers])
      rescue StandardError => e
        debug(e); false
      end
      # Every request of an HTTPX batch has its headers observed; the first AI
      # request among them starts the agent.
      def start_on_httpx(requests)
        return false unless lazy_ready?
        started = false
        requests.each do |request|
          break if started
          started = start_on_request(request.verb, request.uri, request.headers.to_h)
        end
        started
      rescue StandardError => e
        debug(e); false
      end
      def init(**arguments) = start(arguments, :explicit)
      # `how` says what asked for the start:
      #   :explicit  TraceAI::Auto.init or auto_instrument
      #   :request   the first AI request
      #   :attached  Bundler boot or the application set its own SDK provider
      #   :report    a report or doctor call
      # The first two start the agent for good. The last two are provisional:
      # until the first AI request, an explicit init replaces them and applies
      # its own destinations and settings. Only a refused explicit init warns.
      def start(arguments, how)
        return @provider || self if @initializing && @initializing_thread.equal?(Thread.current)
        INIT_MUTEX.synchronize do
          retired = false
          if @initialized
            unless (how == :explicit && @provisional) || (how == :attached && provisional_local_boot? && sdk_host_provider)
              if how == :explicit && !@repeat_logged
                @repeat_logged = true
                warn 'traceai-auto: already initialized; existing provider retained'
              end
              return @provider || self
            end
            retire_provisional_start
            retired = true
          end
          waiting = @pending
          @initialized = true; @initializing = true; @initializing_thread = Thread.current; @pending = false
          # While a replacing start builds its setup, the start stays provisional,
          # so an AI request that arrives now waits in finalize_start for the
          # mutex and then runs on the new setup.
          @provisional = retired || %i[attached report].include?(how)
          result = initialize_once(arguments)
          @provisional = %i[attached report].include?(how)
          clear_retired_start if retired && !(@active && result.equal?(@provider))
          # A report that finds nothing to start leaves the agent waiting, as
          # it was, for the application's provider or the first AI request.
          if how == :report && !@active && waiting
            @initialized = false; @provisional = false; @pending = true
          end
          result
        ensure
          @initializing = false; @initializing_thread = nil
          sync_hooks
        end
      end
      # The first AI request closes the provisional window (see start).
      # Under INIT_MUTEX, so that an explicit init that is replacing the start
      # finishes first, and one that has not begun finds the start final.
      def finalize_start
        adopt_host_provider if provisional_local_boot?
        return unless @provisional
        return @provisional = false if @initializing_thread.equal?(Thread.current)
        INIT_MUTEX.synchronize { @provisional = false }
      end
      # An explicit init replaces a provisional start. Processors cannot be
      # removed from an SDK provider, so the agent switches its own off in
      # place. Spans still queued for Future AGI are dropped, not sent with
      # settings the application has just replaced.
      def retire_provisional_start
        (@attached_processors || []).each { |processor| processor.retire if processor.respond_to?(:retire) }
        (@owned_exporters || []).each(&:retire)
        (@owned_processors || []).each do |processor|
          processor.shutdown(timeout: 0.5)
        rescue StandardError => e
          debug(e)
        end
        # The old provider, tracer and processor stay in place until the new
        # start assigns its own (see start): a request in flight never meets a
        # gap with nothing to export to.
      rescue StandardError => e
        debug(e)
      end
      # A replacing start that did not become active leaves nothing behind.
      def clear_retired_start
        @active = false
        @provider = @own_provider = @tracer = @processor = @exporter = nil
        @owned_processors = []; @borrowed_flush_targets = []; @attached_processors = []; @owned_exporters = []
        @snapshots_every_span = false
      end
      def sdk_host_provider
        return unless defined?(OpenTelemetry::SDK::Trace::TracerProvider)
        candidate = OpenTelemetry.tracer_provider
        candidate if candidate.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
      end
      def initialize_once(arguments)
        # Destinations are opaque application objects, not serializable config.
        destinations = arguments.select { |key, _| %i[provider exporter processor].include?(key) }
        @config = Config.new(arguments.reject { |key, _| destinations.key?(key) }, announce: false)
        announce_warnings(@config)
        SECRET_REGISTRY.seed(@config)
        return self if @config.truthy?('sdk_disabled')
        candidate = destinations[:provider] || sdk_host_provider
        neutral = candidate || destinations[:exporter] || destinations[:processor]
        unless @config.credentials? || neutral
          warn_once('missing FI_API_KEY/FI_SECRET_KEY and application destination; instrumentation disabled')
          return self
        end
        # With explicit destinations, FI export is opt-in even if FI keys happen
        # to be in the environment. Existing FI-only deployments retain defaults.
        fi_route = @config.credentials? && (@config['fi_export'].nil? ? destinations.values.compact.empty? : @config.truthy?('fi_export'))
        unless neutral || fi_route
          warn_once('no export destination requested; instrumentation disabled')
          return self
        end
        require_relative 'wire'
        require_relative 'export'
        require_relative 'projection'
        # Only load dependencies whose versions the application already selected.
        # Bundler normally selects them all. Standalone HTTP scripts use the
        # dependency-free pipeline, never activating even an unrelated default gem.
        # An SDK the application has already loaded is enough by itself: a
        # lockfile need not list the default gems (uri, timeout, logger) it uses.
        sdk_loaded = defined?(OpenTelemetry::SDK::Trace::TracerProvider) ? true : false
        if sdk_loaded || %w[opentelemetry-sdk opentelemetry-api opentelemetry-common opentelemetry-registry opentelemetry-semantic_conventions logger uri timeout].all? { |name| Gem.loaded_specs.key?(name) }
          require 'opentelemetry/sdk'
          @sdk = true
        end
        @owned_processors = []; @borrowed_flush_targets = []; @attached_processors = []; @owned_exporters = []
        if @sdk
          # Created once: contexts made before an explicit init replaced a
          # provisional start must keep their capture marks and attributes.
          @sdk_keys ||= [OpenTelemetry::Context.create_key('traceai.wire.capture'), OpenTelemetry::Context.create_key('traceai.context.attributes')]
          @capture_key, @attributes_key = @sdk_keys
          resource = OpenTelemetry::SDK::Resources::Resource.create(resource_attributes)
          @own_provider = nil
          if candidate
            raise ArgumentError unless candidate.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
            @provider = candidate
          else
            # A private provider for wire spans only. It is never made global:
            # the application's own OpenTelemetry configuration stays in charge,
            # and adopt_host_provider moves to the application's provider once
            # the application sets one.
            @provider = @own_provider = OpenTelemetry::SDK::Trace::TracerProvider.new(resource: OpenTelemetry::SDK::Resources::Resource.default, sampler: configured_sampler)
          end
          attached = @provider.instance_variable_get(:@span_processors)
          supplied_processor = destinations[:processor]
          supplied_exporter = destinations[:exporter]
          if supplied_processor
            # A processor takes precedence when both are supplied; never attach
            # an already-installed host processor/exporter a second time.
            attach_processor(supplied_processor) unless attached.any? { |item| item.equal?(supplied_processor) }
            @borrowed_flush_targets << supplied_processor
          elsif supplied_exporter && !attached.any? { |item| item.instance_variable_get(:@exporter).equal?(supplied_exporter) || item.instance_variable_get(:@span_exporter).equal?(supplied_exporter) }
            destination = ResourceExporter.new(supplied_exporter, ExportResource.new({}), sdk_records: true)
            add_owned_processor(destination)
          end
          # Future AGI receives only AI spans from the provider in use.
          add_owned_processor(ResourceExporter.new(WireExporter.new, resource), ai_only: true) if fi_route
          @borrowed_flush_targets << @provider if @owned_processors.empty? && @borrowed_flush_targets.empty?
          require_relative 'media_admission'
          prepend_once(OpenTelemetry::SDK::Trace::Span, MediaAdmission)
          # A provider the agent attached to before (a provisional start that
          # an explicit init replaced) keeps its marker processor; never add a
          # second one.
          ancestry = attached.find { |item| item.instance_of?(CoveredAncestry) }
          ancestry ? @attached_processors << ancestry : attach_processor(CoveredAncestry.new)
          @tracer = @provider.tracer('traceai-auto', VERSION)
        elsif fi_route && !neutral
          @capture_key = :capture; @attributes_key = :attributes
          resource = ExportResource.new(resource_attributes)
          @provider = LocalProvider.new(resource)
          # Without an SDK every emitted span is a root. OTel ratio samplers
          # use the SDK's trace-ID cutoff; explicit rates retain random sampling.
          name, @local_sample_rate = sampling_options
          @local_ratio_bound = if !@config.sample_rate_explicit && name.end_with?('traceidratio')
                                 (@local_sample_rate * (2**64 - 1)).ceil
                               end
          @exporter = ResourceExporter.new(WireExporter.new, resource)
          @processor = LocalProcessor.new(@exporter)
          @owned_processors << @processor; @owned_exporters << @exporter
        else
          warn_once('application destinations require an application-selected OpenTelemetry SDK; application left running')
          return self
        end
        @active = true
        @enabled_transports = []
        denied = @config.list('disabled_instrumentations')
        if @config.truthy?('wire_capture') && !denied.include?('wire')
          # 'wire' (or no allowlist) enables both transports; 'net_http' or
          # 'httpx' in the allowlist enables the wire layer for that transport.
          allowed = @config.list('instrumentations')
          wanted = allowed.empty? || allowed.include?('wire') ? %w[net_http httpx] : allowed & %w[net_http httpx]
          @enabled_transports = wanted - denied
        end
        observe_targets
        install_transports
        install_exit_hooks
        warn Codec.generate(report) if @config.truthy?('auto_debug')
        @provider
      rescue LoadError, SyntaxError, StandardError => e
        @active = false
        warn_once("initialization failed (#{e.class.name}); application left running")
        self
      end
      # Resolve only for providers the agent creates. An explicit rate keeps its
      # existing parent-based ratio semantics; an OTel ratio is never clamped.
      def sampling_options
        return ['parentbased_traceidratio', @config['sample_rate']] if @config.sample_rate_explicit
        name = @config.otel_sampler
        return ['parentbased_traceidratio', 1.0] if name.empty?
        rate = case name
               when 'always_on', 'parentbased_always_on' then 1.0
               when 'always_off', 'parentbased_always_off' then 0.0
               when 'traceidratio', 'parentbased_traceidratio'
                 argument = @config.otel_sampler_arg
                 value = Float(argument.nil? ? 1.0 : argument)
                 raise ArgumentError unless value.finite? && (0.0..1.0).cover?(value)
                 value
               else
                 raise ArgumentError
               end
        [name, rate]
      rescue ArgumentError, TypeError
        # Separate from general initialization/export warnings, and deduplicated
        # even if an explicit init replaces a provisional start. Never echo values.
        unless @sampler_warned
          @sampler_warned = true
          warn 'traceai-auto: invalid OTEL_TRACES_SAMPLER or OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIdRatio(1.0))'
        end
        ['parentbased_traceidratio', 1.0]
      end
      def configured_sampler
        name, rate = sampling_options
        samplers = OpenTelemetry::SDK::Trace::Samplers
        root = case name.delete_prefix('parentbased_')
               when 'always_on' then samplers::ALWAYS_ON
               when 'always_off' then samplers::ALWAYS_OFF
               when 'traceidratio' then samplers.trace_id_ratio_based(rate)
               end
        name.start_with?('parentbased_') ? samplers.parent_based(root: root) : root
      end
      def add_owned_processor(destination, ai_only: false)
        processor = OpenTelemetry::SDK::Trace::Export::BatchSpanProcessor.new(destination, exporter_timeout: 4000, max_queue_size: 512, max_export_batch_size: 64)
        processor.singleton_class.prepend(QuietBatchResult)
        attach_processor(ai_only ? AISpanFilter.new(processor) : processor)
        # A supplied exporter receives a sanitized copy of every span.
        @snapshots_every_span = true unless ai_only
        @owned_processors << processor; @owned_exporters << destination
        @processor = processor; @exporter = destination
      end
      # An AI span: one that carries gen_ai.span.kind or traceai.capture.layer,
      # or one made by the agent's own tracer (a wire span keeps its scope even
      # if a tight attribute limit dropped traceai.capture.layer).
      def ai_marked?(attributes, scope)
        return true if attributes && AI_MARKERS.any? { |key| attributes.key?(key) }
        !!(scope && scope.name == 'traceai-auto')
      end
      # Media bookkeeping (MediaAdmission) runs inside the application's span
      # calls, so it is limited to spans an agent export can receive. With the
      # Future AGI route alone those are AI spans and server spans; a supplied
      # exporter receives every span.
      def media_wanted?(kind, attributes, scope)
        return true unless @active && !@snapshots_every_span
        kind == :server || ai_marked?(attributes, scope)
      end
      # Everything the agent adds to a provider, so that the same processors
      # follow the agent to the application's provider when it is adopted.
      def attach_processor(processor)
        @provider.add_span_processor(processor)
        @attached_processors << processor
      end
      # Bundler may finish before a require:false SDK is loaded. Its local
      # fallback remains replaceable until the first AI request or explicit init.
      def provisional_local_boot?
        (@bundler_booted || @bundler_booting) && @provisional && @active && !@sdk
      end
      # The agent started on its private provider (an AI request came before the
      # application configured OpenTelemetry). Once the application sets its
      # own SDK provider, attach the same processors there and parent later wire
      # spans under the application's spans. Nothing is made global.
      def adopt_host_provider
        check_startup_window
        if provisional_local_boot? && sdk_host_provider
          # Retire the unused local pipeline under INIT_MUTEX. The replacement
          # stays provisional, so explicit init still supplies its own settings.
          start({}, :attached)
          return
        end
        return unless @sdk && @own_provider && @provider.equal?(@own_provider)
        host = sdk_host_provider
        return unless host && !host.equal?(@own_provider)
        attached = host.instance_variable_get(:@span_processors) || []
        @attached_processors.each do |processor|
          host.add_span_processor(processor) unless attached.any? { |item| item.equal?(processor) }
        end
        @borrowed_flush_targets.map! { |target| target.equal?(@own_provider) ? host : target }
        @tracer = host.tracer('traceai-auto', VERSION)
        @provider = host
      rescue StandardError => e
        debug(e)
      end
      # Bundler calls this only after Runtime#require returns successfully.
      # Keep automatic setup provisional so a later application init still
      # supplies its settings. Repeated Bundler.require never reconfigures it.
      def bundler_boot
        BUNDLER_BOOT_MUTEX.synchronize do
          return if @bundler_booted || @config.nil? || @config.truthy?('sdk_disabled')
          @bundle_ready = true
          return unless active? || startable?
          @bundler_booting = true
          if @initialized
            adopt_host_provider
            observe_targets
            install_transports
          else
            start({}, :attached)
          end
          return unless active?
          install_server_instrumentation if @sdk
          @bundle_wait_expired = false
          @bundler_booted = true
          sync_hooks
        ensure
          @bundler_booting = false
        end
      rescue LoadError, SyntaxError, StandardError => e
        debug(e)
      end
      def auto_instrument(instrumentations: nil)
        @config ||= Config.new(announce: false)
        announce_warnings(@config)
        # An early explicit discovery is not a failed once-only init. Wait for
        # the application's bundle/destination, then allow the caller to retry.
        return @provider || self if @config.truthy?('sdk_disabled') || waiting_for_bundle?
        return @provider || self unless active? || startable?
        init unless @initialized
        adopt_host_provider if provisional_local_boot?
        # An explicit call: what it adds must not be undone by a later init.
        @provisional = false
        return @provider || self unless active?
        @bundle_wait_expired = false
        names = instrumentations || %w[wire rack]
        names = names.split(',').map(&:strip) if names.is_a?(String)
        @enabled_transports |= names.include?('wire') ? %w[net_http httpx] : (names & %w[net_http httpx])
        observe_targets
        install_transports
        install_server_instrumentation(force: true) if @sdk && names.include?('rack')
        sync_hooks
        @provider
      rescue StandardError => e
        debug(e); @provider || self
      end
      def active? = !!@active && !@config.truthy?('sdk_disabled')
      def wire_enabled? = active? && !(@enabled_transports || []).empty?
      def transport_enabled?(name) = wire_enabled? && @enabled_transports.include?(name)
      def current_context
        # Also adopt after startup hooks close, before capturing a request parent.
        adopt_host_provider if provisional_local_boot?
        @sdk ? OpenTelemetry::Context.current : (Thread.current[:traceai_local_context] ||= LocalContext.new({}))
      end
      def attach_context(context)
        return OpenTelemetry::Context.attach(context) if @sdk
        previous = current_context; Thread.current[:traceai_local_context] = context; previous
      end
      def with_context(context)
        token = attach_context(context)
        yield
      ensure
        detach_context(token)
      end
      def capture_allowed?
        return false unless wire_enabled?
        ctx = current_context
        return false if ctx.value(@capture_key)
        return true unless @sdk
        return false if OpenTelemetry::Common::Utilities.untraced?(ctx)
        !CoveredAncestry.covered?(OpenTelemetry::Trace.current_span(ctx))
      rescue StandardError => e
        debug(e); false
      end
      def capture_context(capture = true)
        current_context.set_value(@capture_key, capture)
      end
      def current_capture
        current_context.value(@capture_key) if @capture_key
      end
      def detach_context(token)
        return unless token
        @sdk ? OpenTelemetry::Context.detach(token) : Thread.current[:traceai_local_context] = token
      rescue StandardError => e
        debug(e)
      end
      # The context helpers never start the agent. While it waits, they only
      # remember their attributes for the current Fiber; the first AI request
      # made inside the block starts the agent and picks them up.
      # Keep helper attributes portable while a boot fallback may become SDK-backed.
      def early_context? = ((!@active && !!@pending) || provisional_local_boot?) && startable?
      def with_attributes(attributes)
        if early_context? && attributes.instance_of?(Hash)
          previous = Thread.current[:traceai_early_attributes]
          begin
            Thread.current[:traceai_early_attributes] = (previous || {}).merge(attributes)
            return yield
          ensure
            Thread.current[:traceai_early_attributes] = previous
          end
        end
        return yield unless active? && @attributes_key
        ctx = current_context
        current = ctx.value(@attributes_key) || {}
        with_context(ctx.set_value(@attributes_key, current.merge(attributes))) { yield }
      end
      # The context a request starts in: the current context plus anything a
      # context helper remembered for this Fiber before the agent had started.
      def request_context
        context = current_context
        early = Thread.current[:traceai_early_attributes]
        return context unless early && context && @attributes_key
        context.set_value(@attributes_key, early.merge(context.value(@attributes_key) || {}))
      end
      def projection_limit
        return @provider.span_limits.attribute_count_limit if @sdk
        Integer(ENV['OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT'] || ENV['OTEL_ATTRIBUTE_COUNT_LIMIT'] || 128)
      rescue ArgumentError, TypeError
        128
      end
      def emit(request, response, parent, started, finished = Time.now)
        check_startup_window
        result = Wire.build(request, response, limit: @config['wire_max_body_bytes'])
        return unless result
        adopt_host_provider if @own_provider && @provider.equal?(@own_provider)
        install_server_instrumentation if @sdk && !%w[instrumented failed].include?(@rack_status)
        context_attrs = parent.value(@attributes_key) || {}
        attributes = Privacy.mask(Privacy.scrub_attributes(context_attrs.merge(result.attributes)), @config)
        description = Privacy.hide_errors?(@config) ? '__REDACTED__' : result.description.to_s
        projection = AttributeProjection.project(attributes, projection_limit)
        attributes = projection.attributes
        if @sdk
          span = @tracer.start_span(result.name, with_parent: parent, kind: :client, start_timestamp: started, attributes: attributes)
          if projection.dropped.positive? && span.respond_to?(:to_span_data)
            span.instance_variable_set(:@traceai_projection_dropped, projection.dropped)
            span.singleton_class.prepend(ProjectedSpan)
          end
          span.status = result.ok ? OpenTelemetry::Trace::Status.ok : OpenTelemetry::Trace::Status.error(description)
          span.finish(end_timestamp: finished)
        else
          trace_id = nil
          if @local_ratio_bound
            # Match Ruby SDK TraceIdRatioBased on the same ID we export, including
            # its probability-one special case and unsigned low-64-bit comparison.
            trace_id = Random.urandom(16)
            return unless @local_sample_rate == 1.0 || trace_id[8, 8].unpack1('Q>') < @local_ratio_bound
          else
            return unless Random.rand < @local_sample_rate
          end
          record = ExportRecord.new(result.name, :client, LocalStatus.new(result.ok ? 1 : 2, description), "\0".b * 8,
                                    attributes.size + projection.dropped, 0, 0, (started.to_r * 1_000_000_000).to_i, (finished.to_r * 1_000_000_000).to_i,
                                    attributes, [], [], @provider.resource, ExportScope.new('traceai-auto', VERSION, {}),
                                    Random.urandom(8), trace_id || Random.urandom(16), 1, '', false)
          @processor.enqueue(record)
        end
      rescue StandardError => e
        debug(e)
      end
      # The session id set through using_session or with_attributes in a
      # request's context. A request body that repeats exactly this value is
      # not learned as a credential (Privacy.observe_body_credentials).
      def session_id(context)
        return unless context && @attributes_key
        value = (context.value(@attributes_key) || {})['session.id']
        value if value.instance_of?(String)
      rescue StandardError => e
        debug(e); nil
      end
      # The session id of the request being made now, also before the agent has
      # started (the context helpers then keep it in the early attributes).
      def current_session_id
        # Before the start there is no context class yet (current_context would
        # raise), so only the early attributes are read then.
        value = @attributes_key ? session_id(request_context) : nil
        value ||= (Thread.current[:traceai_early_attributes] || {})['session.id']
        value if value.instance_of?(String)
      rescue StandardError => e
        debug(e); nil
      end
      def using_session(id, &block) = with_attributes({'session.id' => id}, &block)
      def using_user(id, &block) = with_attributes({'user.id' => id}, &block)
      def using_metadata(value)
        return yield unless early_context? || (active? && @attributes_key)
        begin
          encoded = Codec.generate(value)
        rescue StandardError => e
          debug(e)
          return yield
        end
        with_attributes('metadata' => encoded) { yield }
      end
      def uuid
        bytes = Random.urandom(16).bytes
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
        hex = bytes.pack('C*').unpack1('H*')
        [hex[0, 8], hex[8, 4], hex[12, 4], hex[16, 4], hex[20, 12]].join('-')
      end
      def resource_attributes
        {'project_name' => @config['project_name'], 'project_type' => @config['project_type'].to_s.downcase,
         'project_version_name' => @config['project_version_name'] || 'DEFAULT_PROJECT_VERSION_NAME', 'project_version_id' => (@project_version_id ||= uuid),
         'eval_tags' => '[]', 'metadata' => '{}', 'semantic_convention' => 'fi'}
      end
      # Shut down only agent-created processors. Borrowed application destinations
      # may be drained, never shut down (including normal exit and SIGTERM).
      def flush(timeout: 5.0) = bounded(:force_flush, timeout)
      def shutdown(timeout: 5.0) = bounded(:shutdown, timeout)
      def bounded(method, timeout)
        owned = @owned_processors || []
        borrowed = @borrowed_flush_targets || []
        return false if owned.empty? && borrowed.empty?
        return false if @flushing
        @flushing = true
        timeout = Float(timeout).clamp(0.001, 5.0)
        worker = Thread.new do
          begin
            deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + timeout
            calls = borrowed.map { |target| [target, :force_flush] } + owned.map { |target| [target, method] }
            calls.map do |target, operation|
              remaining = deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
              remaining.positive? && target.public_send(operation, timeout: remaining) == 0
            rescue StandardError => e
              debug(e); false
            end.all?
          rescue StandardError => e
            debug(e); false
          ensure
            @flushing = false
          end
        end
        worker.report_on_exception = false
        !!(worker.join(timeout) && worker.value)
      rescue StandardError => e
        @flushing = false; debug(e); false
      end
      # Flush and shut down at exit only. Ruby's default SIGTERM handling raises
      # SignalException in the main thread, which runs at_exit hooks, and an
      # application's own TERM handler normally ends in exit, which does too, so
      # the agent never replaces the application's signal handlers.
      def install_exit_hooks
        return if @exit_hooks
        @exit_hooks = true
        at_exit { shutdown(timeout: 5) }
      end
      def installed?(name)
        !Gem::Specification.find_all_by_name(name).empty?
      rescue StandardError
        false
      end
      def install_transports
        return unless active? && (wire_enabled? || @sdk)
        require_relative 'transports'
        # Disabling wire spans must not disable request-credential discovery for
        # native/application spans still forwarded by our SDK processor. These
        # entry hooks never read response bodies unless their wire transport is on.
        if @sdk || transport_enabled?('net_http')
          prepend_once(target('Net::HTTP'), NetHTTP)
          prepend_once(target('Net::BufferedIO'), NetConnectHeaders)
          prepend_once(target('Net::HTTP'), SDKTransportContext) if @sdk
          if transport_enabled?('net_http')
            prepend_once(target('Net::HTTPResponse'), NetResponse)
            prepend_once(target('Net::HTTPResponse::Inflater'), NetInflater)
            prepend_once(target('Net::HTTPGenericRequest'), NetRequestBody)
          end
          prepend_once(target('OpenAI::Internal::Transport::PooledNetRequester'), SDKRequestContext) if @sdk
        end
        if @sdk || transport_enabled?('httpx')
          prepend_once(target('HTTPX::Session'), HTTPXSession)
          prepend_once(target('HTTPX::StreamResponse'), HTTPXStreamContext)
          prepend_once(target('HTTPX::Connection::HTTP1'), HTTPXProtocolHeaders)
          @httpx_installed = !!target('HTTPX::Session') if transport_enabled?('httpx')
        end
      end
      # Rack server spans are installed lazily (on an AI request, a report or
      # auto_instrument), only once Rack is loaded and only on an application
      # or supplied provider, after the application's own OpenTelemetry
      # configuration: never on the agent's private provider, and never ahead
      # of the application's own Rack options. Until then the status is pending.
      def install_server_instrumentation(force: false)
        return if %w[instrumented failed].include?(@rack_status) || @initializing
        return unless (force || @config.enabled?('rack')) && Gem.loaded_specs.key?('opentelemetry-instrumentation-rack')
        rack_loaded = Object.const_defined?(:Rack, false) && !Object.autoload?(:Rack)
        unless rack_loaded && @sdk && @provider && !@provider.equal?(@own_provider)
          @rack_status = 'pending'
          return
        end
        require 'opentelemetry/instrumentation/rack'
        @rack_status = OpenTelemetry::Instrumentation::Rack::Instrumentation.instance.install ? 'instrumented' : 'failed'
      rescue LoadError, SyntaxError, StandardError => e
        @rack_status = 'failed'; debug(e)
      end
      def report(test_export: false)
        # A report needs a running agent to show and to test, but its start is
        # provisional: a later explicit init still applies its settings.
        start({}, :report) if @pending && !waiting_for_bundle?
        install_server_instrumentation if active?
        integrations = {}
        {'wire' => nil, 'net_http' => 'net-http', 'httpx' => 'httpx', 'openai' => 'openai', 'ruby-openai' => 'ruby-openai',
         'anthropic' => 'anthropic', 'anthropic-sdk-ruby' => 'anthropic-sdk-ruby', 'ruby_llm' => 'ruby_llm', 'roast' => 'roast', 'rack' => 'rack'}.each do |name, gem|
          allowed = %w[net_http httpx].include?(name) ? transport_enabled?(name) : (name == 'wire' ? wire_enabled? : @config&.enabled?(name))
          allowed = true if name == 'rack' && @rack_status == 'instrumented'
          state = if !active? || !allowed || (%w[wire net_http httpx].include?(name) && !wire_enabled?)
                    'disabled'
                  elsif name == 'wire' || name == 'net_http' then 'instrumented'
                  elsif name == 'rack' && @rack_status then @rack_status
                  elsif !installed?(gem) then 'not_installed'
                  elsif name == 'httpx' then @httpx_installed ? 'instrumented' : 'pending'
                  else 'missing_instrumentor'
                  end
          integrations[name] = {status: state}
          integrations[name][:reason] = 'No dedicated Ruby traceAI instrumentor; known protocols use enabled wire transports' if state == 'missing_instrumentor'
        end
        collector = 'not_tested'
        if test_export && active? && @sdk && @exporter && @processor
          # A connectivity probe must run even with zero sampling or an unsampled
          # application parent. Use only our processor, without changing the app's
          # provider/sampler or sending diagnostics to its other exporters.
          diagnostic = OpenTelemetry::SDK::Trace::TracerProvider.new(sampler: OpenTelemetry::SDK::Trace::Samplers::ALWAYS_ON, resource: @provider.resource)
          diagnostic.add_span_processor(@processor)
          s = diagnostic.tracer('traceai-auto', VERSION).start_span('traceai-auto doctor', with_parent: OpenTelemetry::Context.empty, attributes: {'traceai.diagnostic' => true})
          s.finish
          flushed = flush(timeout: 5)
          collector = flushed && @exporter.last_result == OpenTelemetry::SDK::Trace::Export::SUCCESS ? 'accepted' : 'failed'
        elsif test_export && active? && @exporter && @processor
          # The dependency-free exporter sends the same probe directly, bypassing
          # sampling, without activating any gem.
          now = (Time.now.to_r * 1_000_000_000).to_i
          probe = ExportRecord.new('traceai-auto doctor', :internal, LocalStatus.new(0, ''), "\0".b * 8, 1, 0, 0, now, now,
                                   {'traceai.diagnostic' => true}, [], [], @provider.resource, ExportScope.new('traceai-auto', VERSION, {}),
                                   Random.urandom(8), Random.urandom(16), 1, '', false)
          collector = @exporter.export([probe], timeout: 5).zero? ? 'accepted' : 'failed'
        end
        if collector == 'not_tested'
          detail = if !test_export then 'test export not requested'
                   elsif waiting_for_bundle? then 'waiting for the application to run Bundler.setup'
                   elsif !active? then 'agent not active (no Future AGI key pair or application destination)'
                   else 'no Future AGI route (explicit destinations without fi_export: true)'
                   end
        end
        {version: VERSION, active: active?, integrations: integrations, config: @config&.masked, collector: collector, collector_detail: detail,
         projection_zero_limit_spans: defined?(AttributeProjection) ? AttributeProjection.zero_limit_spans : 0}
      end
    end
    # While the agent waits, these wrappers look only at what a request already
    # holds (method, URL and headers, never a body) and ask whether it is an AI
    # request. Only then does the agent start, and the call is redispatched to
    # the real wrapper that the start installed above this one. For any other
    # request super preserves the native API, including block absence.
    module LazyNetHTTP
      def request(req, body = nil, &block)
        return super unless Auto.pending?
        timing = [Time.now, Process.clock_gettime(Process::CLOCK_MONOTONIC)]
        previous = Thread.current[:traceai_lazy_timing]
        Thread.current[:traceai_lazy_timing] = timing
        return request(req, body, &block) if Auto.start_on_net_http(self, req)
        super
      ensure
        Thread.current[:traceai_lazy_timing] = previous if timing
      end
    end
    module LazySDKRequest
      def execute(request, ...)
        return execute(request, ...) if Auto.start_on_sdk_request(request)
        super
      end
    end
    module LazyHTTPX
      private
      # The real wrapper (HTTPXSession) keeps one batch of captures per
      # send_requests call and closes it when that call ends. When the agent
      # starts inside a call that is already running, this frame does the same
      # for it. The start can come at once, from an AI request among
      # `requests`, or later from one a plugin creates below this call: a
      # redirect to an AI route.
      def send_requests(*requests)
        return super unless Auto.pending?
        outer = Thread.current[:traceai_lazy_httpx]
        frame = Thread.current[:traceai_lazy_httpx] = []
        begin
          frame.replace(HTTPXSession.open_batch) if Auto.start_on_httpx(requests) && defined?(HTTPXSession)
          super
        rescue StandardError => e
          HTTPXSession.fail_batch(frame.first, e) unless frame.empty?
          raise
        ensure
          Thread.current[:traceai_lazy_httpx] = outer
          HTTPXSession.close_batch(*frame) unless frame.empty?
        end
      end
      def send_request(request, ...)
        frame = Thread.current[:traceai_lazy_httpx]
        if frame && frame.empty? && Auto.start_on_httpx([request]) && defined?(HTTPXSession)
          frame.replace(HTTPXSession.open_batch)
          # Redispatch to the real wrapper that the start installed above.
          return send_request(request, ...)
        end
        super
      end
    end
    module LazyHTTPXStream
      def each(&block)
        return super unless block
        return each(&block) if Auto.start_on_httpx([@request])
        super
      end
      private
      def response(...)
        return response(...) if Auto.start_on_httpx([@request])
        super
      end
    end
    # Propagate coverage through native spans without a process-wide ancestry map.
    # A marker lives only as long as its span; no exporter-visible attributes change.
    class CoveredAncestry
      def self.covered?(span)
        span.instance_variable_get(:@traceai_covered) || MODEL_KINDS.include?(span.respond_to?(:attributes) ? span.attributes&.fetch('gen_ai.span.kind', nil) : nil)
      end
      def on_start(span, parent_context)
        parent = OpenTelemetry::Trace.current_span(parent_context)
        span.instance_variable_set(:@traceai_covered, true) if self.class.covered?(span) || self.class.covered?(parent)
      rescue StandardError => e
        Auto.debug(e)
      end
      def on_finish(span); end
      def force_flush(timeout: nil) = 0
      def shutdown(timeout: nil) = 0
    end
    # The Future AGI route receives only AI spans: the agent's own wire spans,
    # spans carrying gen_ai.span.kind or traceai.capture.layer, and a server
    # span whose trace contained one of those. Other application spans, such as
    # database, queue or HTTP client spans, stay with the application's own
    # exporters. Server spans end after their children, so AI trace ids are
    # remembered in a small bounded set until then.
    class AISpanFilter
      TRACE_LIMIT = 4096
      def initialize(delegate)
        @delegate = delegate
        @traces = {}
        @mutex = Mutex.new
      end
      # Switched off in place when an explicit init replaces a provisional
      # start: an SDK provider offers no way to remove a processor.
      def retire
        @retired = true
      end
      def on_start(span, parent_context)
        @delegate.on_start(span, parent_context) unless @retired
      end
      def on_finish(span)
        return if @retired
        trace_id = span.context.trace_id
        if ai_span?(span)
          remember(trace_id)
          @delegate.on_finish(span)
        elsif span.kind == :server && seen?(trace_id)
          @delegate.on_finish(span)
        end
      rescue StandardError => e
        Auto.debug(e)
      end
      def force_flush(timeout: nil) = @delegate.force_flush(timeout: timeout)
      def shutdown(timeout: nil) = @delegate.shutdown(timeout: timeout)
      private
      def ai_span?(span)
        Auto.ai_marked?(span.attributes, span.respond_to?(:instrumentation_scope) ? span.instrumentation_scope : nil)
      end
      def remember(trace_id)
        @mutex.synchronize do
          @traces.delete(trace_id)
          @traces[trace_id] = true
          @traces.shift while @traces.size > TRACE_LIMIT
        end
      end
      def seen?(trace_id) = @mutex.synchronize { @traces.key?(trace_id) }
    end
    module QuietBatchResult
      private
      def report_result(code, spans)
        return super if code == OpenTelemetry::SDK::Trace::Export::SUCCESS
        Auto.warn_once('collector export failed; application left running')
      end
    end
    class ResourceExporter
      attr_reader :last_result
      def initialize(delegate, resource, sdk_records: false)
        @delegate, @resource, @sdk_records = delegate, resource, sdk_records
      end
      # A retired exporter (see Auto.retire_provisional_start) reports success
      # and sends nothing.
      def retire
        @retired = true
      end
      def export(spans, timeout: nil)
        return 0 if @retired
        copies = spans.map do |span|
          copy = Privacy.snapshot(span, @resource, Auto.config)
          if @sdk_records
            # The internal FI serializer uses OTLP status numbers; application
            # SDK exporters expect the SDK API's different OK/UNSET numbering.
            copy = OpenTelemetry::SDK::Trace::SpanData.new(*copy.to_a)
            copy.trace_flags = OpenTelemetry::Trace::TraceFlags.from_byte(copy.trace_flags)
            copy.tracestate = OpenTelemetry::Trace::Tracestate.from_string(copy.tracestate)
            copy.links = copy.links.map do |link|
              ctx = link.span_context
              context = OpenTelemetry::Trace::SpanContext.new(trace_id: ctx.trace_id, span_id: ctx.span_id,
                tracestate: OpenTelemetry::Trace::Tracestate.from_string(ctx.tracestate),
                trace_flags: OpenTelemetry::Trace::TraceFlags.from_byte(ctx.trace_flags), remote: ctx.remote)
              OpenTelemetry::Trace::Link.new(context, link.attributes)
            end
            copy.status = case copy.status.code
                          when 1 then OpenTelemetry::Trace::Status.ok
                          when 2 then OpenTelemetry::Trace::Status.error(copy.status.description)
                          else OpenTelemetry::Trace::Status.unset
                          end
          end
          copy
        end
        token = Auto.attach_context(Auto.capture_context)
        @last_result = @delegate.export(copies, timeout: timeout)
        Auto.warn_once('collector export failed; application left running') if @last_result != 0
        @last_result
      rescue StandardError => e
        Auto.debug(e)
        Auto.warn_once('collector export failed; application left running')
        1
      ensure
        Auto.detach_context(token)
      end
      def force_flush(timeout: nil) = @delegate.force_flush(timeout: timeout)
      def shutdown(timeout: nil) = @delegate.shutdown(timeout: timeout)
    end
  end
end
