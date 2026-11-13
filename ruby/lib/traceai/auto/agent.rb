# frozen_string_literal: true
# Config loading must not activate gems before the application's Bundler.setup,
# especially when the agent is disabled or lacks credentials.
require_relative 'config'
require_relative 'secrets'
module TraceAI
  module Auto
    VERSION = '0.1.0'
    INIT_MUTEX = Mutex.new
    TARGETS = %w[Net::HTTP Net::HTTPResponse Net::HTTPResponse::Inflater Net::HTTPGenericRequest Net::BufferedIO HTTPX::Session HTTPX::StreamResponse HTTPX::Connection::HTTP1 OpenAI::Internal::Transport::PooledNetRequester].freeze
    MODEL_KINDS = %w[LLM EMBEDDING RERANKER GUARDRAIL].freeze
    class << self
      attr_reader :config, :provider, :tracer, :processor, :exporter
      def warn_once(message)
        return if @warned
        @warned = true
        warn 'traceai-auto: ' + message
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
        return init if @initialized
        @config ||= Config.new
        return self if @config.truthy?('sdk_disabled')
        # A neutral destination may be installed by the application after this
        # RUBYOPT shim. Keep dependency-free lazy hooks until it exists; do not
        # consume the once-only init or warn about missing FI keys in neutral mode.
        unless @config.credentials? || sdk_host_provider
          at_exit { warn_once('missing FI_API_KEY/FI_SECRET_KEY and application destination; instrumentation disabled') unless @initialized || sdk_host_provider }
        end
        @pending = true
        observe_targets
        # Observe completed class definitions, never resolve application constants
        # (including intermediate autoload namespaces) and never wrap require.
        @load_hook ||= TracePoint.new(:end, :call, :return, :c_call, :c_return) do |event|
          if event.event == :end
            observe_target(event.self)
            next
          end
          if event.event == :return && event.method_id == :setup && event.return_value && Module.instance_method(:name).bind_call(event.self) == 'Bundler'
            @bundle_ready = true
            after_require if Thread.current[:traceai_require_depth].to_i.zero?
          end
          next unless event.method_id == :require || event.method_id == :require_relative
          depth = Thread.current[:traceai_require_depth].to_i
          if event.event == :call || event.event == :c_call
            Thread.current[:traceai_require_depth] = depth + 1
          else
            Thread.current[:traceai_require_depth] = [depth - 1, 0].max
            after_require if depth <= 1
          end
        rescue StandardError => e
          debug(e)
        end
        @load_hook.enable
        after_require
        self
      rescue StandardError => e
        debug(e)
        self
      end
      def waiting_for_bundle?
        return false if bundle_ready?
        return true unless ENV['BUNDLE_GEMFILE'].to_s.empty?
        # Bundler also discovers Gemfile/gems.rb without BUNDLE_GEMFILE. Respect
        # that startup order rather than activating newer default gems first.
        directory = Dir.pwd
        loop do
          return true if File.file?(File.join(directory, 'Gemfile')) || File.file?(File.join(directory, 'gems.rb'))
          parent = File.dirname(directory)
          return false if parent == directory
          directory = parent
        end
      end
      def bundle_ready?
        @bundle_ready || $LOADED_FEATURES.any? { |path| path.end_with?('/bundler/setup.rb') }
      end
      def target(name) = (@targets ||= {})[name]
      def named_instance?(value, name)
        Module.instance_method(:ancestors).bind_call(Codec.type(value)).any? { |klass| Module.instance_method(:name).bind_call(klass) == name }
      end
      def observe_targets
        @scanning_targets = true
        ObjectSpace.each_object(Class) { |klass| observe_target(klass) }
      ensure
        @scanning_targets = false
      end
      def observe_target(klass)
        name = Module.instance_method(:name).bind_call(klass)
        return unless TARGETS.include?(name)
        (@targets ||= {})[name] = klass
        after_require unless @initializing || @scanning_targets
      rescue TypeError
        nil
      end
      def prepend_once(klass, wrapper)
        klass.prepend(wrapper) if klass && !klass.ancestors.include?(wrapper)
      end
      def after_require
        return if @initializing || waiting_for_bundle?
        if @active
          install_transports
        elsif @pending
          if bundle_ready? && (@config.credentials? || sdk_host_provider)
            init
          else
            prepend_once(target('Net::HTTP'), LazyNetHTTP)
            prepend_once(target('HTTPX::Session'), LazyHTTPX)
            prepend_once(target('HTTPX::StreamResponse'), LazyHTTPXStream)
            prepend_once(target('OpenAI::Internal::Transport::PooledNetRequester'), LazySDKRequest)
          end
        end
      rescue LoadError, SyntaxError, StandardError => e
        debug(e)
      end
      def pending? = !!@pending
      def start_on_request
        return false unless @pending && !waiting_for_bundle?
        init
        active?
      rescue LoadError, SyntaxError, StandardError => e
        debug(e); false
      end
      def init(**arguments)
        return @provider || self if @initializing && @initializing_thread.equal?(Thread.current)
        INIT_MUTEX.synchronize do
          if @initialized
            unless @repeat_logged
              @repeat_logged = true
              warn 'traceai-auto: already initialized; existing provider retained'
            end
            return @provider || self
          end
          @initialized = true; @initializing = true; @initializing_thread = Thread.current; @pending = false
          initialize_once(arguments)
        ensure
          @initializing = false; @initializing_thread = nil
        end
      end
      def sdk_host_provider
        return unless defined?(OpenTelemetry::SDK::Trace::TracerProvider)
        candidate = OpenTelemetry.tracer_provider
        candidate if candidate.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
      end
      def initialize_once(arguments)
        @load_hook&.disable
        Thread.current[:traceai_require_depth] = nil
        # Destinations are opaque application objects, not serializable config.
        destinations = arguments.select { |key, _| %i[provider exporter processor].include?(key) }
        @config = Config.new(arguments.reject { |key, _| destinations.key?(key) })
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
        if %w[opentelemetry-sdk opentelemetry-api opentelemetry-common opentelemetry-registry opentelemetry-semantic_conventions logger uri timeout].all? { |name| Gem.loaded_specs.key?(name) }
          require 'opentelemetry/sdk'
          @sdk = true
        end
        @owned_processors = []; @borrowed_flush_targets = []
        if @sdk
          @capture_key = OpenTelemetry::Context.create_key('traceai.wire.capture')
          @attributes_key = OpenTelemetry::Context.create_key('traceai.context.attributes')
          resource = OpenTelemetry::SDK::Resources::Resource.create(resource_attributes)
          if candidate
            raise ArgumentError unless candidate.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
            @provider = candidate
          else
            sampler = OpenTelemetry::SDK::Trace::Samplers.parent_based(root: OpenTelemetry::SDK::Trace::Samplers.trace_id_ratio_based(@config['sample_rate']))
            @provider = OpenTelemetry::SDK::Trace::TracerProvider.new(resource: OpenTelemetry::SDK::Resources::Resource.default, sampler: sampler)
            OpenTelemetry.tracer_provider = @provider
          end
          attached = @provider.instance_variable_get(:@span_processors)
          supplied_processor = destinations[:processor]
          supplied_exporter = destinations[:exporter]
          if supplied_processor
            # A processor takes precedence when both are supplied; never attach
            # an already-installed host processor/exporter a second time.
            @provider.add_span_processor(supplied_processor) unless attached.any? { |item| item.equal?(supplied_processor) }
            @borrowed_flush_targets << supplied_processor
          elsif supplied_exporter && !attached.any? { |item| item.instance_variable_get(:@exporter).equal?(supplied_exporter) || item.instance_variable_get(:@span_exporter).equal?(supplied_exporter) }
            destination = ResourceExporter.new(supplied_exporter, ExportResource.new({}), sdk_records: true)
            add_owned_processor(destination)
          end
          add_owned_processor(ResourceExporter.new(WireExporter.new, resource)) if fi_route
          @borrowed_flush_targets << @provider if @owned_processors.empty? && @borrowed_flush_targets.empty?
          require_relative 'media_admission'
          prepend_once(OpenTelemetry::SDK::Trace::Span, MediaAdmission)
          @provider.add_span_processor(CoveredAncestry.new)
          @tracer = @provider.tracer('traceai-auto', VERSION)
        elsif fi_route && !neutral
          @capture_key = :capture; @attributes_key = :attributes
          resource = ExportResource.new(resource_attributes)
          @provider = LocalProvider.new(resource)
          @exporter = ResourceExporter.new(WireExporter.new, resource)
          @processor = LocalProcessor.new(@exporter)
          @owned_processors << @processor
        else
          warn_once('application destinations require an application-selected OpenTelemetry SDK; application left running')
          return self
        end
        @active = true
        @enabled_transports = []
        if @config.truthy?('wire_capture') && @config.enabled?('wire')
          @enabled_transports = %w[net_http httpx].reject { |name| @config.list('disabled_instrumentations').include?(name) }
        end
        observe_targets
        install_transports
        install_server_instrumentation if @sdk
        install_exit_hooks
        warn Codec.generate(report) if @config.truthy?('auto_debug')
        @provider
      rescue LoadError, SyntaxError, StandardError => e
        @active = false
        warn_once("initialization failed (#{e.class.name}); application left running")
        self
      end
      def add_owned_processor(destination)
        processor = OpenTelemetry::SDK::Trace::Export::BatchSpanProcessor.new(destination, exporter_timeout: 4000, max_queue_size: 512, max_export_batch_size: 64)
        processor.singleton_class.prepend(QuietBatchResult)
        @provider.add_span_processor(processor)
        @owned_processors << processor
        @processor = processor; @exporter = destination
      end
      def auto_instrument(instrumentations: nil)
        init unless @initialized
        return @provider || self unless active?
        names = instrumentations || %w[wire rack]
        names = names.split(',').map(&:strip) if names.is_a?(String)
        @enabled_transports |= names.include?('wire') ? %w[net_http httpx] : (names & %w[net_http httpx])
        observe_targets
        install_transports
        install_server_instrumentation(force: true) if @sdk && names.include?('rack')
        @provider
      rescue StandardError => e
        debug(e); @provider || self
      end
      def active? = !!@active && !@config.truthy?('sdk_disabled')
      def wire_enabled? = active? && !(@enabled_transports || []).empty?
      def transport_enabled?(name) = wire_enabled? && @enabled_transports.include?(name)
      def current_context
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
      def with_attributes(attributes)
        init if @pending && !waiting_for_bundle?
        return yield unless active? && @attributes_key
        ctx = current_context
        current = ctx.value(@attributes_key) || {}
        with_context(ctx.set_value(@attributes_key, current.merge(attributes))) { yield }
      end
      def projection_limit
        return @provider.span_limits.attribute_count_limit if @sdk
        Integer(ENV['OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT'] || ENV['OTEL_ATTRIBUTE_COUNT_LIMIT'] || 128)
      rescue ArgumentError, TypeError
        128
      end
      def emit(request, response, parent, started, finished = Time.now)
        result = Wire.build(request, response, limit: @config['wire_max_body_bytes'])
        return unless result
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
        elsif Random.rand < @config['sample_rate']
          record = ExportRecord.new(result.name, :client, LocalStatus.new(result.ok ? 1 : 2, description), "\0".b * 8,
                                    attributes.size + projection.dropped, 0, 0, (started.to_r * 1_000_000_000).to_i, (finished.to_r * 1_000_000_000).to_i,
                                    attributes, [], [], @provider.resource, ExportScope.new('traceai-auto', VERSION, {}),
                                    Random.urandom(8), Random.urandom(16), 1, '', false)
          @processor.enqueue(record)
        end
      rescue StandardError => e
        debug(e)
      end
      def using_session(id, &block) = with_attributes({'session.id' => id}, &block)
      def using_user(id, &block) = with_attributes({'user.id' => id}, &block)
      def using_metadata(value)
        init if @pending && !waiting_for_bundle?
        return yield unless active? && @attributes_key
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
      def install_exit_hooks
        return if @exit_hooks
        @exit_hooks = true
        at_exit { shutdown(timeout: 5) }
        @previous_term = Signal.trap('TERM') do
          unless @handling_term
            @handling_term = true
            begin
              flush(timeout: 5)
            ensure
              previous = @previous_term
              if previous.respond_to?(:call)
                @handling_term = false
                previous.call(Signal.list['TERM'])
              elsif previous == 'IGNORE' || previous == 'SIG_IGN'
                @handling_term = false
              elsif previous == 'EXIT'
                exit
              else
                Signal.trap('TERM', 'DEFAULT')
                Process.kill('TERM', Process.pid)
              end
            end
          end
        end
      rescue ArgumentError, ThreadError => e
        debug(e)
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
        # Explicit init also needs lazy class observation. No namespace resolution.
        @target_hook ||= TracePoint.new(:end) { |event| observe_target(event.self) }
        @target_hook.enable unless @target_hook.enabled?
      end
      def install_server_instrumentation(force: false)
        return if @rack_status == 'instrumented'
        return unless (force || @config.enabled?('rack')) && Gem.loaded_specs.key?('rack') && Gem.loaded_specs.key?('opentelemetry-instrumentation-rack')
        require 'opentelemetry/instrumentation/rack'
        @rack_status = OpenTelemetry::Instrumentation::Rack::Instrumentation.instance.install ? 'instrumented' : 'failed'
      rescue LoadError, SyntaxError, StandardError => e
        @rack_status = 'failed'; debug(e)
      end
      def report(test_export: false)
        init if @pending && !waiting_for_bundle?
        integrations = {}
        {'wire' => nil, 'net_http' => 'net-http', 'httpx' => 'httpx', 'openai' => 'openai', 'ruby-openai' => 'ruby-openai',
         'anthropic' => 'anthropic', 'anthropic-sdk-ruby' => 'anthropic-sdk-ruby', 'ruby_llm' => 'ruby_llm', 'roast' => 'roast', 'rack' => 'rack'}.each do |name, gem|
          allowed = %w[net_http httpx].include?(name) ? transport_enabled?(name) : (name == 'wire' ? wire_enabled? : @config&.enabled?(name))
          allowed = true if name == 'rack' && @rack_status == 'instrumented'
          state = if !active? || !allowed || (%w[wire net_http httpx].include?(name) && !wire_enabled?)
                    'disabled'
                  elsif name == 'wire' || name == 'net_http' then 'instrumented'
                  elsif !installed?(gem) then 'not_installed'
                  elsif name == 'httpx' then @httpx_installed ? 'instrumented' : 'pending'
                  elsif name == 'rack' && @rack_status then @rack_status
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
        end
        {version: VERSION, active: active?, integrations: integrations, config: @config&.masked, collector: collector,
         projection_zero_limit_spans: defined?(AttributeProjection) ? AttributeProjection.zero_limit_spans : 0}
      end
    end
    # No requires and no inspection of application request/body objects here.
    # Redispatch only when initialization installed the real wrapper above us;
    # otherwise super preserves the native API, including block absence.
    module LazyNetHTTP
      # An explicit #start can send authenticated CONNECT before #request exists.
      # Initialize without requiring dependencies, then redispatch to its observer.
      def connect(...)
        return connect(...) if Auto.start_on_request
        super
      end
      private :connect
      def request(...)
        return super unless Auto.pending?
        timing = [Time.now, Process.clock_gettime(Process::CLOCK_MONOTONIC)]
        previous = Thread.current[:traceai_lazy_timing]
        Thread.current[:traceai_lazy_timing] = timing
        return request(...) if Auto.start_on_request
        super
      ensure
        Thread.current[:traceai_lazy_timing] = previous if timing
      end
    end
    module LazySDKRequest
      def execute(...)
        return execute(...) if Auto.start_on_request
        super
      end
    end
    module LazyHTTPX
      def request(...)
        Auto.start_on_request
        super
      end
    end
    module LazyHTTPXStream
      def each(&block)
        return super unless block
        return each(&block) if Auto.start_on_request
        super
      end
      private
      def response(...)
        return response(...) if Auto.start_on_request
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
      def export(spans, timeout: nil)
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
