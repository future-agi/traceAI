# frozen_string_literal: true
require_relative 'test_helper'
require_relative 'test_wire'
require_relative 'test_e2e'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/transports'

class Round6TransportMilestonesTest < Minitest::Test
  A = TraceAI::Auto
  def capture(limit: 4 * 1024 * 1024)
    config = A::Config.new({wire_max_body_bytes: limit}, env: {})
    A.stub(:config, config) do
      A.stub(:current_context, nil) do
        yield A::Capture.new('POST', 'http://127.0.0.1:1234/v1/chat/completions', {'content-type' => 'application/json'},
                             '{"model":"m","messages":[],"stream":true}', started: Time.at(100), clock: 100.0)
      end
    end
  end
  def event(delta)
    "data: #{JSON.generate(choices: [{delta: delta}])}\n\n"
  end
  def test_a10_capture_uses_arrival_before_parse_and_complete_event_delimiter
    capture do |c|
      c.headers(200, {'content-type' => 'text/event-stream'})
      c.chunk('', clock: 100.0, observed: Time.at(100))
      c.chunk(event(role: 'assistant'), clock: 100.01, observed: Time.at(100.01))
      text = event(content: 'Hello')
      c.chunk(text.byteslice(0, text.bytesize - 1), clock: 100.02, observed: Time.at(100.02))
      before = A::Wire.build(c.request, c.response).attributes
      assert_in_delta 0.01, before['gen_ai.response.time_to_first_chunk'], 0.000001
      refute before.key?('gen_ai.response.time_to_first_text')
      c.chunk("\n", clock: 100.04, observed: Time.at(100.04))
      Process.stub(:clock_gettime, 900.0) do
        after = A::Wire.build(c.request, c.response).attributes
        assert_in_delta 0.04, after['gen_ai.response.time_to_first_text'], 0.000001
      end
      emitted = nil
      A.stub(:emit, ->(*args) { emitted = args }) { c.finish }
      assert_equal Time.at(100), emitted[3]
      assert_equal Time.at(100.04), emitted[4]
      assert_nil c.response[:arrivals], 'release timestamp storage after finalization'
    end
  end
  def test_a10_body_limit_marks_lost_arrival_association_incomplete
    capture(limit: 256) do |c|
      c.headers(200, {'content-type' => 'text/event-stream'})
      c.chunk(event(content: 'x' * 300), clock: 100.01)
      attrs = A::Wire.build(c.request, c.response).attributes
      assert_equal true, attrs['traceai.wire.truncated']
      assert_equal true, attrs['traceai.wire.incomplete']
      assert_equal 0.01, attrs['gen_ai.response.time_to_first_chunk']
      refute attrs.key?('gen_ai.response.time_to_first_text')
    end
  end
  def test_a10_bounded_arrivals_keep_known_prefix_and_omit_unassociated_output
    [false, true].each do |early|
      capture do |c|
        c.headers(200, {'content-type' => 'text/event-stream'})
        c.chunk(event(early ? {content: 'early'} : {role: 'assistant'}), clock: 100.0)
        A::Capture::MAX_ARRIVALS.times { |index| c.chunk(": ping\n\n", clock: 100.01 + index / 1000.0) }
        c.chunk(event(content: 'late'), clock: 110.0)
        assert_equal A::Capture::MAX_ARRIVALS, c.response[:arrivals].size
        attrs = A::Wire.build(c.request, c.response).attributes
        assert_equal 0.0, attrs['gen_ai.response.time_to_first_chunk']
        assert_equal true, attrs['traceai.wire.incomplete']
        early ? assert_equal(0.0, attrs['gen_ai.response.time_to_first_text']) : refute(attrs.key?('gen_ai.response.time_to_first_text'))
      end
    end
  end
  def test_a10_net_http_records_raw_arrival_before_native_inflater
    capture do |c|
      c.headers(200, {'content-type' => 'text/event-stream'}, decoded: true)
      now = 100.01; output = +''
      payload = event(content: 'inflated')
      native = Class.new do
        attr_accessor :adapter
        def inflate_adapter(_destination) = adapter
      end.new
      destination = A::Tee.new(output, c)
      adapter = Object.new
      adapter.define_singleton_method(:<<) { |bytes| now = 120.0; destination << payload; bytes }
      native.adapter = adapter
      native.singleton_class.prepend(A::NetInflater)
      compressed = +'compressed'
      wrapped = native.inflate_adapter(destination)
      Process.stub(:clock_gettime, ->(*) { now }) { assert_same compressed, wrapped << compressed }
      attrs = A::Wire.build(c.request, c.response).attributes
      assert_equal payload, output
      assert_equal 0.01, attrs['gen_ai.response.time_to_first_chunk']
      assert_equal 0.01, attrs['gen_ai.response.time_to_first_text']
      assert_nil c.native_arrival
    end
  end
  def test_a10_httpx_records_raw_arrival_before_native_decoder
    capture do |c|
      c.headers(200, {'content-type' => 'text/event-stream', 'content-encoding' => 'gzip'}, decoded: true)
      now = 100.01
      native = Class.new do
        attr_accessor :decode
        def decode_chunk(bytes) = decode.call(bytes)
      end.new
      native.instance_variable_set(:@traceai_capture, c)
      native.singleton_class.prepend(A::HTTPXDecodeTee)
      native.decode = ->(_bytes) { now = 105.0; nil }
      Process.stub(:clock_gettime, ->(*) { now }) { assert_nil native.send(:decode_chunk, 'gzip header') }
      now = 100.03
      payload = event(content: 'decoded')
      native.decode = ->(_bytes) { now = 110.0; payload }
      Process.stub(:clock_gettime, ->(*) { now }) { assert_same payload, native.send(:decode_chunk, 'compressed output') }
      attrs = A::Wire.build(c.request, c.response).attributes
      assert_equal 0.01, attrs['gen_ai.response.time_to_first_chunk']
      assert_equal 0.03, attrs['gen_ai.response.time_to_first_text']
    end
  end
