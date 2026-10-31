# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/wire'
require_relative '../lib/traceai/auto/config'
require 'tempfile'
class SafetyTest < Minitest::Test
  W = TraceAI::Auto::Wire
  P = TraceAI::Auto::Privacy
  def request(path = '/v1/chat/completions', host = 'api.openai.com', body = nil)
    {method: 'POST', url: "https://#{host}#{path}", headers: {'Content-Type' => 'application/json'}, body: JSON.generate(body || {model: 'm', messages: [{role: 'user', content: 'hi'}]})}
  end
  def response(data = {}, type = 'application/json')
    {status: 200, headers: {'content-type' => type}, body: data.is_a?(String) ? data : JSON.generate(data), size: 12}
  end
  def test_unknown_and_known_truncated_requests
    req = request; req[:body] = 'x' * 200
    result = W.build(req, response, limit: 100)
    assert_equal true, result.attributes['traceai.wire.truncated']; refute result.attributes.key?('input.value')
    req = request('/v1/chat/completions', 'my-proxy.example'); req[:body] = 'x' * 200
    assert_nil W.build(req, response, limit: 100)
  end
  def test_unavailable_transport_failure_known_host
    req = request; req.delete(:body); req[:unavailable] = true
    result = W.build(req, response.merge(error: Timeout::Error.new('no response'), status: nil))
    refute result.ok; assert_equal 'Timeout::Error', result.attributes['error.type']; assert_equal true, result.attributes['traceai.wire.body_unavailable']
  end
  def test_compression_bounds_and_unknown_encoding
    io = StringIO.new; gz = Zlib::GzipWriter.new(io); gz.write('x' * 1024 * 1024); gz.close
    r = response(io.string); r[:headers]['content-encoding'] = 'gzip'
    result = W.build(request, r, limit: 1024)
    assert_equal true, result.attributes['traceai.wire.truncated']; refute result.attributes.key?('output.value')
    r = response('opaque'); r[:headers]['content-encoding'] = 'br'
    result = W.build(request, r)
    assert_equal 'br', result.attributes['traceai.wire.undecoded_encoding']; assert result.attributes['input.value']
  end
  def test_deflate_and_raw_deflate
    data = JSON.generate(choices: [{message: {role: 'assistant', content: 'decoded'}}])
    [Zlib::MAX_WBITS, -Zlib::MAX_WBITS].each do |w|
      z = Zlib::Deflate.new(Zlib::DEFAULT_COMPRESSION, w); encoded = z.deflate(data, Zlib::FINISH); z.close
      r = response(encoded); r[:headers]['content-encoding'] = 'deflate'
      assert_equal 'decoded', W.build(request, r).attributes['output.value']
    end
  end
  def test_binary_never_recorded
    req = request('/v1/audio/speech', 'api.openai.com', {model: 'tts', input: 'hello'})
    r = response("\xFF\x00RAW_AUDIO", 'audio/mpeg'); r[:size] = 123
    a = W.build(req, r).attributes
    assert_equal 123, a['traceai.audio.output_bytes']; refute a.key?('output.value'); refute_includes a.to_s, 'RAW_AUDIO'
  end
  def test_privacy_every_surface
    secret = 'PRIVATE-SECRET-1234'; media = 'A' * 300
    value = {data: media, images: [media], nested: {'authorization_token' => secret}, url: 'https://cdn/x?X-Amz-Signature=abc&sv=xyz&safe=ok',
             message: "data:audio/wav;base64,#{media} #{secret}", tool: JSON.generate('api-key' => secret)}
    scrubbed = P.scrub(JSON.generate(value), [secret])
    refute_includes scrubbed, media; refute_includes scrubbed, secret; refute_includes scrubbed, '=abc'; refute_includes scrubbed, '=xyz'; assert_includes scrubbed, 'safe=ok'
    assert_includes scrubbed, '<omitted: 300 base64 chars>'
    assert_equal '<redacted>', JSON.parse(scrubbed).dig('nested', 'authorization_token')
  end
  def test_parameterized_data_urls_are_scrubbed_on_every_wire_surface
    media = 'A' * 400
    ['data:audio/wav;charset=binary;base64,', 'DATA:image/png;name=photo.png;charset=utf-8;BASE64,'].each do |prefix|
      data = prefix + media
      body = {model: 'm', messages: [{role: 'user', content: data}], response_format: {description: data},
              tools: [{type: 'function', function: {name: 'f', description: data}}]}
      reply = {choices: [{message: {role: 'assistant', content: data, tool_calls: [{id: 't', function: {name: 'f', arguments: JSON.generate(value: data)}}]}}]}
      result = W.build(request('/v1/chat/completions', 'api.openai.com', body), response(reply))
      %w[input.value gen_ai.input.messages.0.message.content gen_ai.request.parameters gen_ai.tool.definitions.0.tool.json_schema output.value gen_ai.output.messages.0.message.content gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments].each do |key|
        assert_includes result.attributes.fetch(key), '<omitted: 400 base64 chars>', key
        refute_includes result.attributes.fetch(key), media, key
      end
      failure = W.build(request, response({error: {message: data}}).merge(status: 400))
      assert_equal '<omitted: 400 base64 chars>', failure.description
    end
  end
  def test_invocation_hiding_alone_covers_raw_request_alias
    body = {model: 'm', messages: [{role: 'user', content: 'visible input'}],
            response_format: {type: 'json_schema', json_schema: {name: 'PRIVATE_SCHEMA_NAME', schema: {type: 'object'}}}}
    attributes = W.build(request('/v1/chat/completions', 'api.openai.com', body), response).attributes
    config = TraceAI::Auto::Config.new({}, env: {'FI_HIDE_LLM_INVOCATION_PARAMETERS' => 'true'})
    masked = P.mask(attributes, config)
    refute masked.key?('gen_ai.request.parameters')
    assert_equal '__REDACTED__', masked['input.value']
    assert_equal 'visible input', masked['gen_ai.input.messages.0.message.content']
    refute_includes JSON.generate(masked), 'PRIVATE_SCHEMA_NAME'
  end
  def test_all_credential_headers_and_config_keys
    TraceAI::Auto::Privacy::CATALOG['credentials']['seed_names'].each do |name|
      headers = {name => name == 'authorization' ? 'Bearer long-secret-value' : 'long-secret-value'}
      assert_equal '<redacted>', P.scrub('long-secret-value', P.secrets(headers))
    end
    config = TraceAI::Auto::Config.new({}, env: {'FI_API_KEY' => 'long-secret-value', 'FI_BASE_URL' => 'https://x?key=foo', 'FI_PROJECT_NAME' => 'long-secret-value'})
    masked = JSON.generate(config.masked)
    refute_includes masked, 'long-secret-value'; refute_includes masked, '=foo'
  end
  def test_side_hiding_and_invocation_flags
    attrs = {'input.value' => 'secret', 'gen_ai.input.messages.0.message.content' => 'secret', 'gen_ai.tool.definitions.0.tool.json_schema' => 'secret',
             'gen_ai.request.parameters' => 'secret', 'output.value' => 'answer', 'gen_ai.output.messages.0.message.content' => 'answer', 'gen_ai.audio.transcript' => 'secret', 'gen_ai.operation.name' => 'text_to_speech'}
    config = TraceAI::Auto::Config.new({hide_input_text: true, hide_output_text: true}, env: {})
    masked = P.mask(attrs, config); refute_includes JSON.generate(masked), 'secret'; refute_includes JSON.generate(masked), 'answer'; assert P.hide_errors?(config)
    config = TraceAI::Auto::Config.new({hide_inputs: true, hide_llm_invocation_parameters: true}, env: {})
    masked = P.mask(attrs, config); refute masked.key?('gen_ai.request.parameters'); refute masked.key?('gen_ai.input.messages.0.message.content')
  end
  def test_config_precedence_and_subset
    file = Tempfile.new(['config', '.toml'], File.join(ROOT, '.bundle/tmp'))
    file.write("project_name = \"file # not-comment\" # comment\nwire_capture = false\nsample_rate = 0.25\ninstrumentations = [\"openai\", \"wire\"]\ndebug = true\n[ignored]\nproject_type = \"observe\"\n"); file.close
    config = TraceAI::Auto::Config.new({project_name: 'argument'}, env: {'FI_CONFIG_FILE' => file.path, 'FI_PROJECT_NAME' => 'env', 'FI_WIRE_CAPTURE' => 'true', 'FI_AUTO_DEBUG' => 'false'})
    assert_equal 'argument', config['project_name']; assert config.truthy?('wire_capture'); refute config.truthy?('auto_debug'); assert_equal 0.25, config['sample_rate']; assert_equal %w[openai wire], config.list('instrumentations')
    config = TraceAI::Auto::Config.new({}, env: {'FI_CONFIG_FILE' => file.path})
    assert_equal 'file # not-comment', config['project_name']; refute config.truthy?('wire_capture')
  ensure
    file&.unlink
  end
  def test_dependency_free_config_parser_escaping_and_invalid_lines
    file = Tempfile.new(['config-parser', '.toml'], File.join(ROOT, '.bundle/tmp'))
    file.write(<<~'TOML')
      project_name = "escaped \"quote\" # text \\ A 😀"
      instrumentations = ["wire", "escaped\\slash", "line\nfeed"]
      project_version_name = "https:\/\/example.com"
      sample_rate = +2.5e-1
      wire_max_body_bytes = 1_024
      invalid_array = ["trailing",]
      invalid_string = "\uD800"
      invalid_type = {"nested": "not supported"}
      auto_debug = false
    TOML
    file.close
    c = TraceAI::Auto::Config.new({}, env: {'FI_CONFIG_FILE' => file.path})
    assert_equal "escaped \"quote\" # text \\ A \u{1f600}", c['project_name']
    assert_equal ['wire', 'escaped\\slash', "line\nfeed"], c['instrumentations']
    assert_equal 'https://example.com', c['project_version_name']
    assert_equal 0.25, c['sample_rate']
    assert_equal 1024, c['wire_max_body_bytes']
    assert_nil c['invalid_array']; assert_nil c['invalid_string']; assert_nil c['invalid_type']
    refute c.truthy?('auto_debug')
  ensure
    file&.unlink
  end
  def test_project_name_fallback
    c = TraceAI::Auto::Config.new({}, env: {'OTEL_SERVICE_NAME' => 'otel-service'}, cwd: ROOT); assert_equal 'otel-service', c['project_name']
    c = TraceAI::Auto::Config.new({}, env: {}, cwd: ROOT); assert_equal 'ruby', c['project_name']; assert_equal 'observe', c['project_type']
  end
  def test_provider_order_and_all_rules
    assert_equal 'openai', W.provider('api.openai.com', 443)
    assert_equal 'aws.bedrock', W.provider('bedrock-runtime.us-east-1.amazonaws.com', 443)
    assert_nil W.provider('bedrock-runtime.malicious.example', 443)
    assert_equal 'ollama', W.provider('localhost', 11434)
    assert_nil W.match('POST', 'https://example.com/api/chat')
  end
  def test_incremental_multipart_discards_file_and_caps_fields
    require_relative '../lib/traceai/auto/upload'
    reader = TraceAI::Auto::MultipartReader.new('multipart/form-data; boundary=boundary')
    body = "--boundary\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n--boundary\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n" + ('z' * 70_000) + "\r\n--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"long.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + ('AUDIO' * 400_000) + "\r\n--boundary--\r\n"
    [1, 7, 16_384, 65_537].each do |chunk_size|
      reader = TraceAI::Auto::MultipartReader.new('multipart/form-data; boundary=boundary')
      offset = 0; max_buffer = 0
      while offset < body.bytesize
        size = offset < 1024 ? chunk_size : 16_384
        reader.feed(body.byteslice(offset, size)); offset += size
        max_buffer = [max_buffer, reader.instance_variable_get(:@buffer).bytesize].max
      end
      assert max_buffer < 32_768
      assert reader.complete; assert_equal 'whisper-1', reader.fields['model']; assert_equal 65_536, reader.fields['prompt'].bytesize
      assert_equal 2_000_000, reader.files.first[:size]; refute_includes reader.files.to_s, 'AUDIO'
    end
  end
  def test_bedrock_invoke_accepts_any_json
    [[1, 2], 'prompt', 7, false, nil].each do |body|
      req = request('/model/a/invoke', 'bedrock-runtime.us-east-1.amazonaws.com'); req[:body] = JSON.generate(body)
      result = W.build(req, response({'result' => 'ok'}))
      refute_nil result; assert_equal JSON.generate(body), result.attributes['input.value']
    end
  end
  def test_other_catalog_formats
    cases = [
      ['/v1/completions', 'api.openai.com', {model: 'm', prompt: 'hi'}, {choices: [{text: 'yes', finish_reason: 'stop'}]}, 'output.value', 'yes'],
      ['/v1/images/generations', 'api.openai.com', {model: 'm', prompt: 'hi'}, {data: [{b64_json: 'AAAA'}]}, 'output.value', '{"data":[{"b64_json":"<base64 omitted>"}]}'],
      ['/v1/moderations', 'api.openai.com', {input: 'hi'}, {results: [{flagged: true}]}, 'traceai.moderation.flagged', true],
      ['/v1beta/models/e:embedContent', 'generativelanguage.googleapis.com', {content: {parts: [{text: 'hi'}]}}, {embedding: {values: [1, 2]}}, 'gen_ai.embeddings.dimension.count', 2],
      ['/v2/embed', 'api.cohere.com', {texts: ['hi']}, {embeddings: {float: [[1, 2, 3]]}}, 'gen_ai.embeddings.dimension.count', 3],
      ['/api/embed', 'ollama.com', {input: ['hi']}, {embeddings: [[1, 2]]}, 'gen_ai.embeddings.dimension.count', 2],
      ['/api/generate', 'ollama.com', {prompt: 'hi'}, {response: 'yes', done: true, prompt_eval_count: 1, eval_count: 1}, 'output.value', 'yes'],
      ['/model/a%3Ab/invoke', 'bedrock-runtime.us-east-1.amazonaws.com', {prompt: 'hi'}, {output: 'yes'}, 'gen_ai.request.model', 'a:b']
    ]
    cases.each do |path, host, input, output, key, expected|
      result = W.build(request(path, host, input), response(output)); refute_nil result, path; assert_equal expected, result.attributes[key], path
    end
  end
end
