# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/config'
# The RubyLLM instrumentor: one chat span per model request, one
# "execute_tool <name>" span per tool call, no wire-level duplicate of either,
# and the same masking as every other span the agent exports. Each test runs the
# application in a child process with the zero-code RUBYOPT flag, so the agent
# loads before RubyLLM does, as it does in production.
class RubyLLMInstrumentorTest < Minitest::Test
  include ChildHelper
  JSON_TYPE = {'Content-Type' => 'application/json'}.freeze
  CHAT = JSON.generate(id: 'chat-1', object: 'chat.completion', model: 'gpt-4o-mini-2024-07-18', choices: [{index: 0, message: {role: 'assistant', content: 'Hello from RubyLLM'}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5}).freeze
  CALL = JSON.generate(id: 'chat-0', object: 'chat.completion', model: 'gpt-4o-mini-2024-07-18', choices: [{index: 0, message: {role: 'assistant', content: nil, tool_calls: [{id: 'call_1', type: 'function', function: {name: 'weather', arguments: JSON.generate(city: 'Paris')}}]}, finish_reason: 'tool_calls'}], usage: {prompt_tokens: 7, completion_tokens: 4, total_tokens: 11}).freeze
  STREAM = ["data: #{JSON.generate(id: 'c', model: 'gpt-4o-mini', choices: [{index: 0, delta: {role: 'assistant', content: 'one'}}])}\n\n",
            "data: #{JSON.generate(id: 'c', model: 'gpt-4o-mini', choices: [{index: 0, delta: {content: ' two'}, finish_reason: 'stop'}], usage: {prompt_tokens: 4, completion_tokens: 2})}\n\n", "data: [DONE]\n\n"].freeze
  def setup
    @collector = MockHTTP.new { |_r| [200, {'Content-Type' => 'application/x-protobuf'}, ''] }
    # A request that offers the tool and carries no tool result yet gets a tool
    # call; every other request gets the final answer.
    @provider = MockHTTP.new { |r| [200, JSON_TYPE, calls_tool?(r[:body]) ? CALL : CHAT] }
  end
  def teardown
    @provider.stop; @collector.stop
  end
  def calls_tool?(body) = body.include?('"tools"') && !body.include?('"role":"tool"')
  TOOL = <<~RUBY
    class Weather < RubyLLM::Tool
      description 'Weather for a city'
      parameter :city, description: 'City name'
      def name = 'weather'
      def execute(city:) = "Sunny in \#{city}"
    end
  RUBY
  RAISING_TOOL = TOOL.sub('"Sunny in #{city}"', "raise(ArgumentError, 'tool exploded')")
  # The child runs the RubyLLM version the suite runs under (the floor and the
  # latest are tested by pointing BUNDLE_GEMFILE at a Gemfile that pins one).
  RUBY_LLM_PIN = Gem.loaded_specs['ruby_llm'] ? "gem 'ruby_llm', '= #{Gem.loaded_specs['ruby_llm'].version}'" : ''
  def llm(code, tool: nil)
    <<~RUBY
      #{RUBY_LLM_PIN}
      require 'ruby_llm'
      RubyLLM.configure { |c| c.openai_api_key = 'mock-key'; c.openai_api_base = '#{@provider.url}/v1'; c.max_retries = 0 }
      #{tool}
      chat = RubyLLM.chat(model: 'gpt-4o-mini', provider: :openai, protocol: :chat_completions, assume_model_exists: true)
      chat.with_tools(Weather) if defined?(Weather)
      #{code}
    RUBY
  end
  def spans(layer: 'ruby_llm')
    all = export_spans
    wire = all.select { |span, _| attrs(span.attributes)['traceai.capture.layer'] == 'wire' }
    assert_empty wire.map { |span, _| span.name }, 'the wire layer must not repeat what the instrumentor reports'
    all.map(&:first).select { |span| attrs(span.attributes)['traceai.capture.layer'] == layer }
  end
  def by_name(spans, name) = spans.select { |span| span.name == name }

  def test_chat_span_carries_the_genai_attributes_and_no_wire_duplicate
    stdout, stderr, status = app(llm("chat.with_temperature(0.25); puts chat.ask('hi there').content"))
    assert status.success?, stderr; assert_equal "Hello from RubyLLM\n", stdout
    chats = spans
    assert_equal ['chat gpt-4o-mini'], chats.map(&:name)
    span = chats.first; a = attrs(span.attributes)
    assert_equal :SPAN_KIND_CLIENT, span.kind; assert_equal :STATUS_CODE_OK, span.status.code
    assert_equal 'LLM', a['gen_ai.span.kind']; assert_equal 'chat', a['gen_ai.operation.name']
    assert_equal 'openai', a['gen_ai.provider.name']
    assert_equal 'gpt-4o-mini', a['gen_ai.request.model']; assert_equal 'gpt-4o-mini-2024-07-18', a['gen_ai.response.model']
    assert_equal 3, a['gen_ai.usage.input_tokens']; assert_equal 2, a['gen_ai.usage.output_tokens']; assert_equal 5, a['gen_ai.usage.total_tokens']
    assert_equal ['stop'], a['gen_ai.response.finish_reasons']
    assert_in_delta 0.25, a['gen_ai.request.temperature']
    assert_equal 'user', a['gen_ai.input.messages.0.message.role']; assert_equal 'hi there', a['gen_ai.input.messages.0.message.content']
    assert_equal 'assistant', a['gen_ai.output.messages.0.message.role']; assert_equal 'Hello from RubyLLM', a['gen_ai.output.messages.0.message.content']
    assert_equal 'hi there', a['input.value']; assert_equal 'Hello from RubyLLM', a['output.value']
    assert_equal 'ruby_llm', a['traceai.capture.layer']
  end

  def test_chat_span_nests_under_the_application_span_on_an_application_provider
    code = app_sdk + llm("OpenTelemetry.tracer_provider.tracer('app').in_span('checkout') { |s| puts s.context.span_id.unpack1('H*'); chat.ask('hi') }")
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    parent = stdout.lines.first.strip
    chats = spans
    assert_equal ['chat gpt-4o-mini'], chats.map(&:name)
    assert_equal parent, chats.first.parent_span_id.unpack1('H*')
  end

  def test_tool_round_trip_emits_two_chat_spans_and_one_execute_tool_span
    stdout, stderr, status = app(llm("puts chat.ask('weather in Paris?').content", tool: TOOL))
    assert status.success?, stderr; assert_equal "Hello from RubyLLM\n", stdout
    all = spans
    assert_equal ['chat gpt-4o-mini', 'chat gpt-4o-mini', 'execute_tool weather'], all.map(&:name).sort
    first = all.select { |s| s.name.start_with?('chat') }.min_by(&:start_time_unix_nano)
    a = attrs(first.attributes)
    assert_equal ['tool_calls'], a['gen_ai.response.finish_reasons']
    assert_equal 'weather', a['gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name']
    tool = by_name(all, 'execute_tool weather').first; t = attrs(tool.attributes)
    assert_equal :SPAN_KIND_INTERNAL, tool.kind; assert_equal :STATUS_CODE_OK, tool.status.code
    assert_equal 'TOOL', t['gen_ai.span.kind']; assert_equal 'execute_tool', t['gen_ai.operation.name']
    assert_equal 'weather', t['gen_ai.tool.name']; assert_equal 'function', t['gen_ai.tool.type']
    # RubyLLM numbers the call itself (2.2 replaces the provider's id); the id on the tool span is the one the chat span's reply carries.
    refute_empty t['gen_ai.tool.call.id'].to_s
    assert_equal t['gen_ai.tool.call.id'], a['gen_ai.output.messages.0.message.tool_calls.0.tool_call.id']
    assert_equal({'city' => 'Paris'}, JSON.parse(t['gen_ai.tool.call.arguments']))
    assert_equal 'Sunny in Paris', t['gen_ai.tool.call.result']
    assert_equal 'ruby_llm', t['traceai.capture.layer']
  end

  def test_tool_span_is_current_while_the_tool_runs_on_an_application_provider
    tool = TOOL.sub('def execute(city:) = "Sunny in #{city}"') { %q(def execute(city:); puts(OpenTelemetry::Trace.current_span.context.span_id.unpack1('H*')); "Sunny in #{city}"; end) }
    code = app_sdk + llm("OpenTelemetry.tracer_provider.tracer('app').in_span('checkout') { |s| puts s.context.span_id.unpack1('H*'); chat.ask('weather?') }", tool: tool)
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    app_span, inside_tool = stdout.lines.first(2).map(&:strip)
    all = spans
    tool_span = by_name(all, 'execute_tool weather').first
    assert_equal inside_tool, tool_span.span_id.unpack1('H*')
    assert_equal app_span, tool_span.parent_span_id.unpack1('H*')
    assert_equal [app_span], all.select { |s| s.name.start_with?('chat') }.map { |s| s.parent_span_id.unpack1('H*') }.uniq
  end

  CALLS = JSON.generate(id: 'chat-0', object: 'chat.completion', model: 'gpt-4o-mini-2024-07-18', choices: [{index: 0, message: {role: 'assistant', content: nil, tool_calls: %w[Paris Rome].each_with_index.map { |city, i| {id: "call_#{i}", type: 'function', function: {name: 'weather', arguments: JSON.generate(city: city)}} }}, finish_reason: 'tool_calls'}], usage: {prompt_tokens: 7, completion_tokens: 4, total_tokens: 11}).freeze
  # RubyLLM 2.1 and later carry the caller's instrumentation context to the
  # threads that run concurrent tool calls; 2.0 does not.
  def test_concurrent_tool_spans_nest_under_the_callers_span
    skip 'RubyLLM 2.0 does not hand its callers context to tool threads' if Gem.loaded_specs['ruby_llm'] && Gem.loaded_specs['ruby_llm'].version < Gem::Version.new('2.1')
    @provider.stop; @provider = MockHTTP.new { |r| [200, JSON_TYPE, calls_tool?(r[:body]) ? CALLS : CHAT] }
    code = app_sdk + llm("chat.with_tool_options(concurrency: :threads); OpenTelemetry.tracer_provider.tracer('app').in_span('checkout') { |s| puts s.context.span_id.unpack1('H*'); chat.ask('weather?') }", tool: TOOL)
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    parent = stdout.lines.first.strip
    tools = by_name(spans, 'execute_tool weather')
    assert_equal 2, tools.length
    assert_equal [parent], tools.map { |t| t.parent_span_id.unpack1('H*') }.uniq
  end

  def test_tool_error_is_recorded_on_the_tool_span_and_raised_to_the_application
    code = llm("begin; chat.ask('weather?'); rescue => e; puts \"RAISED \#{e.class}: \#{e.message}\"; end", tool: RAISING_TOOL)
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal "RAISED ArgumentError: tool exploded\n", stdout
    tool = by_name(spans, 'execute_tool weather').first
    refute_nil tool
    assert_equal :STATUS_CODE_ERROR, tool.status.code; assert_equal 'tool exploded', tool.status.message
    assert_equal 'ArgumentError', attrs(tool.attributes)['error.type']
  end

  def test_failed_request_marks_the_chat_span_and_raises_to_the_application
    @provider.stop; @provider = MockHTTP.new { |_r| [500, JSON_TYPE, JSON.generate(error: {message: 'provider is down'})] }
    stdout, stderr, status = app(llm("begin; chat.ask('hi'); rescue => e; puts \"RAISED \#{e.class.name.split('::').last}\"; end"))
    assert status.success?, stderr; assert_equal "RAISED ServerError\n", stdout
    chats = spans
    assert_equal ['chat gpt-4o-mini'], chats.map(&:name)
    assert_equal :STATUS_CODE_ERROR, chats.first.status.code
    assert_equal 'RubyLLM::ServerError', attrs(chats.first.attributes)['error.type']
  end

  def test_streaming_chat_span_ends_once_with_the_whole_reply
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    stdout, stderr, status = app(llm("chat.ask('hi') { |chunk| print chunk.content }; puts"))
    assert status.success?, stderr; assert_equal "one two\n", stdout
    chats = spans
    assert_equal 1, chats.length
    a = attrs(chats.first.attributes)
    assert_equal :STATUS_CODE_OK, chats.first.status.code
    assert_equal true, a['gen_ai.request.stream']; assert_equal 'one two', a['output.value']
    assert_equal 4, a['gen_ai.usage.input_tokens']; assert_equal 2, a['gen_ai.usage.output_tokens']
    assert_equal ['stop'], a['gen_ai.response.finish_reasons']
  end

  def test_streaming_early_break_ends_the_chat_span_exactly_once
    @provider.stop; @provider = MockHTTP.new { |_r| [200, {'Content-Type' => 'text/event-stream'}, STREAM] }
    stdout, stderr, status = app(llm("chat.ask('hi') { |chunk| break }; puts 'AFTER_BREAK'"))
    assert status.success?, stderr; assert_equal "AFTER_BREAK\n", stdout
    chats = spans
    assert_equal 1, chats.length
    assert_equal :STATUS_CODE_OK, chats.first.status.code
    assert_equal true, attrs(chats.first.attributes)['traceai.ruby_llm.incomplete']
  end

  def test_streaming_transport_failure_ends_the_chat_span_once_with_an_error
    @provider.raw do |io, _|
      io.write "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
      chunk = STREAM.first; io.write "#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\n"; io.flush
    end
    stdout, stderr, status = app(llm("begin; chat.ask('hi') { |c| print c.content }; rescue => e; puts \"|RAISED\"; end"))
    assert status.success?, stderr; assert_includes stdout, '|RAISED'
    chats = spans
    assert_equal 1, chats.length
    assert_equal :STATUS_CODE_ERROR, chats.first.status.code
    refute_nil attrs(chats.first.attributes)['error.type']
  end

  HIDE_GROUPS = [
    {'FI_HIDE_INPUTS' => 'true', 'FI_HIDE_OUTPUTS' => 'true'},
    {'FI_HIDE_INPUT_MESSAGES' => 'true', 'FI_HIDE_OUTPUT_MESSAGES' => 'true'},
    {'FI_HIDE_INPUT_TEXT' => 'true', 'FI_HIDE_OUTPUT_TEXT' => 'true'}
  ].freeze
  def test_hide_settings_keep_prompts_arguments_and_results_out_of_every_span
    private_chat = JSON.generate(id: 'c', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: 'PRIVATE_ANSWER'}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5})
    private_call = CALL.sub('Paris', 'PRIVATE_CITY')
    @provider.stop; @provider = MockHTTP.new { |r| [200, JSON_TYPE, calls_tool?(r[:body]) ? private_call : private_chat] }
    tool = TOOL.sub('Sunny in', 'PRIVATE_RESULT in')
    HIDE_GROUPS.each do |flags|
      stdout, stderr, status = app(llm("puts chat.ask('PRIVATE_PROMPT').content", tool: tool), flags)
      assert status.success?, stderr; assert_equal "PRIVATE_ANSWER\n", stdout
      all = spans
      assert_equal ['chat gpt-4o-mini', 'chat gpt-4o-mini', 'execute_tool weather'], all.map(&:name).sort, flags.inspect
      %w[PRIVATE_PROMPT PRIVATE_CITY PRIVATE_RESULT PRIVATE_ANSWER].each do |marker|
        refute_includes all.map(&:to_h).to_s, marker, "#{marker} under #{flags.keys.inspect}"
      end
      t = attrs(by_name(all, 'execute_tool weather').first.attributes)
      # Names, ids and counts are not content.
      assert_equal 'weather', t['gen_ai.tool.name']; refute_includes ['__REDACTED__', nil, ''], t['gen_ai.tool.call.id']
      chat = attrs(all.select { |s| s.name.start_with?('chat') }.first.attributes)
      assert_equal 'gpt-4o-mini', chat['gen_ai.request.model']; assert chat['gen_ai.usage.input_tokens']
    end
  end

  def test_hiding_invocation_parameters_drops_the_request_parameters
    _, stderr, status = app(llm("chat.with_temperature(0.25); chat.ask('hi')"), {'FI_HIDE_LLM_INVOCATION_PARAMETERS' => 'true'})
    assert status.success?, stderr
    a = attrs(spans.first.attributes)
    refute a.key?('gen_ai.request.temperature'); assert_equal 'gpt-4o-mini', a['gen_ai.request.model']
  end

  def test_session_and_user_helpers_reach_the_chat_and_tool_spans
    _, stderr, status = app(llm("TraceAI::Auto.using_session('session-9') { chat.ask('hi') }", tool: TOOL))
    assert status.success?, stderr
    all = spans
    refute_empty all
    assert_equal ['session-9'], all.map { |s| attrs(s.attributes)['session.id'] }.uniq
  end

  # The nine content hide settings of the 1.4 line withhold raw input and raw
  # output, whichever of them is the one that is set.
  CONTENT_HIDE = %w[hide_inputs hide_outputs hide_input_messages hide_output_messages hide_input_text hide_output_text
                    hide_input_images hide_embedding_vectors hide_llm_invocation_parameters].freeze
  def test_raw_input_and_output_are_dropped_under_every_content_hide_setting
    attributes = {'raw.input' => 'RAW_IN', 'raw.input.value' => 'RAW_IN_CHILD', 'raw.output' => 'RAW_OUT', 'raw.output.value' => 'RAW_OUT_CHILD', 'gen_ai.request.model' => 'm'}
    shown = TraceAI::Auto::Privacy.mask(attributes, TraceAI::Auto::Config.new({}, env: {}))
    assert_equal attributes, shown
    CONTENT_HIDE.each do |setting|
      config = TraceAI::Auto::Config.new({}, env: {"FI_#{setting.upcase}" => 'true'})
      masked = TraceAI::Auto::Privacy.mask(attributes, config)
      assert_equal %w[gen_ai.request.model], masked.keys, setting
    end
    config = TraceAI::Auto::Config.new({}, env: {'FI_HIDE_ERROR_DETAILS' => 'true'})
    assert_equal attributes, TraceAI::Auto::Privacy.mask(attributes, config), 'error-detail hiding does not hide content'
  end

  def test_tool_identity_stays_visible_and_arguments_and_result_follow_their_own_side
    attributes = {'gen_ai.tool.name' => 'weather', 'gen_ai.tool.call.id' => 'call_1', 'gen_ai.tool.type' => 'function',
                  'gen_ai.tool.call.arguments' => '{"q":1}', 'gen_ai.tool.call.result' => 'RESULT'}
    mask = lambda do |setting|
      TraceAI::Auto::Privacy.mask(attributes, TraceAI::Auto::Config.new({}, env: {"FI_#{setting.upcase}" => 'true'}))
    end
    %w[hide_inputs hide_input_text hide_input_messages].each do |setting|
      masked = mask.call(setting)
      assert_equal '__REDACTED__', masked['gen_ai.tool.call.arguments'], setting
      assert_equal 'RESULT', masked['gen_ai.tool.call.result'], setting
      assert_equal 'weather', masked['gen_ai.tool.name']; assert_equal 'call_1', masked['gen_ai.tool.call.id']; assert_equal 'function', masked['gen_ai.tool.type']
    end
    %w[hide_outputs hide_output_text hide_output_messages].each do |setting|
      masked = mask.call(setting)
      assert_equal '__REDACTED__', masked['gen_ai.tool.call.result'], setting
      assert_equal '{"q":1}', masked['gen_ai.tool.call.arguments'], setting
      assert_equal 'weather', masked['gen_ai.tool.name']
    end
  end

  def test_doctor_reports_ruby_llm_instrumented_when_the_gem_is_present
    code = "require 'json'; puts JSON.generate(TraceAI::Auto.report)"
    stdout, stderr, status = app(code)
    assert status.success?, stderr
    report = JSON.parse(stdout)
    assert_equal 'instrumented', report.dig('integrations', 'ruby_llm', 'status')
    refute report.dig('integrations', 'ruby_llm').key?('reason')
  end

  def test_the_disabled_list_turns_the_instrumentor_off_and_leaves_the_wire_span
    code = "require 'json'; puts JSON.generate(TraceAI::Auto.report)"
    stdout, stderr, status = app(code, {'FI_DISABLED_INSTRUMENTATIONS' => 'ruby_llm'})
    assert status.success?, stderr
    assert_equal 'disabled', JSON.parse(stdout).dig('integrations', 'ruby_llm', 'status')
    stdout, stderr, status = app(llm("puts chat.ask('hi').content"), {'FI_DISABLED_INSTRUMENTATIONS' => 'ruby_llm'})
    assert status.success?, stderr; assert_equal "Hello from RubyLLM\n", stdout
    all = export_spans.map(&:first)
    assert_equal ['chat gpt-4o-mini'], all.map(&:name)
    assert_equal 'wire', attrs(all.first.attributes)['traceai.capture.layer']
  end

  def test_an_allowlist_without_ruby_llm_leaves_the_wire_span
    _, stderr, status = app(llm("puts chat.ask('hi').content"), {'FI_INSTRUMENTATIONS' => 'wire'})
    assert status.success?, stderr
    all = export_spans.map(&:first)
    assert_equal ['chat gpt-4o-mini'], all.map(&:name)
    assert_equal 'wire', attrs(all.first.attributes)['traceai.capture.layer']
  end

  # Without the RUBYOPT flag: RubyLLM is loaded first and the application starts
  # the agent afterwards, as the README describes for clients loaded after boot.
  def test_explicit_start_after_ruby_llm_is_loaded_instruments_it
    code = "#{RUBY_LLM_PIN}; require 'ruby_llm'; RubyLLM::Support::Instrumentation\n" \
           "require 'traceai/auto/agent'; TraceAI::Auto.init; TraceAI::Auto.auto_instrument\n" + llm("puts chat.ask('hi').content; TraceAI::Auto.flush")
    stdout, stderr, status = app(code, {'RUBYOPT' => nil})
    assert status.success?, stderr; assert_equal "Hello from RubyLLM\n", stdout
    assert_equal ['chat gpt-4o-mini'], spans.map(&:name)
  end

  def test_an_application_exception_in_the_instrumentor_never_reaches_the_application
    # A payload the instrumentor cannot read (no messages, no model) still lets
    # the call through unchanged and returns what the block returned.
    code = <<~RUBY
      #{RUBY_LLM_PIN}
      require 'ruby_llm'
      result = RubyLLM.instrument('chat.ruby_llm', {input_messages: 42, model: Object.new, tools: nil}) { |event| event[:response] = Object.new; :answer }
      puts result.inspect
      begin
        RubyLLM.instrument('tool_call.ruby_llm', {tool_arguments: 7}) { raise KeyError, 'from the block' }
      rescue KeyError => e
        puts "KEPT \#{e.message}"
      end
    RUBY
    stdout, stderr, status = app(code)
    assert status.success?, stderr; assert_equal ":answer\nKEPT from the block\n", stdout
  end

  # fi-security: the instrumentor writes the tool arguments into three attributes of the execute_tool
  # span and the tool result into three more. Every copy follows its own side's hide settings, and
  # the raw copies follow all nine content settings. A new attribute that carries either value must
  # be added to these lists, or the key-set check below fails.
  ARGUMENT_COPIES = %w[gen_ai.tool.call.arguments input.value raw.input].freeze
  RESULT_COPIES = %w[gen_ai.tool.call.result output.value raw.output].freeze
  INPUT_SIDE = %w[hide_inputs hide_input_text hide_input_messages].freeze
  OUTPUT_SIDE = %w[hide_outputs hide_output_text hide_output_messages].freeze
  def tool_span_with(flags)
    private_chat = JSON.generate(id: 'c', object: 'chat.completion', model: 'gpt-4o-mini', choices: [{index: 0, message: {role: 'assistant', content: 'final answer'}, finish_reason: 'stop'}], usage: {prompt_tokens: 3, completion_tokens: 2, total_tokens: 5})
    private_call = CALL.sub('Paris', 'SYNTHETIC_ARGUMENT_VALUE')
    @provider.stop; @provider = MockHTTP.new { |r| [200, JSON_TYPE, calls_tool?(r[:body]) ? private_call : private_chat] }
    _, stderr, status = app(llm("chat.ask('question')", tool: TOOL.sub('"Sunny in #{city}"', '"SYNTHETIC_RESULT_VALUE"')), flags) # the result does not echo the argument
    assert status.success?, stderr
    tools = by_name(spans, 'execute_tool weather')
    assert_equal 1, tools.length, flags.inspect
    attrs(tools.first.attributes)
  end
  def carrying(attributes, marker) = attributes.select { |_key, value| value.to_s.include?(marker) }.keys.sort
  def assert_withheld(attributes, keys, marker, setting)
    keys.each { |key| assert_includes [nil, '__REDACTED__'], attributes[key], "#{key} under #{setting}" }
    assert_empty carrying(attributes, marker), "#{marker} survived under #{setting}"
  end

  def test_tool_arguments_and_result_are_in_exactly_the_known_copies_without_a_hide_setting
    tool = tool_span_with({})
    assert_equal ARGUMENT_COPIES.sort, carrying(tool, 'SYNTHETIC_ARGUMENT_VALUE'), 'a new attribute carries the tool arguments: cover it here and in the hide checks'
    assert_equal RESULT_COPIES.sort, carrying(tool, 'SYNTHETIC_RESULT_VALUE'), 'a new attribute carries the tool result: cover it here and in the hide checks'
  end

  (INPUT_SIDE + OUTPUT_SIDE).each do |setting|
    define_method("test_every_tool_copy_follows_its_side_under_#{setting}") do
      tool = tool_span_with("FI_#{setting.upcase}" => 'true')
      input = INPUT_SIDE.include?(setting)
      withheld, kept = input ? [ARGUMENT_COPIES, RESULT_COPIES] : [RESULT_COPIES, ARGUMENT_COPIES]
      withheld_marker, kept_marker = input ? %w[SYNTHETIC_ARGUMENT_VALUE SYNTHETIC_RESULT_VALUE] : %w[SYNTHETIC_RESULT_VALUE SYNTHETIC_ARGUMENT_VALUE]
      assert_withheld(tool, withheld, withheld_marker, setting)
      # The raw copy of the other side is withheld by every content setting; its other two copies stay.
      raw = kept.find { |key| key.start_with?('raw.') }
      assert_nil tool[raw], "#{raw} under #{setting}"
      (kept - [raw]).each { |key| assert_includes tool[key].to_s, kept_marker, "#{key} under #{setting}" }
      assert_equal 'weather', tool['gen_ai.tool.name']
      refute_includes ['__REDACTED__', nil, ''], tool['gen_ai.tool.call.id']
    end
  end

  (CONTENT_HIDE - INPUT_SIDE - OUTPUT_SIDE).each do |setting|
    define_method("test_raw_tool_copies_are_withheld_under_#{setting}") do
      tool = tool_span_with("FI_#{setting.upcase}" => 'true')
      assert_nil tool['raw.input'], setting
      assert_nil tool['raw.output'], setting
      assert_equal 'weather', tool['gen_ai.tool.name']
    end
  end
end