end

class DecodedBedrockMilestonesTest < Minitest::Test
  Dir[File.join(FixtureHarness::SPEC, 'fixtures/round6/*.json')].sort.each do |file|
    next unless (157..166).cover?(File.basename(file).to_i)
    define_method("test_decoded_#{File.basename(file, '.json').tr('-', '_')}") do
      fixture = JSON.parse(File.read(file)); response = fixture.fetch('response')
      frames = response.fetch('sse').each_with_index.map { |event, index| [event, index + 1] }
      arrivals = response.fetch('arrival_offsets_ms').each_with_index.map { |offset, index| [index + 1, offset / 1000.0] }
      value = TraceAI::Auto::Wire::Milestones.first_text('bedrock.converse', frames, arrivals)
      expected = fixture.fetch('expect')
      key = 'gen_ai.response.time_to_first_text'
      if expected.fetch('attributes').key?(key)
        assert_equal expected['attributes'][key], value
      else
        assert_includes expected['absent'], key
        assert_nil value
      end
      assert_equal expected['attributes']['gen_ai.response.time_to_first_chunk'], arrivals.first.last
    end
  end
end

class EndToEndTest
  def test_a10_stream_milestones_preserve_net_http_and_httpx_application_bytes
    chunks = ["data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n",
              "data: {\"choices\":[{\"delta\":{\"refusal\":\"No\"}}]}\n\n", "data: [DONE]\n\n"]
    [false, true].each do |compressed|
      headers = {'content-type' => 'text/event-stream'}
      delivered = chunks
      if compressed
        io = StringIO.new; gz = Zlib::GzipWriter.new(io); gz.write(chunks.join); gz.close
        delivered = [io.string.byteslice(0, 10), io.string.byteslice(10..)]
        headers['content-encoding'] = 'gzip'
      end
      @provider.stop
      @provider = MockHTTP.new { |_request| [200, headers, delivered] }
      %w[net_http httpx].each do |transport|
        code = if transport == 'net_http'
                 <<~RUBY
                   require 'net/http'; require 'json'
                   body = +''
                   request = Net::HTTP::Post.new('/v1/chat/completions'); request['content-type'] = 'application/json'
                   request.body = '{"model":"m","messages":[],"stream":true}'
                   response = Net::HTTP.start('127.0.0.1', #{@provider.port}) { |client| client.request(request) { |res| res.read_body { |part| body << part } } }
                   puts JSON.generate([response.class.name, response.code, body])
                 RUBY
               else
                 <<~RUBY
                   require 'httpx'; require 'json'
                   response = HTTPX.post('#{@provider.url}/v1/chat/completions', headers: {'content-type' => 'application/json'}, body: '{"model":"m","messages":[],"stream":true}')
                   puts JSON.generate([response.class.name, response.status, response.to_s])
                 RUBY
               end
        results = [false, true].map do |enabled|
          stdout, stderr, status = app(code, enabled ? {} : {'RUBYOPT' => nil})
          assert status.success?, stderr
          spans = export_spans
          if enabled
            assert_equal 1, spans.size
            attributes = attrs(spans.first.first.attributes)
            assert_operator attributes.fetch('gen_ai.response.time_to_first_text'), :>=, attributes.fetch('gen_ai.response.time_to_first_chunk')
          else
            assert_empty spans
          end
          stdout
        end
        assert_equal results.first, results.last, transport
        assert_equal chunks.join, JSON.parse(results.last).last
      end
    end
  end
end
