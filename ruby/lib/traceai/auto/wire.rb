# frozen_string_literal: true
require_relative 'codec'
require_relative 'privacy'
require_relative 'frames'
require_relative 'milestones'
module TraceAI
  module Auto
    module Wire
      MAX_BODY = 4 * 1024 * 1024
      CONTENT_KEYS = %w[messages input prompt contents system systemInstruction system_instruction instructions tools documents text texts inputs query transcript file audio_url url file_url image_url chat_history message preamble content requests].freeze
      CATALOG = Codec.parse(File.binread(File.join(__dir__, 'wire-protocols.json')))
      ENDPOINTS = CATALOG['endpoints'].map { |e| e.merge('regex' => Regexp.new(e['path'])) }.freeze
      Result = Struct.new(:name, :attributes, :ok, :description, keyword_init: true)
      module_function
      def header(headers, name)
        (headers || {}).find { |k, _| k.to_s.downcase == name }&.last.to_s
      end
      def content_type(headers)
        header(headers, 'content-type').split(';', 2).first.to_s.strip.downcase
      end
      def binary?(ctype)
        ctype.start_with?('audio/', 'video/', 'image/') || %w[application/octet-stream application/vnd.amazon.eventstream].include?(ctype)
      end
      def provider(host, port)
        rules = %w[hosts host_suffixes host_prefixes ports]
        matches = ->(p, key) do
          case key
          when 'hosts' then p[key].include?(host)
          when 'host_suffixes' then p[key].any? { |v| host.end_with?(v) }
          when 'host_prefixes' then p[key].any? { |v| host.start_with?(v) }
          when 'ports' then p[key].include?(port)
          end
        end
        rules.each do |key|
          CATALOG['providers'].each do |p|
            next unless p[key] && matches.call(p, key)
            next if p['match'] == 'all' && !rules.select { |r| p[r] }.all? { |r| matches.call(p, r) }
            return p['name']
          end
        end
        nil
      end
      URL = Struct.new(:scheme, :host, :port, :path, :query)
      def decode_component(value)
        value.tr('+', ' ').gsub(/%[0-9a-f]{2}/i) { |hex| hex[1..].to_i(16).chr }.force_encoding('UTF-8').scrub
      end
      def parse_url(url)
        found = /\A(https?):\/\/(?:[^\/?#@]*@)?(\[[^\]]+\]|[^\s\/:?#]+)(?::(\d+))?([^?#]*)(?:\?([^#]*))?(?:#.*)?\z/i.match(url)
        return nil unless found
        scheme, host, port, path, query = found.captures
        port = port ? Integer(port, 10) : (scheme.downcase == 'https' ? 443 : 80)
        return nil unless (1..65_535).cover?(port)
        URL.new(scheme.downcase, host.delete_prefix('[').delete_suffix(']'), port, path.empty? ? '/' : path, query.to_s)
      end
      def match(method, url)
        uri = parse_url(url)
        return nil unless uri
        host_provider = provider(uri.host.downcase, uri.port)
        ENDPOINTS.each do |endpoint|
          next unless endpoint['methods'].include?(method.upcase)
          found = endpoint['regex'].match(uri.path)
          next unless found
          p = host_provider || endpoint['default_provider'] || CATALOG['default_provider']
          next if endpoint['providers'] && !endpoint['providers'].include?(p)
          next if (endpoint['exclude_providers'] || []).include?(p)
          return {endpoint: endpoint, provider: p, known: !host_provider.nil?, host: uri.host.downcase, port: uri.port,
                  path: uri.path, query: uri.query.split('&').to_h { |part| key, value = part.split('=', 2); [decode_component(key), decode_component(value.to_s)] },
                  groups: found.named_captures.transform_values { |v| v.to_s.gsub(/%[0-9a-f]{2}/i) { |hex| hex[1..].to_i(16).chr }.force_encoding('UTF-8').scrub }}
        end
        nil
      rescue ArgumentError
        nil
      end
      def json(data)
        Codec.parse(data) if data && !data.empty?
      rescue Codec::ParserError, Codec::NestingError, EncodingError
        nil
      end
      def text(value)
        case value
        when String then value
        when Hash then value['text'].is_a?(String) ? value['text'] : text(value['parts'])
        when Array
          parts = value.filter_map do |v|
            if v.is_a?(String)
              v
            elsif v.is_a?(Hash) && [nil, 'text', 'input_text', 'output_text'].include?(v['type']) && v['text'].is_a?(String)
              v['text']
            end
          end
          parts.join("\n") unless parts.empty?
        end
      end
      def args(value)
        value.is_a?(String) || value.nil? ? value : Codec.generate(value)
      end
      def msg(role = 'user', content = nil, calls = [], tool_id = nil)
        {role: role, content: content.is_a?(String) ? content.dup : content, calls: calls, tool_id: tool_id}
      end
      def call(id, name, arguments)
        {id: id, name: name, arguments: args(arguments)&.dup}
      end
      def openai_message(raw)
        msg(raw['role'] || 'user', text(raw['content']), (raw['tool_calls'] || []).map { |c| call(c['id'], c.dig('function', 'name'), c.dig('function', 'arguments')) }, raw['tool_call_id'])
      end
      def anthropic_message(raw)
        content = raw['content']
        return msg(raw['role'] || 'user', content) if content.is_a?(String)
        out = msg(raw['role'] || 'user')
        parts = []
        Array(content).each do |b|
          next unless b.is_a?(Hash)
          case b['type']
          when 'text' then parts << b['text'] if b['text'].is_a?(String)
          when 'tool_use' then out[:calls] << call(b['id'], b['name'], b['input'])
          when 'tool_result'
            out[:tool_id] = b['tool_use_id']
            parts << text(b['content']) if text(b['content'])
          end
        end
        out[:content] = parts.join("\n") unless parts.empty?
        out
      end
      def gemini_message(raw)
        out = msg(raw['role'] == 'model' ? 'assistant' : (raw['role'] || 'user'))
        texts = []
        Array(raw['parts']).each do |part|
          if part['text'].is_a?(String) && !part['thought']
            texts << part['text']
          elsif part['functionCall'].is_a?(Hash)
            c = part['functionCall']; out[:calls] << call(c['id'], c['name'], c['args'])
          elsif part['functionResponse'].is_a?(Hash)
            r = part['functionResponse']; out[:role] = 'tool'; out[:tool_id] = r['id'] || r['name']; texts << args(r['response']).to_s
          end
        end
        out[:content] = texts.join("\n") unless texts.empty?
        out
      end
      def bedrock_message(raw)
        out = msg(raw['role'] || 'user'); texts = []
        Array(raw['content']).each do |b|
          if b['text'].is_a?(String)
            texts << b['text']
          elsif b['toolUse']
            c = b['toolUse']; out[:calls] << call(c['toolUseId'], c['name'], c['input'])
          elsif b['toolResult']
            r = b['toolResult']; out[:tool_id] = r['toolUseId']; texts.concat(Array(r['content']).filter_map { |v| text(v) })
          end
        end
        out[:content] = texts.join("\n") unless texts.empty?
        out
      end
      def utf8(value, limit)
        value&.byteslice(0, limit)&.force_encoding('UTF-8')&.scrub
      end
      # Scan a binary view so String#index and byteslice use the same offsets;
      # decode only bounded metadata, never alter the application's body.
      def multipart(body, ctype)
        body = body.b
        boundary = ctype[/boundary="?([^";]+)"?/i, 1]
        return [{}, []] unless boundary
        marker = "--#{boundary}".b
        fields = {}; files = []; pos = body.index(marker)
        128.times do
          break unless pos
          pos += marker.bytesize
          break if body.byteslice(pos, 2) == '--'
          pos += 2 if body.byteslice(pos, 2) == "\r\n"
          last = body.index("\r\n#{marker}", pos)
          break unless last
          head_end = body.index("\r\n\r\n", pos)
          if head_end && head_end < last && head_end - pos <= 8192
            head = body.byteslice(pos, head_end - pos)
            name = head[/(?:^|[;\s])name="([^"]*)"/i, 1]
            if name
              name = utf8(name, 256); start = head_end + 4
              filename = utf8(head[/filename="([^"]*)"/i, 1], 1024)
              part_type = utf8(head[/content-type:\s*([^\r\n;]+)/i, 1], 256)
              if filename || binary?(part_type.to_s.downcase)
                files << {name: name, filename: filename, content_type: part_type, size: last - start}
              else
                fields[name] = body.byteslice(start, [last - start, 65_536].min).force_encoding('UTF-8').scrub
              end
            end
          end
          pos = last + 2
        end
        [fields, files]
      end
      # Incremental inflate's block form avoids allocating an unbounded expanded string.
      def decode(data, encoding, limit)
        return [data, false, nil] if encoding.empty? || encoding == 'identity'
        return [nil, false, encoding] unless %w[gzip deflate].include?(encoding)
        @inflater ||= ObjectSpace.each_object(Class).find { |klass| Module.instance_method(:name).bind_call(klass) == 'Zlib::Inflate' }
        return [nil, false, encoding] unless @inflater
        windows = encoding == 'gzip' ? [31] : [15, -15]
        windows.each do |window|
          z = @inflater.new(window); out = ''.b
          begin
            z.inflate(data) do |part|
              return [nil, true, nil] if out.bytesize + part.bytesize > limit
              out << part
            end
            return [out, false, nil]
          rescue StandardError
            next
          ensure
            z.close
          end
        end
        [nil, false, encoding]
      end
      def response_body(response, limit)
        ctype = content_type(response[:headers])
        return {kind: :binary} if binary?(ctype)
        data = response[:body]
        return {kind: :empty} if response[:truncated] || data.nil?
        unless response[:decoded]
          encoding = header(response[:headers], 'content-encoding').downcase
          response[:arrival_unavailable] = true unless ['', 'identity'].include?(encoding)
          data, exceeded, encoding = decode(data, encoding, limit)
          response[:truncated] ||= exceeded
          response[:undecoded_encoding] = encoding
          return {kind: :empty} unless data
        end
        if ctype == 'text/event-stream' || ctype.include?('ndjson') || ctype == 'application/jsonl'
          kind = ctype == 'text/event-stream' ? :sse : :ndjson
          frames = Frames.parse(data, kind)
          return {kind: kind, events: frames.map(&:first), frames: frames}
        end
        parsed = json(data.dup.force_encoding('UTF-8'))
        frames = Frames.parse(data, :json)
        if !parsed.nil? || !frames.empty?
          parsed ||= data.lstrip.start_with?('[') ? frames.map(&:first) : frames.first&.first
          return {kind: :json, json: parsed, frames: frames}
        end
        if data.lines.count > 1
          frames = Frames.parse(data, :ndjson)
          return {kind: :ndjson, events: frames.map(&:first), frames: frames} unless frames.empty?
        end
        {kind: :text, text: data.dup.force_encoding('UTF-8').scrub}
      end
      def build(request, response, limit: MAX_BODY)
        Privacy.observe_request(request[:headers] || {}, request[:url])
        m = match(request[:method], request[:url]); return nil unless m
        Parser.new(m, request, response, limit).build
      end
      class Parser
        def initialize(m, req, resp, limit)
          @m, @req, @resp, @limit = m, req, resp, limit
          @endpoint = m[:endpoint]; @format = @endpoint['format']
          @attrs = {}; @in = []; @out = []; @tools = []; @params = {}; @usage = {}; @finish = []
        end
        def put(key, value)
          return if value.nil? || value == '' || value == []
          value = Codec.generate(value) if value.is_a?(Hash) || (value.is_a?(Array) && !value.all? { |v| v.is_a?(String) })
          @attrs[key] = value
        end
        def usage(values)
          values.each { |k, v| @usage[k] = v.to_i if v.is_a?(Numeric) }
        end
        def common(body)
          %w[temperature top_p frequency_penalty presence_penalty seed].each { |k| @params[k] = body[k] if body[k].is_a?(Numeric) }
          k = %w[max_tokens max_completion_tokens max_output_tokens].find { |v| body[v].is_a?(Numeric) }
          @params['max_tokens'] = body[k] if k
        end
        def validate(body)
          case @format
          when 'openai.chat', 'ollama.chat', 'cohere.chat'
            if body['messages'].is_a?(Array)
              @in = body['messages'].grep(Hash).map { |r| Wire.openai_message(r) }
            elsif @format == 'cohere.chat' && body['message'].is_a?(String)
              @in << Wire.msg('system', body['preamble']) if body['preamble'].is_a?(String)
              Array(body['chat_history']).each { |r| @in << Wire.msg({'USER' => 'user', 'CHATBOT' => 'assistant', 'SYSTEM' => 'system'}[r['role'].to_s.upcase] || 'user', Wire.text(r['message'])) }
              @in << Wire.msg('user', body['message'])
            elsif @format == 'openai.chat' && body['prompt'].is_a?(String)
              @in << Wire.msg('user', body['prompt'])
            else return false
            end
            @tools = body['tools'] || []; common(body)
            @params['top_p'] = body['p'] if body['p'].is_a?(Numeric)
          when 'openai.completions', 'ollama.generate'
            return false unless body.key?('prompt')
            @in << Wire.msg('system', body['system']) if body['system'].is_a?(String)
            @in << Wire.msg('user', body['prompt'].is_a?(Array) ? body['prompt'].grep(String).join("\n") : Wire.text(body['prompt']))
            common(body)
          when 'openai.responses'
            return false unless body.key?('input') || body.key?('model')
            @in << Wire.msg('system', body['instructions']) if body['instructions'].is_a?(String)
            if body['input'].is_a?(String)
              @in << Wire.msg('user', body['input'])
            elsif body['input'].is_a?(Array)
              body['input'].grep(Hash).each do |r|
                if r['type'] == 'function_call_output'
                  @in << Wire.msg('tool', Wire.text(r['output']) || Wire.args(r['output']), [], r['call_id'])
                elsif r['type'] == 'function_call'
                  @in << Wire.msg('assistant', nil, [Wire.call(r['call_id'], r['name'], r['arguments'])])
                elsif r['role']
                  @in << Wire.msg(r['role'], Wire.text(r['content']))
                end
              end
            end
            @tools = body['tools'] || []; common(body)
          when 'openai.embeddings', 'ollama.embed', 'cohere.embed', 'gemini.embed_content'
            value = case @format
                    when 'cohere.embed' then body['texts'] || body['inputs']
                    when 'gemini.embed_content' then body['requests'] || body['content']
                    when 'ollama.embed' then body['input'] || body['prompt']
                    else body['input']
                    end
            return false if value.nil?
            count = value.is_a?(Array) && !value.first.is_a?(Integer) ? value.length : 1
            put('gen_ai.request.embedding_inputs_count', count)
          when 'openai.audio_speech', 'deepgram.speak', 'elevenlabs.tts', 'cartesia.tts'
            key = {'openai.audio_speech' => 'input', 'cartesia.tts' => 'transcript'}[@format] || 'text'
            return false unless body[key].is_a?(String)
            @input, @input_mime = body[key], 'text/plain'
            put('gen_ai.audio.transcript', @input); put('gen_ai.request.voice', body['voice']) if body['voice'].is_a?(String) || !body['voice']
            put('gen_ai.audio.transcript', @input)
          when 'openai.images'
            return false unless body['prompt'].is_a?(String)
            @in << Wire.msg('user', body['prompt'])
          when 'openai.moderations'
            return false unless body.key?('input')
          when 'rerank'
            return false unless body['query'].is_a?(String) && body['documents'].is_a?(Array)
            @in << Wire.msg('user', body['query']); put('gen_ai.request.top_n', body['top_n'] || body['top_k']); put('gen_ai.rerank.documents_count', body['documents'].length)
          when 'anthropic.messages', 'bedrock.invoke'
            return false if @format == 'anthropic.messages' && !body['messages'].is_a?(Array)
            @in << Wire.msg('system', Wire.text(body['system'])) if body['system']
            @in.concat(Array(body['messages']).grep(Hash).map { |r| Wire.anthropic_message(r) })
            @tools = body['tools'] || []; common(body)
          when 'gemini.generate_content'
            return false unless body.key?('contents')
            system = body['systemInstruction'] || body['system_instruction']; @in << Wire.msg('system', Wire.text(system)) if system
            c = body['contents']
            @in.concat(c.is_a?(String) ? [Wire.msg('user', c)] : (c.is_a?(Array) ? c : [c]).grep(Hash).map { |r| Wire.gemini_message(r) })
            @tools = body['tools'] || []
            conf = body['generationConfig'] || body['generation_config'] || {}
            {'temperature' => 'temperature', 'topP' => 'top_p', 'maxOutputTokens' => 'max_tokens', 'seed' => 'seed', 'frequencyPenalty' => 'frequency_penalty', 'presencePenalty' => 'presence_penalty'}.each { |s, d| @params[d] = conf[s] if conf[s].is_a?(Numeric) }
          when 'bedrock.converse'
            return false unless body['messages'].is_a?(Array)
            @in << Wire.msg('system', Wire.text(body['system'])) if body['system']
            @in.concat(body['messages'].grep(Hash).map { |r| Wire.bedrock_message(r) })
            {'temperature' => 'temperature', 'topP' => 'top_p', 'maxTokens' => 'max_tokens'}.each { |s, d| @params[d] = body.dig('inferenceConfig', s) if body.dig('inferenceConfig', s).is_a?(Numeric) }
            @tools = body.dig('toolConfig', 'tools') || []
          end
          if @format.start_with?('ollama.')
            {'temperature' => 'temperature', 'top_p' => 'top_p', 'num_predict' => 'max_tokens', 'seed' => 'seed'}.each { |s, d| @params[d] = body.dig('options', s) if body.dig('options', s).is_a?(Numeric) }
          end
          true
        end
        def request
          body = @req[:body]; fields = @req[:fields]; files = @req[:files] || []
          ctype = Wire.content_type(@req[:headers])
          multipart = %w[openai.audio_transcription elevenlabs.stt].include?(@format)
          # Multipart bypasses the body limit: retain text metadata only, never file bytes.
          if multipart && body && ctype.start_with?('multipart/')
            fields, files = Wire.multipart(body, Wire.header(@req[:headers], 'content-type')); body = nil
          elsif body && body.bytesize > @limit && !Wire.binary?(ctype)
            @req[:truncated] = true; body = nil
          end
          Privacy.observe_body_credentials(fields, @req[:session_id]) if fields
          unavailable = @req[:unavailable] || @req[:truncated] || (body.nil? && fields.nil? && @req[:method] != 'GET' && !@req[:binary])
          if unavailable
            return false unless @m[:known]
            @req[:unavailable] = true unless @req[:truncated]
          elsif multipart
            fields = (fields || {}).to_h { |k, v| [Wire.utf8(k.to_s, 256), Wire.utf8(v.to_s, 65_536)] }
            model_key = @format == 'elevenlabs.stt' ? 'model_id' : 'model'
            return false if fields[model_key].to_s.empty?
            @request_model = fields[model_key]
            put('gen_ai.audio.language', fields['language'] || fields['language_code'])
            @json_params = fields.reject { |k, _| CONTENT_KEYS.include?(k) || k == model_key }
            described = fields.dup
            files.each { |f| described[f[:name]] = {filename: f[:filename], content_type: f[:content_type], bytes: f[:size]} }
            @input, @input_mime = Codec.generate(described), 'application/json'
          elsif Wire.binary?(ctype) || @req[:binary]
            return false unless %w[deepgram.listen assemblyai.transcript].include?(@format)
            put('gen_ai.audio.mime_type', ctype); put('traceai.audio.input_bytes', @req[:body_size] || body&.bytesize)
          elsif body
            parsed = Wire.json(body)
            Privacy.observe_body_credentials(parsed, @req[:session_id]) unless parsed.nil?
            if @format == 'bedrock.invoke' && (!parsed.nil? || body.strip == 'null') && !parsed.is_a?(Hash)
              @input, @input_mime = Codec.generate(parsed), 'application/json'
            else
              return false unless parsed.is_a?(Hash)
              return false unless validate(parsed)
              @request_model = %w[model model_id speech_model modelId].filter_map { |k| parsed[k] if parsed[k].is_a?(String) }.first
              @input ||= Codec.generate(parsed); @input_mime ||= 'application/json'
              @json_params = parsed.reject { |k, _| CONTENT_KEYS.include?(k) }
              @request_stream = parsed['stream'] == true || (%w[ollama.chat ollama.generate].include?(@format) && parsed['stream'] != false)
            end
          elsif !%w[assemblyai.transcript deepgram.listen].include?(@format)
            return false
          end
          @request_model ||= @m[:groups]['model'] || @m[:query]['model']
          put('gen_ai.audio.language', @m[:query]['language']) if @format == 'deepgram.listen'
          put('gen_ai.request.voice', @m[:groups]['voice'])
          if files.first
            put('gen_ai.audio.mime_type', files.first[:content_type]); put('traceai.audio.input_bytes', files.first[:size])
          end
          true
        end
        # Merge cumulative snapshots before normalizing once. Explicit null/zero
        # replaces prior values; absent optional counters are not emitted as zero.
        def merge_usage(source, target = (@raw_usage ||= {}))
          return unless source.is_a?(Hash)
          source.each do |key, value|
            if value.is_a?(Hash)
              target[key] = {} unless target[key].is_a?(Hash)
              merge_usage(value, target[key])
            else
              target[key] = value
            end
          end
        end
        def openai_usage(u) = merge_usage(u)
        def anthropic_usage(u) = merge_usage(u)
        def token_number(raw, key, optional = false)
          return optional ? 0 : nil unless raw.key?(key)
          value = raw[key]
          value.is_a?(Integer) && value >= 0 ? value : nil
        end
        def inclusive(raw, base, *additions)
          values = [token_number(raw, base)] + additions.map { |key| token_number(raw, key, true) }
          values.any?(&:nil?) ? nil : values.sum
        end
        def normalize_usage
          raw = @raw_usage
          if raw
            total = token_number(raw, 'total_tokens')
            case @format
            when 'anthropic.messages', 'bedrock.invoke'
              usage_values = {input: inclusive(raw, 'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'),
                              output: token_number(raw, 'output_tokens'), cache_read: token_number(raw, 'cache_read_input_tokens'),
                              cache_write: token_number(raw, 'cache_creation_input_tokens')}
            when 'bedrock.converse'
              total = token_number(raw, 'totalTokens')
              usage_values = {input: inclusive(raw, 'inputTokens', 'cacheReadInputTokens', 'cacheWriteInputTokens'),
                              output: token_number(raw, 'outputTokens'), cache_read: token_number(raw, 'cacheReadInputTokens'),
                              cache_write: token_number(raw, 'cacheWriteInputTokens')}
            when 'gemini.generate_content'
              total = token_number(raw, 'totalTokenCount')
              usage_values = {input: inclusive(raw, 'promptTokenCount', 'toolUsePromptTokenCount'),
                              output: inclusive(raw, 'candidatesTokenCount', 'thoughtsTokenCount'),
                              cache_read: token_number(raw, 'cachedContentTokenCount'), reasoning: token_number(raw, 'thoughtsTokenCount')}
            else
              ikey, okey = raw.key?('prompt_tokens') || raw.key?('completion_tokens') ? %w[prompt_tokens completion_tokens] : %w[input_tokens output_tokens]
              input_details = raw[ikey + '_details']; output_details = raw[okey + '_details']
              input_details = {} unless input_details.is_a?(Hash)
              output_details = {} unless output_details.is_a?(Hash)
              usage_values = {input: token_number(raw, ikey), output: token_number(raw, okey),
                              cache_read: token_number(input_details, 'cached_tokens'), reasoning: token_number(output_details, 'reasoning_tokens'),
                              input_audio: token_number(input_details, 'audio_tokens'), output_audio: token_number(output_details, 'audio_tokens')}
            end
            usage(usage_values)
            @usage[:total] = total unless total.nil?
          end
          if @usage.key?(:input) && @usage.key?(:output)
            total = @usage[:input] + @usage[:output]
            @usage[:provider_total] = @usage[:total] if @usage.key?(:total) && @usage[:total] != total
            @usage[:total] = total
          end
        end
        def transcript(value)
          return unless value.is_a?(String)
          @output, @output_mime = value, 'text/plain'; put('gen_ai.audio.transcript', value)
        end
        # Terminal Responses events are authoritative snapshots. Without one,
        # retain received tool items and argument fragments, including on abort.
        def responses_deltas(events, out)
          calls = {}; indices = {}
          out[:content] = events.filter_map { |e| e['delta'] if e['type'] == 'response.output_text.delta' }.join
          events.each do |event|
            item = event['item'] || {}
            index = event['output_index'] || indices[event['item_id']] || calls.length
            case event['type']
            when 'response.output_item.added', 'response.output_item.done'
              next unless item['type'] == 'function_call'
              indices[item['id']] = index if item['id']
              c = (calls[index] ||= Wire.call(nil, nil, ''))
              c[:id] = item['call_id'] if item['call_id']
              c[:name] = item['name'] if item['name']
              c[:arguments] = Wire.args(item['arguments']).to_s.dup if item.key?('arguments')
            when 'response.function_call_arguments.delta', 'response.function_call_arguments.done'
              c = (calls[index] ||= Wire.call(nil, nil, ''))
              if event['type'].end_with?('.done')
                c[:arguments] = Wire.args(event['arguments']).to_s.dup if event.key?('arguments')
              else
                c[:arguments] << event['delta'].to_s
              end
            end
          end
          out[:calls] = calls.sort.map(&:last)
        end
        def response
          d = @body[:json]; d = {} unless d.is_a?(Hash)
          events = @body[:events] || []
          case @format
          when 'openai.chat', 'openai.completions'
            if @body[:kind] == :json
              @response_id, @response_model = d['id'], d['model']
              Array(d['choices']).each do |c|
                @out << (c['message'] ? Wire.openai_message(c['message']) : Wire.msg('assistant', c['text']))
                @finish << c['finish_reason'] if c['finish_reason']
              end
              openai_usage(d['usage'])
            else
              slots = {}; finish = {}
              events.each do |e|
                @response_id ||= e['id']; @response_model ||= e['model']; openai_usage(e['usage'])
                Array(e['choices']).each do |c|
                  i = c['index'] || 0; delta = c['delta'] || {}; s = (slots[i] ||= Wire.msg('assistant', ''))
                  s[:role] = delta['role'] if delta['role']; s[:content] << (Wire.text(delta['content']) || c['text'] || '')
                  calls = (s[:indexed_calls] ||= {})
                  Array(delta['tool_calls']).each do |tc|
                    cc = (calls[tc['index'] || 0] ||= Wire.call(nil, nil, '')); f = tc['function'] || {}
                    cc[:id] ||= tc['id']
                    cc[:name] = cc[:name].to_s + f['name'] if f['name'].is_a?(String)
                    cc[:arguments] << f['arguments'].to_s
                  end
                  finish[i] = c['finish_reason'] if c['finish_reason']
                end
              end
              @out = slots.sort.map { |_, s| s[:calls] = (s[:indexed_calls] || {}).sort.map(&:last); s }
              @finish = finish.sort.map(&:last)
            end
          when 'openai.responses'
            final = events.reverse.find { |e| %w[response.completed response.incomplete response.failed].include?(e['type']) }
            d = final['response'] if final
            created = events.find { |e| e['type'] == 'response.created' }; created = created && created['response']
            @response_id = d['id'] || (created && created['id']); @response_model = d['model'] || (created && created['model'])
            out = Wire.msg('assistant', ''); Array(d['output']).each do |item|
              if item['type'] == 'message'
                out[:content] << Array(item['content']).filter_map { |p| p['text'] if p['type'] == 'output_text' && p['text'].is_a?(String) }.join("\n")
              elsif item['type'] == 'function_call'
                out[:calls] << Wire.call(item['call_id'], item['name'], item['arguments'])
              end
            end
            responses_deltas(events, out) if !final && @body[:kind] == :sse
            @out << out; @finish = [d['status']] if d['status']
            events.each { |event| openai_usage(event.dig('response', 'usage')) }
            openai_usage(d['usage']) if events.empty?
          when 'openai.embeddings'
            @response_model = d['model']; dimension(d.dig('data', 0, 'embedding')); openai_usage(d['usage'])
          when 'openai.audio_transcription'
            if @body[:kind] == :text
              transcript(@body[:text])
            else
              done = events.find { |e| e['type'] == 'transcript.text.done' }
              d = done || {'text' => events.filter_map { |e| e['delta'] if e['type'] == 'transcript.text.delta' }.join} if @body[:kind] == :sse
              transcript(d['text']); put('gen_ai.audio.language', d['language']); put('gen_ai.audio.duration_secs', d['duration']); openai_usage(d['usage'])
            end
          when 'openai.audio_speech', 'deepgram.speak', 'elevenlabs.tts', 'cartesia.tts'
            if d['audio_base64'].is_a?(String)
              put('traceai.audio.output_bytes', base64_size(d['audio_base64']))
            elsif @body[:kind] == :sse
              put('traceai.audio.output_bytes', events.sum { |e| e['type'].nil? || e['type'] == 'chunk' ? base64_size(e['data'].to_s) : 0 })
            end
          when 'openai.images'
            if @body[:kind] == :json
              Array(d['data']).each { |r| r['b64_json'] = '<base64 omitted>' if r.key?('b64_json') }
              @output, @output_mime = Codec.generate(d), 'application/json'; openai_usage(d['usage'])
            end
          when 'openai.moderations'
            @response_id, @response_model = d['id'], d['model']; put('traceai.moderation.flagged', Array(d['results']).any? { |r| r['flagged'] })
            @output, @output_mime = Codec.generate(d), 'application/json'
          when 'rerank'
            @response_id, @response_model = d['id'], d['model']
            @output = Codec.generate(Array(d['results'] || d['data']).map { |r| {index: r['index'], relevance_score: r['relevance_score']} }); @output_mime = 'application/json'
            usage(total: d.dig('usage', 'total_tokens'))
          when 'anthropic.messages', 'bedrock.invoke'
            if @body[:kind] == :json && d['content']
              @response_id, @response_model = d['id'], d['model']; @out = [Wire.anthropic_message(d.merge('role' => d['role'] || 'assistant'))]
              @finish = [d['stop_reason']] if d['stop_reason']; anthropic_usage(d['usage'])
            elsif %i[sse ndjson].include?(@body[:kind])
              blocks = {}
              events.each do |e|
                case e['type']
                when 'message_start'
                  s = e['message'] || {}; @response_id, @response_model = s['id'], s['model']; anthropic_usage(s['usage'])
                when 'content_block_start'
                  b = (e['content_block'] || {}).dup; b['_parts'] = b['type'] == 'text' ? b['text'].to_s.dup : +''; blocks[e['index'] || blocks.length] = b
                when 'content_block_delta'
                  b = (blocks[e['index'] || 0] ||= {'type' => 'text', '_parts' => ''}); delta = e['delta'] || {}; b['_parts'] << (delta['text'] || delta['partial_json'] || '')
                when 'message_delta'
                  reason = e.dig('delta', 'stop_reason'); @finish = [reason] if reason; anthropic_usage(e['usage'])
                end
              end
              out = Wire.msg('assistant', '')
              blocks.sort.each do |_, b|
                out[:content] << b['_parts'] if b['type'] == 'text'
                out[:calls] << Wire.call(b['id'], b['name'], b['_parts'].empty? ? b['input'] : b['_parts']) if b['type'] == 'tool_use'
              end
              @out = [out]
            elsif @format == 'bedrock.invoke' && @body[:kind] == :json
              @output, @output_mime = Codec.generate(@body[:json]), 'application/json'
            end
          when 'gemini.generate_content'
            items = @body[:kind] == :json ? (@body[:json].is_a?(Array) ? @body[:json] : [d]) : events
            out = Wire.msg('assistant', '')
            items.grep(Hash).each do |item|
              @response_model = item['modelVersion'] || @response_model; @response_id = item['responseId'] || @response_id
              candidates = item['candidates'] || []
              if candidates.first
                part = Wire.gemini_message(candidates.first['content'] || {}); out[:content] << part[:content].to_s; out[:calls].concat(part[:calls])
                reasons = candidates.filter_map { |c| c['finishReason'] }; @finish = reasons unless reasons.empty?
              end
              merge_usage(item['usageMetadata'])
            end
            @out = [out]
          when 'gemini.embed_content'
            emb = d['embedding'] || Array(d['embeddings']).first || {}; dimension(emb['values'])
          when 'cohere.chat'
            if %i[sse ndjson].include?(@body[:kind])
              out = Wire.msg('assistant', ''); calls = {}
              events.each do |event|
                delta = event['delta'] || {}
                case event['type'] || event['event_type']
                when 'message-start'
                  message = delta['message'] || {}
                  @response_id = message['id'] || event['id'] || @response_id
                  out[:role] = message['role'] if message['role']
                when 'content-delta'
                  content = delta.dig('message', 'content')
                  out[:content] << (Wire.text(content) || '')
                when 'tool-call-start', 'tool-call-delta'
                  tools = delta.dig('message', 'tool_calls')
                  tools = [tools] if tools.is_a?(Hash)
                  Array(tools).grep(Hash).each do |tool|
                    index = tool['index'] || event['index'] || 0
                    c = (calls[index] ||= Wire.call(nil, nil, ''))
                    function = tool['function'] || {}
                    c[:id] ||= tool['id']
                    c[:name] = c[:name].to_s + function['name'] if function['name'].is_a?(String)
                    c[:arguments] << Wire.args(function['arguments']).to_s
                  end
                when 'message-end'
                  @finish = [delta['finish_reason']] if delta['finish_reason']
                  u = delta.dig('usage', 'tokens') || {}
                  usage(input: u['input_tokens'], output: u['output_tokens'])
                when 'stream-start'
                  @response_id = event['generation_id'] || @response_id
                when 'text-generation'
                  out[:content] << event['text'].to_s
                when 'stream-end'
                  final = event['response'] || {}
                  @response_id = final['generation_id'] || final['id'] || @response_id
                  out[:content] = final['text'].dup if final['text'].is_a?(String)
                  reason = event['finish_reason'] || final['finish_reason']; @finish = [reason] if reason
                  u = final.dig('meta', 'billed_units') || {}
                  usage(input: u['input_tokens'], output: u['output_tokens'])
                end
              end
              out[:calls] = calls.sort.map(&:last)
              @out << out
            else
              @response_id = d['id'] || d['generation_id']; @out << (d['message'].is_a?(Hash) ? Wire.openai_message(d['message'].merge('role' => d['message']['role'] || 'assistant')) : Wire.msg('assistant', d['text']))
              @finish = [d['finish_reason']] if d['finish_reason']; u = d.dig('usage', 'tokens') || d.dig('meta', 'billed_units') || {}; usage(input: u['input_tokens'], output: u['output_tokens'])
            end
          when 'cohere.embed'
            e = d['embeddings']; e = e['float'] if e.is_a?(Hash); dimension(e&.first); usage(input: d.dig('meta', 'billed_units', 'input_tokens'))
          when 'bedrock.converse'
            @out << Wire.bedrock_message(d.dig('output', 'message')) if d.dig('output', 'message').is_a?(Hash)
            @finish = [d['stopReason']] if d['stopReason']; merge_usage(d['usage'])
          when 'ollama.chat', 'ollama.generate'
            out = Wire.msg('assistant', '')
            (@body[:kind] == :json ? [d] : events).each do |item|
              @response_model = item['model'] || @response_model; out[:content] << (item.dig('message', 'content') || item['response'] || '')
              if item['message'].is_a?(Hash)
                out[:calls].concat(Wire.openai_message(item['message'])[:calls])
              end
              if item['done']
                @finish = [item['done_reason']] if item['done_reason']; usage(input: item['prompt_eval_count'], output: item['eval_count'])
              end
            end
            @out = [out]
          when 'ollama.embed'
            dimension(d.dig('embeddings', 0) || d['embedding']); usage(input: d['prompt_eval_count'])
          when 'deepgram.listen'
            @response_id = d.dig('metadata', 'request_id'); put('gen_ai.audio.duration_secs', d.dig('metadata', 'duration')); transcript(d.dig('results', 'channels', 0, 'alternatives', 0, 'transcript'))
          when 'elevenlabs.stt'
            transcript(d['text']); put('gen_ai.audio.language', d['language_code']); @response_id = d['transcription_id']
          when 'assemblyai.transcript'
            @response_id, @response_model = d['id'], d['speech_model']; @finish = [d['status']] if d['status']
            transcript(d['text']) if d['status'] == 'completed'; put('gen_ai.audio.duration_secs', d['audio_duration']); put('gen_ai.audio.language', d['language_code'])
          end
        end
        def dimension(vector)
          put('gen_ai.embeddings.dimension.count', vector.length) if vector.is_a?(Array)
        end
        def base64_size(value)
          # Count without materializing decoded audio.
          n = value.count('A-Za-z0-9+/_-'); (n * 6) / 8
        end
        def messages(prefix, messages)
          messages.each_with_index do |m, i|
            base = "#{prefix}.#{i}.message"; put("#{base}.role", m[:role]); put("#{base}.content", m[:content]); put("#{base}.tool_call_id", m[:tool_id])
            m[:calls].each_with_index do |c, k|
              b = "#{base}.tool_calls.#{k}.tool_call"; put("#{b}.id", c[:id]); put("#{b}.function.name", c[:name]); put("#{b}.function.arguments", c[:arguments])
            end
          end
        end
        def build
          return nil unless request
          @body = Wire.response_body(@resp, @limit)
          if @endpoint['only_when'] == 'completed'
            return nil unless @body[:json].is_a?(Hash) && %w[completed error].include?(@body[:json]['status'])
          end
          ok = @resp[:error].nil? && @resp[:status].to_i < 400
          begin
            response if ok || @resp[:error]
          rescue StandardError
            # Provider shape changes never affect the host, retain request/status.
          end
          @attrs.merge!('gen_ai.span.kind' => @endpoint['span_kind'], 'gen_ai.operation.name' => @endpoint['operation'], 'gen_ai.provider.name' => @m[:provider],
                        'traceai.capture.layer' => 'wire', 'traceai.wire.endpoint' => @endpoint['id'], 'server.address' => @m[:host], 'server.port' => @m[:port], 'http.request.method' => @req[:method].upcase)
          put('http.response.status_code', @resp[:status]); put('gen_ai.request.model', @request_model); put('gen_ai.response.model', @response_model); put('gen_ai.response.id', @response_id); put('gen_ai.response.finish_reasons', @finish)
          @params.each { |k, v| put("gen_ai.request.#{k}", v) }; put('gen_ai.request.parameters', @json_params) unless @json_params.nil? || @json_params.empty?
          if %i[sse ndjson].include?(@body[:kind]) || @request_stream || @m[:path].end_with?(':streamGenerateContent', ':streamRawPredict', '/converse-stream', '/invoke-with-response-stream')
            put('gen_ai.request.stream', true); put('gen_ai.response.time_to_first_chunk', @resp[:first_chunk_seconds]&.round(6))
            if @resp[:arrival_unavailable] || @resp[:arrivals].nil?
              @resp[:incomplete] = true if @resp[:size].to_i.positive?
            else
              first_text = Milestones.first_text(@format, @body[:frames] || [], @resp[:arrivals])
              put('gen_ai.response.time_to_first_text', first_text&.round(6))
            end
          end
          normalize_usage
          {input: 'input_tokens', output: 'output_tokens', total: 'total_tokens', provider_total: 'provider_total_tokens', cache_read: 'cache_read_tokens', cache_write: 'cache_write_tokens', reasoning: 'output_tokens.reasoning', input_audio: 'input_tokens.audio', output_audio: 'output_tokens.audio'}.each { |k, dest| put("gen_ai.usage.#{dest}", @usage[k]) }
          messages('gen_ai.input.messages', @in); messages('gen_ai.output.messages', @out)
          @tools.each_with_index { |tool, i| put("gen_ai.tool.definitions.#{i}.tool.json_schema", Codec.generate(tool)) }
          put('input.value', @input); put('input.mime_type', @input_mime) if @input
          if @output.nil? && @out.first
            out = @out.first
            if out[:content] && !out[:content].empty?
              @output, @output_mime = out[:content], 'text/plain'
            elsif !out[:calls].empty?
              @output, @output_mime = Codec.generate(out[:calls]), 'application/json'
            end
          end
          put('output.value', @output); put('output.mime_type', @output_mime) if @output
          if @body[:kind] == :binary && ok
            put('gen_ai.audio.mime_type', Wire.content_type(@resp[:headers]))
            put('traceai.audio.output_bytes', @resp[:size]) if Wire.content_type(@resp[:headers]).start_with?('audio/') || @endpoint['operation'] == 'text_to_speech'
          end
          put('traceai.wire.body_unavailable', true) if @req[:unavailable]
          put('traceai.wire.truncated', true) if @req[:truncated] || @resp[:truncated]
          put('traceai.wire.incomplete', true) if @resp[:incomplete]
          put('traceai.wire.undecoded_encoding', @resp[:undecoded_encoding])
          description = nil
          unless ok
            error = @resp[:error]
            if error
              put('error.type', error.class.name); description = error.message
            else
              put('error.type', @resp[:status].to_s)
              d = @body[:json]; d = d.first if d.is_a?(Array); d = {} unless d.is_a?(Hash)
              e = d['error']; description = (e.is_a?(Hash) ? e['message'] : e) || d['message'] || "HTTP #{@resp[:status]}"
            end
          end
          model = @request_model || @response_model
          name = [@endpoint['operation'], model].compact.join(' ')
          Result.new(name: Privacy.best_effort_scrub(name), attributes: Privacy.scrub_attributes(@attrs), ok: ok, description: Privacy.best_effort_scrub(description))
        end
      end
    end
  end
end
