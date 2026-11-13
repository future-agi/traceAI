# frozen_string_literal: true
require_relative 'test_regressions'
require_relative 'test_safety'
require_relative 'test_wire'
require 'tmpdir'

class EndToEndTest
  def test_net_http_buffered_retry_and_exhaustion_are_identical_to_baseline
    recovered = JSON.generate(id: 'recovered-id', status: 'completed', text: 'recovered')
    [1, 2].each do |failures|
      [false, true].each do |enabled|
        attempts = 0
        @provider.raw do |io, _|
          attempts += 1
          if attempts <= failures
            io.write "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            chunk = '{"id":"failed-attempt","choices":['
            io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\n"; io.flush
          else
            io.write "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: #{recovered.bytesize}\r\nConnection: close\r\n\r\n#{recovered}"
          end
        end
        code = <<~RUBY
          require 'net/http'; require 'json'
          http = Net::HTTP.new('api.assemblyai.com', #{@provider.port}, nil)
          http.ipaddr = '127.0.0.1'
          http.max_retries = 1
          begin
            response = http.request(Net::HTTP::Get.new('/v1/chat/completions'))
            print response.body
          rescue => e
            puts "\#{e.class.name}:\#{e.message}"
          end
        RUBY
        # AssemblyAI polling is idempotent; retaining the failed attempt's bytes
        # would invalidate the completed-only response and drop the span.
        code = code.sub('/v1/chat/completions', '/v2/transcript/abc123')
        stdout, stderr, status = app(code, enabled ? {} : {'RUBYOPT' => nil})
        assert status.success?, stderr
        assert_equal 2, attempts
        assert_equal(failures == 1 ? recovered : "EOFError:end of file reached\n", stdout)
        spans = export_spans
        if enabled && failures == 1
          assert_equal 1, spans.length
          span = spans.first.first; a = attrs(span.attributes)
          assert_equal :STATUS_CODE_OK, span.status.code
          assert_equal 'recovered', a['output.value']
          assert_equal 'recovered-id', a['gen_ai.response.id']
          refute a['error.type']; refute a['traceai.wire.incomplete']
          refute_includes JSON.generate(a), 'failed-attempt'
        else
          assert_empty spans # completed-only polling drops unfinished responses.
        end
      end
    end
  end

  def test_net_http_no_block_preserves_request_block_presence_and_get_body_identity
    code = <<~RUBY
      require 'net/http'
      module ObserveNativeBlock
        def transport_request(req, &block)
          abort 'agent invented a response block' if block
          super
        end
        private :transport_request
      end
      Net::HTTP.prepend(ObserveNativeBlock)
      http = Net::HTTP.new('127.0.0.1', #{@provider.port}, nil)
      req = Net::HTTP::Post.new('/v1/chat/completions')
      req['content-type'] = 'application/json'
      req.body = #{JSON.generate(model: 'm', messages: []).inspect}
      response = http.request(req)
      abort 'cached body identity changed' unless response.body.equal?(response.read_body)
      print response.body
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal CHAT, stdout
    assert_equal 1, export_spans.length
  end

  def test_net_http_nested_response_block_request_does_not_overwrite_outer_capture
    @provider.stop
    @provider = MockHTTP.new do |request|
      body = JSON.parse(request[:body])
      nested = JSON.generate(id: 'nested-id', choices: [{message: {role: 'assistant', content: 'nested response'}}])
      [200, {'Content-Type' => 'application/json'}, body['model'] == 'nested-model' ? nested : CHAT]
    end
    code = <<~RUBY
      require 'net/http'; require 'json'
      outer = Net::HTTP::Post.new('/v1/chat/completions')
      outer['content-type'] = 'application/json'
      outer.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: 'outer'}])
      Net::HTTP.start('127.0.0.1', #{@provider.port}) do |http|
        http.request(outer) do |response|
          print response.body
          inner = Net::HTTP::Post.new('/v1/chat/completions')
          inner['content-type'] = 'application/json'
          inner.body = JSON.generate(model: 'nested-model', messages: [])
          nested = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(inner) }
          print nested.body
        end
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    assert_includes stdout, 'nested response'
    assert_includes stdout, CHAT
    a = assert_chat_span(export_spans)
    assert_equal 'Hello from Ruby', a['output.value']
    assert_equal 'chat-1', a['gen_ai.response.id']
    refute_includes JSON.generate(a), 'nested response'
  end

  def test_enabled_preload_activates_no_gems_before_application_bundler_setup
    gemfile = Tempfile.new(['enabled-json-263', '.Gemfile'], File.join(ROOT, '.bundle/tmp'))
    gemfile.write("source 'https://rubygems.org'\ngem 'json', '2.6.3'\n"); gemfile.close
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('BUNDLE_GEMFILE' => gemfile.path)
    baseline, stderr, status = app("puts Gem.loaded_specs.keys.sort.join(','); require 'bundler/setup'; require 'json'; puts 'APPLICATION_RAN ' + JSON::VERSION", overrides.merge('RUBYOPT' => nil))
    assert status.success?, stderr
    baseline_specs, baseline = baseline.split("\n", 2)
    assert_equal "APPLICATION_RAN 2.6.3\n", baseline
    code = <<~RUBY
      abort 'JSON loaded before application Bundler.setup' if defined?(JSON)
      # RUBYOPT injection must not activate ANY additional gems, not just JSON.
      before = Gem.loaded_specs.keys.sort.join(',')
      abort "preloaded gems: \#{before.inspect}" unless before == #{baseline_specs.inspect}
      require 'bundler/setup'
      require 'json'
      puts 'APPLICATION_RAN ' + JSON::VERSION
    RUBY
    stdout, stderr, status = app(code, overrides)
    assert status.success?, stderr
    assert_equal baseline, stdout
    assert_operator stderr.scan('traceai-auto:').length, :<=, 1
    refute_includes stderr, 'already activated'
    assert_empty export_spans
  ensure
    File.unlink(gemfile.path + '.lock') if gemfile && File.exist?(gemfile.path + '.lock')
    gemfile&.unlink
  end

  def test_enabled_pinned_json_bundle_can_still_trace_after_setup
    gemfile = Tempfile.new(['enabled-json-agent', '.Gemfile'], File.join(ROOT, '.bundle/tmp'))
    gemfile.write("source 'https://rubygems.org'\ngem 'json', '2.6.3'\ngem 'traceai-auto', path: #{ROOT.inspect}\n"); gemfile.close
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }.merge('BUNDLE_GEMFILE' => gemfile.path)
    stdout, stderr, status = app("abort 'early JSON' if defined?(JSON)\nrequire 'bundler/setup'\nrequire 'json'\nputs JSON::VERSION\n" + plain_code, overrides)
    assert status.success?, stderr
    assert_equal "2.6.3\n" + CHAT, stdout
    assert_chat_span(export_spans)
  ensure
    File.unlink(gemfile.path + '.lock') if gemfile && File.exist?(gemfile.path + '.lock')
    gemfile&.unlink
  end

  def test_default_gemfile_and_manual_bundler_setup_do_not_preactivate_agent_dependencies
    Dir.mktmpdir('default-json-bundle', File.join(ROOT, '.bundle/tmp')) do |directory|
      File.write(File.join(directory, 'Gemfile'), "source 'https://rubygems.org'\ngem 'json', '2.6.3'\n")
      overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
      ["require 'bundler/setup'", "require 'bundler'; Bundler.setup"].each do |setup|
        before = "require 'net/http'; puts Gem.loaded_specs.keys.sort.join(',')"
        baseline, stderr, status = app(before + "; #{setup}; require 'json'; puts JSON::VERSION", overrides.merge('RUBYOPT' => nil), chdir: directory)
        assert status.success?, stderr
        baseline_specs, output = baseline.split("\n", 2)
        assert_equal "2.6.3\n", output
        code = <<~RUBY
          require 'net/http'
          abort 'agent activated gems before default Gemfile setup' unless Gem.loaded_specs.keys.sort.join(',') == #{baseline_specs.inspect}
          abort 'agent activated JSON before default Gemfile setup' if defined?(JSON)
          #{setup}
          require 'json'; puts JSON::VERSION
        RUBY
        stdout, stderr, status = app(code, overrides, chdir: directory)
        assert status.success?, stderr; assert_equal output, stdout
        refute_includes stderr, 'already activated'
      end
    end
    assert_empty export_spans
  end

  def test_standalone_injection_is_lazy_and_traces_net_http_and_official_openai_without_bundler
    overrides = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
    prefix = <<~RUBY
      abort 'JSON preactivated' if defined?(JSON)
      abort 'OpenTelemetry preactivated' if defined?(OpenTelemetry)
      abort 'Bundler preactivated' if defined?(Bundler)
    RUBY
    [plain_code, official_client_code + "print c.chat.completions.create(model: 'gpt-4o-mini', messages: []).choices.first.message.content"].each_with_index do |code, index|
      stdout, stderr, status = app(prefix + code, overrides, chdir: File.dirname(ROOT))
      assert status.success?, stderr
      assert_equal(index.zero? ? CHAT : 'Hello from Ruby', stdout)
      assert_chat_span(export_spans)
    end
  end

  def official_client_code
    <<~RUBY
      gem 'openai', '0.75.0'
      require File.join(Gem::Specification.find_by_name('openai').full_gem_path, 'lib/openai.rb')
      c = OpenAI::Client.new(api_key: 'mock-key', base_url: '#{@provider.url}/v1', max_retries: 0)
    RUBY
  end

  def test_official_openai_fiber_honors_suppression_dedup_and_all_context_attributes
    [false, true].each do |streamed|
      @provider.stop
      @provider = MockHTTP.new { |_r| [200, {'Content-Type' => streamed ? 'text/event-stream' : 'application/json'}, streamed ? STREAM : CHAT] }
      invocation = streamed ? "c.chat.completions.stream(model: 'gpt-4o-mini', messages: []).each { |e| print e.chunk.choices.first&.delta&.content if e.type == :chunk }" : "print c.chat.completions.create(model: 'gpt-4o-mini', messages: []).choices.first.message.content"
      code = official_client_code + <<~RUBY
        def call_sdk(c)
          #{invocation}
        end
        t = OpenTelemetry.tracer_provider.tracer('app')
        OpenTelemetry::Common::Utilities.untraced { call_sdk(c) }
        %w[LLM EMBEDDING RERANKER GUARDRAIL].each do |kind|
          t.in_span('dedicated-' + kind, attributes: {'gen_ai.span.kind' => kind}) { call_sdk(c) }
        end
        %w[CHAIN AGENT TOOL].each do |kind|
          t.in_span('framework-' + kind, attributes: {'gen_ai.span.kind' => kind}) do
            TraceAI::Auto.using_session('s-' + kind) do
              TraceAI::Auto.using_user('u-' + kind) do
                TraceAI::Auto.using_metadata(tenant: kind) { call_sdk(c) }
              end
            end
          end
        end
        abort 'SDK context leaked' if Thread.current.thread_variable_get(:traceai_sdk_context)
        abort 'caller suppression leaked' if OpenTelemetry::Common::Utilities.untraced?(OpenTelemetry::Context.current)
        puts 'PASSTHROUGH'
      RUBY
      stdout, stderr, status = app(code)
      assert status.success?, stderr
      assert_equal((streamed ? 'one two' : 'Hello from Ruby') * 8 + "PASSTHROUGH\n", stdout)
      spans = export_spans.map(&:first)
      wire = spans.select { |span| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
      assert_equal 3, wire.length
      %w[CHAIN AGENT TOOL].each do |kind|
        parent = spans.find { |span| span.name == 'framework-' + kind }; refute_nil parent
        child = wire.find { |span| span.parent_span_id == parent.span_id }; refute_nil child
        a = attrs(child.attributes)
        assert_equal '<redacted>', a['session.id'] # S6 session-name rule
        assert_equal 'u-' + kind, a['user.id']
        assert_equal({'tenant' => kind}, JSON.parse(a['metadata']))
        assert_equal(streamed ? 'one two' : 'Hello from Ruby', a['output.value'])
      end
    end
  end

  def test_official_openai_stream_keeps_original_context_after_callers_scope_exits
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    code = official_client_code + <<~RUBY
      t = OpenTelemetry.tracer_provider.tracer('app')
      stream = nil
      t.in_span('original-parent', attributes: {'gen_ai.span.kind' => 'CHAIN'}) do
        TraceAI::Auto.using_session('original-session') do
          stream = c.chat.completions.stream(model: 'gpt-4o-mini', messages: [])
        end
      end
      TraceAI::Auto.using_session('different-session') do
        stream.each { |e| print e.chunk.choices.first&.delta&.content if e.type == :chunk }
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal 'one two', stdout
    spans = export_spans.map(&:first)
    parent = spans.find { |s| s.name == 'original-parent' }
    wire = spans.find { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }; refute_nil wire
    assert_equal parent.span_id, wire.parent_span_id
    assert_equal '<redacted>', attrs(wire.attributes)['session.id'] # Parent and user/metadata still prove context propagation.
  end

  def test_net_http_and_httpx_consumer_exceptions_preserve_identity_and_are_incomplete_not_error
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    %w[ArgumentError IOError Timeout::Error].each do |type|
      %w[net_http net_destination httpx].each do |transport|
        consumer = if transport == 'httpx'
                     <<~RUBY
                       require 'httpx'
                       HTTPX.plugin(:stream).post('#{@provider.url}/v1/chat/completions', json: {model: 'm', messages: []}, stream: true).each { |chunk| print chunk; raise failure }
                     RUBY
                   elsif transport == 'net_destination'
                     plain_code(stream: true).sub('r.read_body { |s| STDOUT.write(s) }', 'r.read_body(destination)')
                   else
                     plain_code(stream: true).sub('STDOUT.write(s)', 'STDOUT.write(s); raise failure')
                   end
        code = <<~RUBY
          require 'timeout'
          failure = #{type}.new('consumer abort')
          destination = Object.new
          destination.define_singleton_method(:<<) { |chunk| print chunk; raise failure }
          begin
            #{consumer}
            abort 'consumer did not raise'
          rescue #{type} => e
            abort 'exception identity changed' unless e.equal?(failure)
            abort 'consumer backtrace lost' unless e.backtrace.first.include?('-e:')
            puts 'CONSUMER_ABORT'
          end
        RUBY
        stdout, stderr, status = app(code)
        assert status.success?, "#{transport}/#{type}: #{stderr}"
        assert_includes stdout, STREAM.first
        assert_includes stdout, 'CONSUMER_ABORT'
        spans = export_spans; assert_equal 1, spans.length, "#{transport}/#{type}"
        span = spans.first.first; a = attrs(span.attributes)
        assert_equal :STATUS_CODE_OK, span.status.code, "#{transport}/#{type}"
        assert_equal true, a['traceai.wire.incomplete']
        refute a['error.type']
        assert_includes a['output.value'], 'one'
      end
    end
  end

  def test_disabled_helpers_are_dependency_free_noops_and_preserve_block_results_and_errors
    [{'FI_API_KEY' => '', 'FI_SECRET_KEY' => ''}, {'OTEL_SDK_DISABLED' => 'true'}].each do |settings|
      code = <<~RUBY
        abort 'JSON loaded' if defined?(JSON)
        value = Object.new
        def value.to_json(*); abort 'metadata serialized while disabled'; end
        sentinel = Object.new
        result = TraceAI::Auto.using_session('s') do
          TraceAI::Auto.using_user('u') { TraceAI::Auto.using_metadata(value) { sentinel } }
        end
        abort 'block result changed' unless result.equal?(sentinel)
        failure = IOError.new('app error')
        begin
          TraceAI::Auto.using_metadata(value) { raise failure }
        rescue IOError => e
          abort 'exception changed' unless e.equal?(failure)
        end
        abort 'helper loaded JSON' if defined?(JSON)
        puts 'APPLICATION_RAN'
      RUBY
      stdout, stderr, status = app(code, settings)
      assert status.success?, stderr; assert_equal "APPLICATION_RAN\n", stdout
      assert_operator stderr.scan('traceai-auto:').length, :<=, 1
      assert_empty export_spans
    end
  end
end

class SafetyTest
  def test_cohere_v2_sse_content_id_finish_reason_and_tokens
    req = request('/v2/chat', 'api.cohere.com', {model: 'command-r', messages: [{role: 'user', content: 'hi'}], stream: true})
    events = [
      {type: 'message-start', delta: {message: {id: 'cohere-id', role: 'assistant'}}},
      {type: 'content-start', index: 0, delta: {message: {content: {type: 'text', text: ''}}}},
      {type: 'content-delta', index: 0, delta: {message: {content: {text: 'hello '}}}},
      {type: 'content-delta', index: 0, delta: {message: {content: {text: 'world'}}}},
      {type: 'content-end', index: 0},
      {type: 'message-end', delta: {finish_reason: 'COMPLETE', usage: {billed_units: {input_tokens: 20, output_tokens: 30}, tokens: {input_tokens: 2, output_tokens: 3}}}}
    ]
    body = events.map { |event| "event: #{event[:type]}\r\ndata: #{JSON.generate(event)}\r\n\r\n" }.join
    result = W.build(req, response(body, 'text/event-stream'))
    assert result.ok
    a = result.attributes
    assert_equal 'hello world', a['output.value']
    assert_equal 'hello world', a['gen_ai.output.messages.0.message.content']
    assert_equal 'assistant', a['gen_ai.output.messages.0.message.role']
    assert_equal 'cohere-id', a['gen_ai.response.id']
    assert_equal ['COMPLETE'], a['gen_ai.response.finish_reasons']
    assert_equal 2, a['gen_ai.usage.input_tokens']
    assert_equal 3, a['gen_ai.usage.output_tokens']
    assert_equal 5, a['gen_ai.usage.total_tokens']
    assert_equal true, a['gen_ai.request.stream']
    partial = W.build(req, response(events.first(4).map { |e| "data: #{JSON.generate(e)}\n\n" }.join, 'text/event-stream').merge(incomplete: true))
    assert_equal 'hello world', partial.attributes['output.value']
    assert_equal 'cohere-id', partial.attributes['gen_ai.response.id']
    assert_equal true, partial.attributes['traceai.wire.incomplete']
    refute partial.attributes['gen_ai.usage.total_tokens']
  end

  def test_gemini_joins_parts_with_newline_but_stream_chunks_without_separator
    req = request('/v1beta/models/gemini-test:generateContent', 'generativelanguage.googleapis.com',
                  {systemInstruction: {parts: [{text: 'system one'}, {text: 'system two'}]}, contents: [{role: 'user', parts: [{text: 'alpha'}, {inlineData: {data: 'AAAA'}}, {text: 'beta'}]}]})
    chunk = {candidates: [{content: {role: 'model', parts: [{text: 'one'}, {text: 'hidden thought', thought: true}, {text: 'two'}]}}]}
    a = W.build(req, response(chunk)).attributes
    assert_equal "system one\nsystem two", a['gen_ai.input.messages.0.message.content']
    assert_equal "alpha\nbeta", a['gen_ai.input.messages.1.message.content']
    assert_equal "one\ntwo", a['output.value']
    assert_equal "one\ntwo", a['gen_ai.output.messages.0.message.content']
    last = {candidates: [{content: {role: 'model', parts: [{text: ' three'}, {text: 'four'}]}, finishReason: 'STOP'}]}
    [response([chunk, last]), response([chunk, last].map { |c| "data: #{JSON.generate(c)}\n\n" }.join, 'text/event-stream')].each do |resp|
      attrs = W.build(req, resp).attributes
      assert_equal "one\ntwo three\nfour", attrs['output.value']
      assert_equal ['STOP'], attrs['gen_ai.response.finish_reasons']
    end
    fixture = Dir[File.join(FixtureHarness::SPEC, 'fixtures/18-*.json')].first
    refute_nil fixture
    result = FixtureHarness.run(JSON.parse(File.read(fixture)))
    assert_equal 'Why did the chicken cross?', result.attributes['output.value']
  end
end
