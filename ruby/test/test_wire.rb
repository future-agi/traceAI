# frozen_string_literal: true
require 'minitest/autorun'
require 'stringio'
require_relative '../lib/traceai/auto/wire'
module FixtureHarness
  SPEC = File.expand_path('../../spec/auto-instrumentation', __dir__)
  module_function
  def run(fixture)
    r = fixture.fetch('request'); s = fixture.fetch('response')
    req = {method: r.fetch('method'), url: r.fetch('url'), headers: r.fetch('headers', {}).dup, unavailable: r['unavailable']}
    if r.key?('json') then req[:body] = JSON.generate(r['json'])
    elsif r.key?('text') then req[:body] = r['text']
    elsif r['multipart']
      boundary = 'traceai-fixture-boundary'; body = ''.b
      r['multipart']['fields'].each { |k, v| body << "--#{boundary}\r\nContent-Disposition: form-data; name=\"#{k}\"\r\n\r\n#{v}\r\n" }
      r['multipart']['files'].each do |f|
        body << "--#{boundary}\r\nContent-Disposition: form-data; name=\"#{f['name']}\"; filename=\"#{f['filename']}\"\r\nContent-Type: #{f['content_type']}\r\n\r\n"
        body << ("\0".b * f['size']) << "\r\n"
      end
      body << "--#{boundary}--\r\n"; req[:body] = body; req[:headers]['content-type'] = "multipart/form-data; boundary=#{boundary}"
    elsif r['binary']
      req[:binary] = true; req[:body_size] = r['binary']['size']; req[:headers]['content-type'] ||= r['binary']['content_type']
    end
    chunks = if s.key?('json') then [JSON.generate(s['json'])]
             elsif s.key?('text') then [s['text']]
             elsif s['sse']
               s['sse'].map do |event|
                 if event.is_a?(Hash)
                   data = event.reject { |k, _| k == 'event' }
                   (event['event'] ? "event: #{event['event']}\n" : '') + "data: #{JSON.generate(data)}\n\n"
                 else "data: #{event}\n\n"
                 end
               end
             elsif s['ndjson'] then s['ndjson'].map { |item| JSON.generate(item) + "\n" }
             elsif s['binary'] then ["\0" * (s['binary']['size'] / 2), "\0" * (s['binary']['size'] - s['binary']['size'] / 2)]
             else [] end
    if s['chunk_sizes']
      bytes = chunks.join.b; offset = 0
      chunks = s['chunk_sizes'].map do |size|
        bytes.byteslice(offset, size).tap { offset += size }
      end
      raise 'fixture partition does not cover body' unless offset == bytes.bytesize
    end
    headers = s.fetch('headers', {}).dup
    if s['gzip']
      io = StringIO.new; gz = Zlib::GzipWriter.new(io); gz.write(chunks.join); gz.close
      chunks = [io.string]; headers['content-encoding'] = 'gzip'
    end
    delivered = s['error_after_chunks'] ? chunks.first(s['error_after_chunks']) : chunks
    offsets = s['arrival_offsets_ms'] || Array.new(chunks.size, 12.5)
    size = 0; arrivals = []
    delivered.each_with_index do |chunk, index|
      next if chunk.empty?
      size += chunk.bytesize
      arrivals << [size, offsets.fetch(index) / 1000.0]
    end
    resp = {status: s['status'], headers: headers, size: size, body: delivered.join.b, arrivals: arrivals,
            first_chunk_seconds: arrivals.first&.last, error: s.key?('error_after_chunks') ? Timeout::Error.new('fixture timeout') : nil}
    TraceAI::Auto::Wire.build(req, resp)
  end
end
require 'timeout'
class WireFixturesTest < Minitest::Test
  def test_catalog_is_byte_identical
    assert_equal File.binread(File.join(FixtureHarness::SPEC, 'wire-protocols.json')), File.binread(File.expand_path('../lib/traceai/auto/wire-protocols.json', __dir__))
  end
  Dir[File.join(FixtureHarness::SPEC, 'fixtures/**/*.json')].sort.each do |file|
    # Spec A10 exception: synthetic Bedrock carriers test decoded recognition,
    # never claim AWS binary event-stream support through this wire harness.
    next if (157..166).cover?(File.basename(file).to_i)
    define_method("test_#{File.basename(file, '.json').tr('-', '_')}") do
      fixture = JSON.parse(File.read(file)); result = FixtureHarness.run(fixture); e = fixture.fetch('expect')
      unless e['captured']
        assert_nil result, fixture['name']; next
      end
      refute_nil result, fixture['name']
      assert_equal e['name'], result.name if e.key?('name')
      assert_equal e['status'], result.ok ? 'OK' : 'ERROR' if e.key?('status')
      assert_equal e['status_description'], result.description if e.key?('status_description')
      e.fetch('attributes', {}).each do |key, expected|
        actual = result.attributes[key]
        if expected.is_a?(String) && expected.match?(/\A[\[{]/)
          begin
            assert_equal JSON.parse(expected), JSON.parse(actual), key; next
          rescue JSON::ParserError, TypeError
            nil
          end
        end
        assert_equal expected, actual, key
      end
      e.fetch('present', []).each { |key| assert result.attributes.key?(key), key }
      e.fetch('absent', []).each { |key| refute result.attributes.key?(key), key }
      e.fetch('not_contains', {}).each { |key, strings| strings.each { |value| refute_includes result.attributes[key].to_s, value, key } }
    end
  end
end
