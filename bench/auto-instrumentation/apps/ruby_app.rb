STDOUT.sync = true
puts '{"kind":"ready"}'
exit 0 if ENV['BENCH_STARTUP_ONLY'] == '1'
require 'net/http'
require 'json'
require 'base64'
clock = -> { Process.clock_gettime(Process::CLOCK_MONOTONIC, :nanosecond) }
base = URI(ENV.fetch('BENCH_PROVIDER'))
n, warmup = ENV.fetch('BENCH_N').to_i, ENV.fetch('BENCH_WARMUP').to_i
http = Net::HTTP.new(base.host, base.port, nil)
http.read_timeout = 20
http.start do |client|
  [false, true].each do |stream|
    (n + warmup).times do |i|
      id = "#{ENV.fetch('BENCH_PAIR')}-#{stream ? 1 : 0}-#{i}"
      request = Net::HTTP::Post.new('/v1/chat/completions', {'Content-Type' => 'application/json'})
      request.body = JSON.generate(model: 'gpt-4o-mini', messages: [{role: 'user', content: id}], stream: stream, stream_options: {include_usage: true})
      bytes = ''.b; first = nil; start = clock.call
      client.request(request) do |response|
        raise "HTTP #{response.code}" unless response.code == '200'
        response.read_body do |chunk|
          first ||= clock.call unless chunk.empty?
          bytes << chunk.b
        end
      end
      finish = clock.call
      puts JSON.generate(kind: 'call', id: id, stream: stream, warmup: i < warmup,
                         latency_us: (finish-start)/1000.0, ttfc_us: (first-start)/1000.0,
                         body_b64: Base64.strict_encode64(bytes))
    end
  end
  client.get('/health')
  client.post('/v1/chat/completions', '{"not_ai":true}', {'Content-Type' => 'application/json'})
end
puts '{"kind":"done"}'
sleep ENV.fetch('BENCH_SETTLE_SECONDS').to_f
