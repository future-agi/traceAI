# frozen_string_literal: true
require_relative 'test_helper'

# RubyLLM runs tools inside Chat#ask. The agent gives each tool execution an
# OTel GenAI execute_tool span, in the same trace as the wire chat spans of that
# ask, without any application tracing code.
#
# Each case runs against every RubyLLM version in RUBY_LLM_VERSIONS (comma
# separated; default: the version this bundle selected), in three setups:
#   bundled     bundle exec with this repository's Gemfile (SDK pipeline)
#   standalone  no Bundler, no OpenTelemetry SDK (dependency-free pipeline)
#   app_sdk     no Bundler, the application configures its own SDK provider
# The bundled setup can only use the locked version.
class RubyLLMToolSpansTest < Minitest::Test
  include ChildHelper

  ANSWER = 'It is sunny.'
  ARGUMENTS = '{"city":"Paris"}'
  RESULT_MARKER = 'tool-result-sunny-in-Paris'

  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    # One tool call first; the final answer once the request carries the tool
    # result. The tool name comes from the tools the request declares.
    @provider = MockHTTP.new do |request|
      body = JSON.parse(request[:body]) rescue {}
      messages = body['messages'] || []
      payload = if (messages.last || {})['role'] != 'user' || (body['tools'] || []).empty?
                  {id: 'chat-2', object: 'chat.completion', model: 'gpt-4o-mini',
                   choices: [{index: 0, message: {role: 'assistant', content: ANSWER}, finish_reason: 'stop'}],
                   usage: {prompt_tokens: 9, completion_tokens: 4, total_tokens: 13}}
                else
                  name = body['tools'].first.dig('function', 'name')
                  {id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini',
                   choices: [{index: 0, finish_reason: 'tool_calls',
                              message: {role: 'assistant', content: nil,
                                        tool_calls: [{id: "call_#{messages.count { |m| m['role'] == 'user' }}", type: 'function', function: {name: name, arguments: ARGUMENTS}}]}}],
                   usage: {prompt_tokens: 7, completion_tokens: 5, total_tokens: 12}}
                end
      [200, {'Content-Type' => 'application/json'}, JSON.generate(payload)]
    end
  end

  def teardown
    @provider.stop; @collector.stop
  end

  def versions
    listed = ENV['RUBY_LLM_VERSIONS'].to_s.split(',').map(&:strip).reject(&:empty?)
    return listed unless listed.empty?
    [Gem.loaded_specs['ruby_llm']&.version&.to_s || Gem::Specification.find_by_name('ruby_llm').version.to_s]
  end

  # [label, code prefix, env overrides, chdir] for every setup and version.
  def setups
    standalone = ENV.keys.grep(/\ABUNDL/).to_h { |key| [key, nil] }
    outside = File.dirname(ROOT)
    list = []
    locked = Gem.loaded_specs['ruby_llm']&.version&.to_s
    list << ['bundled', '', {}, ROOT] if locked && versions.include?(locked)
    versions.each do |version|
      pin = "gem 'ruby_llm', '#{version}'\n"
      list << ["standalone #{version}", pin, standalone, outside]
      list << ["app_sdk #{version}", pin + app_sdk, standalone, outside]
    end
    list
  end

  # Version-neutral RubyLLM application code: 1.x declares parameters with
  # `param`, 2.x infers them from #execute; only 2.x has a protocol option.
  def chat_code(tools: true, raise_error: false, report: false, concurrency: false, before: '', body: "puts chat.ask('What is the weather in Paris?').content")
    <<~RUBY
      require 'ruby_llm'
      class Weather < RubyLLM::Tool
        class Down < StandardError; end
        description 'Current weather for a city'
        param :city, desc: 'City name' if respond_to?(:param)
        def execute(city:)
          #{concurrency ? "puts 'TOOL_THREAD ' + (Thread.current == Thread.main ? 'main' : 'worker')" : ''}
          #{raise_error ? "raise Down, 'weather backend down for ' + city" : "'#{RESULT_MARKER.sub('Paris', "' + city + '")}'"}
        end
      end
      RubyLLM.configure { |c| c.openai_api_key = 'mock-key'; c.openai_api_base = '#{@provider.url}/v1'; c.logger = Logger.new(IO::NULL) if c.respond_to?(:logger=) }
      #{concurrency ? "RubyLLM.configure { |c| c.respond_to?(:tool_concurrency=) ? c.tool_concurrency = :threads : puts('NO_TOOL_CONCURRENCY') }" : ''}
      #{before}
      options = {model: 'gpt-4o-mini', provider: :openai, assume_model_exists: true}
      options[:protocol] = :chat_completions if RubyLLM::Chat.instance_method(:initialize).parameters.any? { |_, name| name == :protocol }
      chat = RubyLLM.chat(**options)
      #{tools ? 'chat = chat.respond_to?(:with_tools) ? chat.with_tools(Weather) : chat.with_tool(Weather)' : ''}
      begin
        #{body}
      rescue Weather::Down => e
        puts "RAISED \#{e.class} \#{e.message}"
      end
      #{report ? "puts 'RUBY_LLM_STATUS ' + TraceAI::Auto.report.dig(:integrations, 'ruby_llm', :status).to_s" : ''}
    RUBY
  end

  def run_chat(label, prefix, env, chdir, **options)
    stdout, stderr, status = app(prefix + chat_code(**options), env, chdir: chdir)
    assert status.success?, "#{label}: #{stderr}"
    # RubyLLM 1.x logs its model warning to stdout through its own logger.
    [stdout.lines.grep_v(/\A[DIWE], \[/).join, export_spans.map(&:first)]
  end

  def hex(bytes) = bytes.unpack1('H*')

  def wire_spans(spans) = spans.select { |s| attrs(s.attributes)['traceai.capture.layer'] == 'wire' }
  def tool_spans(spans) = spans.select { |s| s.name.start_with?('execute_tool') }

  def test_tool_execution_has_execute_tool_span_in_the_ask_trace
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir)
      assert_equal "#{ANSWER}\n", stdout, label
      tools = tool_spans(spans)
      assert_equal 1, tools.length, "#{label}: #{spans.map(&:name)}"
      tool = tools.first
      a = attrs(tool.attributes)
      assert_equal 'execute_tool weather', tool.name, label
      assert_equal :SPAN_KIND_INTERNAL, tool.kind, label
      assert_equal 'execute_tool', a['gen_ai.operation.name'], label
      assert_equal 'weather', a['gen_ai.tool.name'], label
      assert_equal 'call_1', a['gen_ai.tool.call.id'], label
      assert_equal 'function', a['gen_ai.tool.type'], label
      assert_equal 'TOOL', a['gen_ai.span.kind'], label
      assert_equal ARGUMENTS, a['gen_ai.tool.call.arguments'], label
      assert_equal RESULT_MARKER, a['gen_ai.tool.call.result'], label
      refute_equal :STATUS_CODE_ERROR, tool.status.code, label
      wire = wire_spans(spans)
      assert_equal 2, wire.length, "#{label}: #{spans.map(&:name)}"
      assert(wire.all? { |s| s.name == 'chat gpt-4o-mini' }, label)
      # One trace for the whole ask: the chat spans and the tool span share it.
      assert_equal [hex(tool.trace_id)], (wire + [tool]).map { |s| hex(s.trace_id) }.uniq, label
      refute_equal '0' * 32, hex(tool.trace_id), label
      # Tool spans and chat spans hang off the same parent, which is exported.
      parent_ids = (wire + [tool]).map { |s| hex(s.parent_span_id) }.uniq
      assert_equal 1, parent_ids.length, label
      parent = spans.find { |s| hex(s.span_id) == parent_ids.first }
      assert parent, "#{label}: parent span not exported"
      assert_equal :SPAN_KIND_INTERNAL, parent.kind, label
      assert_equal [hex(tool.trace_id)], spans.map { |s| hex(s.trace_id) }.uniq, label
      assert tool.start_time_unix_nano >= wire.map(&:end_time_unix_nano).min, label
    end
  end

  # RubyLLM 1.16+ can run tools on worker threads, which start without the
  # caller's context; their spans still belong to the ask.
  def test_concurrent_tools_stay_in_the_ask_trace
    ran = 0
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir, concurrency: true)
      next if stdout.include?('NO_TOOL_CONCURRENCY')
      ran += 1
      assert_includes stdout, "TOOL_THREAD worker\n", label
      assert_includes stdout, "#{ANSWER}\n", label
      tool = tool_spans(spans).first
      assert tool, "#{label}: #{spans.map(&:name)}"
      wire = wire_spans(spans)
      assert_equal 2, wire.length, label
      assert_equal [hex(tool.trace_id)], (wire + [tool]).map { |s| hex(s.trace_id) }.uniq, label
      assert_equal [hex(tool.parent_span_id)], wire.map { |s| hex(s.parent_span_id) }.uniq, label
    end
    skip 'no RubyLLM version under test runs tools concurrently' if ran.zero?
  end

  def test_raising_tool_marks_the_span_and_the_exception_propagates
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir, raise_error: true)
      assert_equal "RAISED Weather::Down weather backend down for Paris\n", stdout, label
      tools = tool_spans(spans)
      assert_equal 1, tools.length, "#{label}: #{spans.map(&:name)}"
      tool = tools.first
      assert_equal :STATUS_CODE_ERROR, tool.status.code, label
      assert_equal 'Weather::Down', attrs(tool.attributes)['error.type'], label
      refute attrs(tool.attributes).key?('gen_ai.tool.call.result'), label
      wire = wire_spans(spans)
      assert_equal 1, wire.length, label
      assert_equal hex(tool.trace_id), hex(wire.first.trace_id), label
    end
  end

  CONTENT_SWITCHES = %w[FI_HIDE_INPUTS FI_HIDE_OUTPUTS FI_HIDE_INPUT_TEXT FI_HIDE_OUTPUT_TEXT FI_HIDE_INPUT_MESSAGES FI_HIDE_OUTPUT_MESSAGES].freeze
  def test_capture_off_exports_no_raw_tool_arguments_or_results
    setups.each do |setup_label, prefix, env, chdir|
      [[{'FI_HIDE_INPUTS' => 'true', 'FI_HIDE_OUTPUTS' => 'true'}, false], [{'FI_HIDE_INPUTS' => 'true', 'FI_HIDE_OUTPUTS' => 'true'}, true],
       *CONTENT_SWITCHES.map { |switch| [{switch => 'true'}, false] }].each do |hidden, raise_error|
        label = "#{setup_label} #{hidden.keys.join('+')}"
        stdout, spans = run_chat(label, prefix, env.merge(hidden), chdir, raise_error: raise_error)
        assert_includes stdout, raise_error ? 'RAISED' : ANSWER, label
        tools = tool_spans(spans)
        assert_equal 1, tools.length, "#{label}: #{spans.map(&:name)}"
        tool = tools.first
        a = attrs(tool.attributes)
        assert_equal 'execute_tool', a['gen_ai.operation.name'], label
        assert_equal 'execute_tool weather', tool.name, label
        # The call's identity is not content and stays visible.
        assert_equal ['weather', 'call_1', 'function'], a.values_at('gen_ai.tool.name', 'gen_ai.tool.call.id', 'gen_ai.tool.type'), label
        values = a.values.flatten.map(&:to_s) + [tool.status.message.to_s]
        %w[Paris tool-result-sunny backend\ down].each do |raw|
          assert(values.none? { |value| value.include?(raw) }, "#{label}: #{raw} exported in #{a}")
        end
        assert_equal 'Weather::Down', a['error.type'], label if raise_error
      end
    end
  end

  # Context helpers entered before the agent started apply to the new spans
  # too, as they do to the wire spans of the same ask.
  def test_context_helpers_apply_to_invoke_agent_and_tool_spans
    body = "TraceAI::Auto.using_user('alice') { TraceAI::Auto.using_session('s-1') { puts chat.ask('What is the weather in Paris?').content } }"
    setups.each do |label, prefix, env, chdir|
      _, spans = run_chat(label, prefix, env, chdir, body: body)
      assert_equal 4, spans.length, "#{label}: #{spans.map(&:name)}"
      spans.each do |span|
        assert_equal %w[alice s-1], attrs(span.attributes).values_at('user.id', 'session.id'), "#{label}: #{span.name}"
      end
    end
  end

  # A tool run after an ask has finished (RubyLLM 2.x Chat#run_tools, #step)
  # never joins that ask's trace or takes its helper attributes.
  def test_tools_run_after_an_ask_do_not_join_the_finished_ask
    body = <<~CODE
      TraceAI::Auto.using_user('alice') { puts chat.ask('What is the weather in Paris?').content }
      if chat.respond_to?(:run_tools) && chat.respond_to?(:ask_later)
        TraceAI::Auto.using_user('bob') { chat.ask_later('And now?'); chat.generate; chat.run_tools }
      else
        puts 'NO_STEP_API'
      end
    CODE
    ran = 0
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir, body: body)
      next if stdout.include?('NO_STEP_API')
      ran += 1
      first = spans.find { |s| s.name == 'invoke_agent' }
      assert first, label
      tools = tool_spans(spans)
      assert_equal 2, tools.length, "#{label}: #{spans.map(&:name)}"
      late = tools.find { |s| attrs(s.attributes)['user.id'] == 'bob' }
      assert late, "#{label}: #{tools.map { |s| attrs(s.attributes)['user.id'] }}"
      refute_equal hex(first.trace_id), hex(late.trace_id), label
      refute_equal hex(first.span_id), hex(late.parent_span_id), label
    end
    skip 'no RubyLLM version under test has run_tools' if ran.zero?
  end

  # A value over the wire body limit is left out, never cut; the span keeps
  # its identity and says it is incomplete.
  def test_oversized_tool_result_is_left_out_and_the_span_keeps_its_identity
    setups.each do |label, prefix, env, chdir|
      _, spans = run_chat(label, prefix, env.merge('FI_WIRE_MAX_BODY_BYTES' => '40'), chdir)
      tool = tool_spans(spans).first
      assert tool, "#{label}: #{spans.map(&:name)}"
      a = attrs(tool.attributes)
      assert_equal ['weather', 'call_1', ARGUMENTS], a.values_at('gen_ai.tool.name', 'gen_ai.tool.call.id', 'gen_ai.tool.call.arguments'), label
      refute a.key?('gen_ai.tool.call.result'), label
      refute a.key?('output.value'), label
      assert_equal true, a['traceai.wire.truncated'], label
    end
  end

  # RubyLLM::Chat is often first used long after startup, once the agent's
  # startup hooks have closed. Its tools are still traced.
  def test_chat_first_used_after_the_startup_window_is_instrumented
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir, report: true, before: 'TraceAI::Auto.close_startup_window if defined?(TraceAI::Auto)')
      assert_includes stdout, "RUBY_LLM_STATUS instrumented\n", label
      assert_equal 1, tool_spans(spans).length, "#{label}: #{spans.map(&:name)}"
    end
  end

  def test_report_shows_ruby_llm_instrumented
    setups.each do |label, prefix, env, chdir|
      stdout, = run_chat(label, prefix, env, chdir, report: true)
      assert_includes stdout, "RUBY_LLM_STATUS instrumented\n", label
    end
  end

  # CONTROL: a chat without tools keeps exactly its one wire chat span, a root,
  # as before; tool spans are added, never chat spans.
  def test_control_chat_without_tools_keeps_its_single_wire_span
    setups.each do |label, prefix, env, chdir|
      stdout, spans = run_chat(label, prefix, env, chdir, tools: false)
      assert_equal "#{ANSWER}\n", stdout, label
      assert_equal ['chat gpt-4o-mini'], spans.map(&:name), label
      span = spans.first
      a = attrs(span.attributes)
      assert_equal :SPAN_KIND_CLIENT, span.kind, label
      assert_equal 'wire', a['traceai.capture.layer'], label
      assert_equal ANSWER, a['output.value'], label
      assert_equal 13, a['gen_ai.usage.total_tokens'], label
      assert_match(/\A0*\z/, hex(span.parent_span_id), label)
    end
  end

  # CONTROL: the chat spans of a tool-using ask are the same wire spans as
  # without the instrumentor: same names, kinds, outcome and content.
  def test_control_wire_chat_spans_of_a_tool_ask_are_unchanged
    setups.each do |label, prefix, env, chdir|
      _, spans = run_chat(label, prefix, env, chdir)
      wire = wire_spans(spans)
      assert_equal 2, wire.length, label
      first, second = wire.sort_by(&:start_time_unix_nano).map { |s| attrs(s.attributes) }
      assert_equal %w[chat chat], [first, second].map { |a| a['gen_ai.operation.name'] }, label
      assert_equal [12, 13], [first, second].map { |a| a['gen_ai.usage.total_tokens'] }, label
      assert_includes first['output.value'].to_s, 'call_1', label
      assert_equal ANSWER, second['output.value'], label
      assert(wire.all? { |s| s.kind == :SPAN_KIND_CLIENT && s.status.code == :STATUS_CODE_OK }, label)
    end
  end

  def test_disabled_integration_adds_no_spans
    setups.each do |label, prefix, env, chdir|
      _, spans = run_chat(label, prefix, env.merge('FI_DISABLED_INSTRUMENTATIONS' => 'ruby_llm'), chdir)
      assert_empty tool_spans(spans), label
      assert_equal 2, wire_spans(spans).length, label
      assert_equal 2, spans.length, "#{label}: #{spans.map(&:name)}"
    end
  end
end
