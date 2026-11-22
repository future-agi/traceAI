# frozen_string_literal: true
require_relative 'codec'
require_relative 'secrets'
require_relative 'privacy_catalog'
require_relative 'projection'
require_relative 'sampling'
module TraceAI
  module Auto
    module Privacy
      INVOCATION_FAMILIES = %w[gen_ai.request anthropic.request anthropic.thinking].freeze
      REQUEST_IDENTITY_KEYS = %w[gen_ai.request.model gen_ai.request.stream anthropic.request.id].freeze
      # Raw input and raw output (and their children) are withheld while any
      # content hide setting is on, whichever one it is.
      RAW_CONTENT_SWITCHES = %w[hide_inputs hide_outputs hide_input_messages hide_output_messages hide_input_text hide_output_text
                                hide_input_images hide_embedding_vectors hide_llm_invocation_parameters].freeze
      # A tool call's identity is not content; its arguments belong to the input
      # side and its result to the output side.
      TOOL_IDENTITY_KEYS = %w[gen_ai.tool.name gen_ai.tool.call.id gen_ai.tool.type].freeze
      TOOL_ARGUMENT_SWITCHES = %w[hide_inputs hide_input_text hide_input_messages].freeze
      TOOL_RESULT_SWITCHES = %w[hide_outputs hide_output_text hide_output_messages].freeze
      SESSION_ID = 'session.id'
      # Unlike other protected families, usage permits application-defined suffixes.
      # Only these literal schema keys are recoverable without a working registry.
      FALLBACK_USAGE_KEYS = %w[input_tokens output_tokens total_tokens provider_total_tokens
                               cache_read_tokens cache_write_tokens output_tokens.reasoning
                               input_tokens.audio output_tokens.audio].map { |key| 'gen_ai.usage.' + key }.freeze
      class Limit < StandardError; end
      class Budget
        MAX_NODES = CATALOG['limits']['max_nodes']
        def initialize = (@nodes = MAX_NODES; @bytes = CATALOG['limits']['max_text_chars']; @work = 0; @work_spent = false)
        def spend(bytes = 0)
          @nodes -= 1; @bytes -= bytes
          raise Limit if @nodes < 0 || @bytes < 0
        end
        # Matcher work (see MATCH_SPAN_WORK) shared by every value scrubbed with this budget, that is, by one span.
        # The amount is charged before the work it pays for; one that does not fit is refused, and from then on
        # every value of the span is the placeholder.
        def spend_work(amount)
          raise MatchLimit if @work_spent || @work + amount > MATCH_SPAN_WORK
          @work += amount
        rescue MatchLimit
          @work_spent = true
          raise
        end
        def work_spent? = @work_spent
      end
      module_function
      def ivar(value, name) = Object.instance_method(:instance_variable_get).bind_call(value, name)
      def field(value, name) = Struct.instance_method(:[]).bind_call(value, name)
      # Events and links are Structs in some SDK/API releases and plain objects with
      # instance variables in others. Both are read without calling any method the
      # application's object defines.
      def part(value, name)
        if Object.instance_method(:is_a?).bind_call(value, Struct)
          Struct.instance_method(:members).bind_call(value).include?(name) ? field(value, name) : nil
        else
          ivar(value, :"@#{name}")
        end
      end
      def record_member?(value, name)
        Struct.instance_method(:members).bind_call(value).include?(name)
      rescue StandardError
        false
      end
      def safe_string(value)
        Codec.type(value) == String ? value : Codec.type_name(value)
      end
      # Secrets a caller hands in are read as the registry reads them (Config.utf8): invalid bytes become U+FFFD and the
      # valid characters stay, so such a secret still protects the text it was taken from. None is skipped for its bytes.
      # A secret that is a byte slice cutting through a multi-byte character reads as U+FFFD there, which matches less than
      # the valid text around it. So the longest runs of valid characters (8 or more, at most Config::VALID_RUNS_PER_SECRET of
      # them) are secrets of their own as well: a secret never adds an unbounded list.
      def normalize_secrets(secrets)
        strings = secrets.select { |secret| Codec.type(secret) == String }
        strings.flat_map { |secret| [Config.utf8(secret), *valid_runs(secret)] }.select { |secret| secret.length >= 8 }.uniq
      end
      def valid_runs(secret) = Config.valid_runs(secret)
      def secret_list?(secrets) = [Array, NormalizedSecrets].include?(Codec.type(secrets))
      # The secrets of a span, read as above and sorted longest first, built once for the span: the copies it makes are made once,
      # and not again for each of the span's names, values, events and links.
      def normalized_secrets(secrets)
        return secrets if secrets.instance_of?(NormalizedSecrets)
        NormalizedSecrets.new(normalize_secrets(secrets).uniq.sort_by { |secret| [-secret.length, secret] })
      end
      def secrets(headers, configured = [])
        values = configured.select { |v| Codec.type(v) == String }
        headers.each do |k, v|
          next unless Codec.type(k) == String && Codec.type(v) == String && credential?(k)
          values << v
          values << v.sub(/\A\S+\s+/, '') if leaf(k).end_with?('authorization')
        end
        values.select { |s| s.length >= 8 }.uniq.sort_by { |s| -s.length }
      end
      # Observation is independent of wire-span eligibility. Suppressed requests
      # read only the native headers/URL already in hand, never their bodies.
      # `session_id` is the id the application set for this request
      # (using_session). A header or query value equal to it, or equal to it
      # after the scheme word (Bearer <id>), is correlation data the application
      # chose to export, so it is not learned as a secret.
      def observe_request(headers, url, session_id = nil)
        session_id = nil unless Codec.type(session_id) == String && !session_id.empty?
        headers.each do |name, value|
          next unless Codec.type(name) == String && Codec.type(value) == String
          key = name.downcase
          next unless credential?(key)
          bare = leaf(key).end_with?('authorization') ? value.sub(/\A\S+\s+/, '') : value
          next if session_id && (value == session_id || bare == session_id)
          Auto.register_secret(value)
          Auto.register_secret(bare) if leaf(key).end_with?('authorization')
        end
        query_parts(url)[2].each do |_, equals, raw_value, name, decoded|
          next if equals.empty? || !query_credential?(name)
          next if session_id && (raw_value == session_id || decoded == session_id)
          Auto.register_secret(raw_value)
          Auto.register_secret(decoded)
        end
      end
      # Only call for bodies/fields the wire layer has already parsed. Match the
      # codec's larger input bounds, not the export sanitizer's smaller budget:
      # every credential in an accepted body must protect unrelated later spans.
      # `session_id` is the session id the application set through the agent
      # for this request (using_session). A body value that is exactly that id
      # is correlation data the application chose to export, so it is not
      # learned as a secret even under a credential-like key such as
      # metadata.session_id. The key itself still gets the name rule on export.
      def observe_body_credentials(value, session_id = nil)
        session_id = nil unless Codec.type(session_id) == String && !session_id.empty?
        observe_credentials(value, nil, 0, [100_000, 64 * 1024 * 1024], session_id)
      rescue Limit
        nil
      end
      def observe_credentials(value, key, depth, budget, session_id = nil)
        budget[0] -= 1
        raise Limit if depth > 64 || budget[0] < 0
        klass = Codec.type(value)
        if klass == Hash
          value.each do |name, item|
            next unless Codec.type(name) == String || Codec.type(name) == Symbol
            name = name.name if Codec.type(name) == Symbol
            path = key && !key.empty? ? key + '.' + name : name
            budget[1] -= path.bytesize
            raise Limit if budget[1] < 0
            observe_credentials(item, path, depth + 1, budget, session_id)
          end
        elsif klass == Array
          value.each { |item| observe_credentials(item, key, depth + 1, budget, session_id) }
        elsif klass == String && key && credential?(key)
          budget[1] -= value.bytesize
          raise Limit if budget[1] < 0
          return if session_id && value == session_id
          Auto.register_secret(value)
          Auto.register_secret(value.sub(/\A\S+\s+/, '')) if leaf(key).end_with?('authorization')
        end
      end
      # `policy` is set only by scrub_provider_response, for the parsed provider
      # response it was given. Nested JSON strings never inherit it.
      # `hinted` says that `hints` already hold the media context of this value's own siblings, read from the attributes as
      # they came (before a hide setting replaced any of them), so the value is not indexed again.
      def scrub(value, secrets = [], key = nil, depth = 0, budget = Budget.new, media = false, audio = false, hints = {}, framed = true, lossy: false, on_media: nil, policy: nil, hinted: false)
        secrets = normalized_secrets(secrets) if depth.zero?
        budget.spend
        raise Limit if depth > CATALOG['limits']['max_depth']
        path = key || ''
        budget.spend(path.bytesize + path_parts(path).length)
        # The session id set through the agent's API (attribute key exactly
        # session.id) is correlation data, not a credential: it is exempt from
        # the name rule only, as in the Node SDK. Registered secrets and
        # credential value shapes inside it are still scrubbed below, and every
        # other session-named key keeps the rule.
        return REDACTED if credential?(path) && !(depth == 1 && path == SESSION_ID)
        klass = Codec.type(value)
        if klass == Hash
          hints = index_hints(value, path, hints, budget) unless hinted
          # A provider-root logprob token skips only the key-name rule. Its
          # value still meets registered secrets and credential shapes, and a
          # masked token also masks its bytes, which spell the same text.
          token = policy&.token_parents&.key?(value) ? value['token'] : nil
          clean_token = scrub(token, secrets, nil, depth + 1, budget) if token
          masked_bytes = token && clean_token != token
          masked_text = policy&.masked_text&.[](value)
          masked_choice = policy&.masked_choices&.key?(value)
          value.each_with_object({}) do |(k, v), out|
            name = Codec.type(k) == Symbol ? k.name : safe_string(k)
            cleaned = clean_string(name, secrets, budget)
            child_path = path.empty? ? name : path + '.' + name
            out[cleaned] =
              if (masked_choice && name == 'logprobs') || masked_text&.key?(name) || (masked_bytes && name == 'bytes')
                REDACTED
              elsif token && Codec.type(k) == String && k == 'token'
                clean_token
              else
                scrub(v, secrets, child_path, depth + 1, budget, media, audio, hints, lossy: lossy, on_media: on_media, policy: policy)
              end
          end
        elsif klass == Array
          raise Limit if value.length > Budget::MAX_NODES
          payload = media_context(path, media, audio, hints)[2] || (lossy && lossy_media_path?(path))
          numeric = [Integer, Float, TrueClass, FalseClass]
          if payload && !value.empty? && value.all? { |item| numeric.include?(Codec.type(item)) }
            on_media&.call
            return MEDIA['other_replacement']
          end
          value.each_with_index.map { |v, i| scrub(v, secrets, path + '.' + i.to_s, depth + 1, budget, media, audio, hints, lossy: lossy, on_media: on_media, policy: policy) }
        elsif klass == String
          budget.spend(value.bytesize)
          replacement = media_replacement(value, path, media, audio, hints, framed)
          if !replacement && lossy && (lossy_media_path?(path) || lossy_base64?(value, path, hints)) && value != REDACTED && !OMISSION.match?(value)
            replacement = MEDIA['other_replacement']
          end
          if replacement
            on_media&.call
            return replacement
          end
          if value.lstrip.start_with?('{', '[')
            # Preserve S1's fail-closed policy for partial/malformed JSON strings.
            parsed = Codec.parse(value)
            context = media_context(path, media, audio, hints)
            cleaned = scrub(parsed, secrets, nil, depth + 1, budget, context[0], context[1], lossy: lossy, on_media: on_media)
            value = Codec.generate(cleaned) unless cleaned == parsed
          end
          cleaned = clean_string(value, secrets, budget, framed)
          on_media&.call if cleaned != value && cleaned.include?('<omitted:')
          cleaned
        elsif [Integer, Float, TrueClass, FalseClass, NilClass].include?(klass)
          # The span has used all the matching work it may: no leaf of it is read, whatever its type.
          return REDACTED if budget.work_spent?
          if klass != NilClass && (media_context(path, media, audio, hints)[2] || (lossy && lossy_media_path?(path)))
            on_media&.call
            return MEDIA['other_replacement']
          end
          klass == Float && !value.finite? ? (value.nan? ? 'NaN' : (value.positive? ? 'Infinity' : '-Infinity')) : value
        else
          clean_string(Codec.type_name(value), secrets, budget)
        end
      end
      ResponsePolicy = Struct.new(:token_parents, :masked_choices, :masked_text)
      def response_roots(value)
        return [value] unless Codec.type(value) == Array
        raise Limit if value.length > Budget::MAX_NODES
        value
      end
      # The exact objects that hold a catalog logprob token, reached from this
      # root through real arrays. Identity, not equality: an equal object
      # elsewhere (tool output, user content) gets no exemption.
      def response_token_parents(value, rule, budget)
        parents = {}.compare_by_identity
        return parents unless rule
        visit = lambda do |node, path, depth|
          budget.spend
          raise Limit if depth > CATALOG['limits']['max_depth']
          segment = path.first
          rest = path.drop(1)
          if segment == '*'
            next unless Codec.type(node) == Array
            raise Limit if node.length > Budget::MAX_NODES
            node.each { |item| visit.call(item, rest, depth + 1) }
          elsif Codec.type(node) != Hash
            next
          elsif rest.empty?
            parents[node] = true if segment == 'token' && Codec.type(node['token']) == String
          else
            visit.call(node[segment], rest, depth + 1)
          end
        end
        response_roots(value).each { |root| rule['paths'].each { |path| visit.call(root, path, 0) unless path.empty? } }
        parents
      end
      # Privacy catalog version 3 `logprob_tokens`: the explicit entry point for
      # a parsed provider response (or a buffered array of its stream chunks)
      # that a caller received from its own provider parser. Logprob tokens at
      # the catalog's exact paths are checked against registered secrets and
      # credential shapes, not key-name rules. When value masking changes a
      # choice's message or delta text, that choice's whole logprobs block is
      # masked; buffered chunks are joined by choice index first, so a secret
      # split across deltas masks every delta and block of that choice. Other
      # input, unknown formats and nested JSON strings use the strict generic
      # rules. Raises Limit when the shared budget is exhausted.
      def scrub_provider_response(value, secrets = [], format = 'openai.chat')
        rule = LOGPROB_FORMATS[format]
        return scrub(value, secrets) unless rule
        secrets = normalized_secrets(secrets)
        budget = Budget.new
        policy = ResponsePolicy.new(response_token_parents(value, rule, budget), {}.compare_by_identity, {}.compare_by_identity)
        streaming = Codec.type(value) == Array
        changed = lambda do |text|
          text.include?(REDACTED) || text.include?('__REDACTED__') || scrub(text, secrets, nil, 0, budget) != text
        end
        slots = {}
        response_roots(value).each do |root|
          next unless Codec.type(root) == Hash
          choices = root['choices']
          next unless Codec.type(choices) == Array
          raise Limit if choices.length > Budget::MAX_NODES
          choices.each_with_index do |choice, position|
            budget.spend
            next unless Codec.type(choice) == Hash
            index = choice['index']
            # A streamed choice without an index cannot be joined to its other
            # deltas, so its logprobs cannot be proven safe.
            if streaming && !(Codec.type(index) == Integer && index >= 0)
              policy.masked_choices[choice] = true
              next
            end
            slot = (slots[streaming ? index : position] ||= {choices: [], text: {}, masked: false})
            fields = []
            rule['text_paths'].each do |text_path|
              parent = choice
              text_path[0...-1].each { |part| parent = Codec.type(parent) == Hash ? parent[part] : nil }
              next unless Codec.type(parent) == Hash
              name = text_path.last
              field = parent[name]
              next if field.nil?
              fields << [parent, name]
              unless Codec.type(field) == String
                slot[:masked] = true
                next
              end
              slot[:masked] = true if changed.call(field)
              next unless streaming
              joined = (slot[:text][text_path] || '') + field
              raise Limit if joined.length > CATALOG['limits']['max_text_chars']
              slot[:text][text_path] = joined
            end
            slot[:choices] << [choice, fields]
          end
        end
        slots.each_value do |slot|
          # A secret or shape can cross delta boundaries: check the joined text.
          slot[:masked] = true if slot[:text].each_value.any? { |joined| changed.call(joined) }
          next unless slot[:masked]
          slot[:choices].each do |choice, fields|
            policy.masked_choices[choice] = true
            next unless streaming
            fields.each { |parent, name| (policy.masked_text[parent] ||= {})[name] = true }
          end
        end
        scrub(value, secrets, nil, 0, budget, policy: policy)
      end
      # Credentials named in a parsed provider response, for the caller to
      # register. Catalog logprob tokens are model text here, not credentials.
      def discover_provider_response_secrets(value, format = 'openai.chat')
        budget = Budget.new
        parents = response_token_parents(value, LOGPROB_FORMATS[format], budget)
        found = []
        collect = lambda do |node, key, depth|
          budget.spend
          raise Limit if depth > 64
          klass = Codec.type(node)
          if klass == Hash
            node.each do |name, item|
              next unless Codec.type(name) == String || Codec.type(name) == Symbol
              name = name.name if Codec.type(name) == Symbol
              next if name == 'token' && Codec.type(item) == String && parents.key?(node)
              collect.call(item, key ? key + '.' + name : name, depth + 1)
            end
          elsif klass == Array
            node.each { |item| collect.call(item, key, depth + 1) }
          elsif klass == String && key && credential?(key)
            found << node
            found << node.sub(/\A\S+\s+/, '') if leaf(key).end_with?('authorization')
          end
        end
        collect.call(value, nil, 0)
        found.select { |secret| secret.length >= 8 }.uniq.sort_by { |secret| [-secret.length, secret] }
      end
      # Upstream scrubbing must not lose a wire span on malformed provider data,
      # nor hand unsafe wire content to other processors. Clear the affected
      # value, then let the authoritative snapshot sanitize every export surface.
      def best_effort_scrub(value, secrets = nil)
        scrub(value, secrets || Auto.secret_values)
      rescue StandardError
        klass = Codec.type(value)
        klass == Hash ? {} : (klass == Array ? [] : '')
      end
      # A malformed content value must fail closed without erasing safe outcome,
      # usage, timing or projection-loss metadata. Never infer that arbitrary
      # application attributes are safe; only protected A7 families and an exact,
      # finite fractional sampling double qualify.
      # Its key Budget and value Budget are its own, apart from the snapshot's (see MATCH_SPAN_WORK).
      def safe_metadata(attributes, secrets = nil, lossy: false)
        return {} unless Codec.type(attributes) == Hash
        ratio = attributes.fetch(SamplingRatio::ATTRIBUTE, nil)
        safe_ratio = if Codec.type(ratio) == Float && SamplingRatio.fractional?(ratio)
                       {SamplingRatio::ATTRIBUTE => ratio}
                     else
                       {}
                     end
        begin
          known = Auto.secret_values
          known += secrets if secret_list?(secrets)
          known = normalize_secrets(known).uniq.sort_by { |secret| [-secret.length, secret] }
        rescue StandardError
          known = nil
        end
        result = safe_ratio.dup; budget = Budget.new; key_budget = Budget.new
        # Recovery must use the same complete sibling context as the normal walk.
        # If indexing fails, no protected-prefix value is safe to recover.
        hints = index_hints(attributes)
        attributes.each_with_index do |(key, value), index|
          break if index >= Budget::MAX_NODES
          next unless Codec.type(key) == String && (family = AttributeProjection.protected_family(key))
          # A protected prefix does not make an application's suffix safe. If
          # registry access failed, only fixed schema keys can keep numeric data.
          next if !known && family == 4 && !FALLBACK_USAGE_KEYS.include?(key)
          begin
            cleaned_key = known ? clean_string(key, known, key_budget) : key
            if [Integer, Float, TrueClass, FalseClass].include?(Codec.type(value))
              # Numeric values cannot echo string credentials, but their keys can.
              # Keep key/value budgets separate so an oversized identity value
              # cannot erase otherwise safe drop/usage counters.
              result[cleaned_key] = scrub(value, [], key, 0, Budget.new, false, false, hints, lossy: lossy)
            elsif known
              result[cleaned_key] = scrub(value, known, key, 0, budget, false, false, hints, lossy: lossy)
            end
          rescue StandardError
            next
          end
        end
        result
      rescue StandardError
        # Unlike content prefixes, the fixed numeric ratio needs no sibling
        # context or secret registry, even if indexing the content itself fails.
        safe_ratio || {}
      end
      def scrub_attributes(attributes)
        scrub(attributes, Auto.secret_values)
      rescue StandardError
        safe_metadata(attributes)
      end
      Admission = Struct.new(:omitted, :lossy, :failed)
      # Remember decisions, never raw payloads, before SDK admission. This shadow
      # belongs only to our export view; the host's values and limits stay native.
      def media_admission(attributes, count_limit: nil, length_limit: nil, previous: nil)
        omitted = previous ? previous.omitted.dup : {}
        lossy = previous&.lossy || false
        return Admission.new(omitted, lossy, previous&.failed) if attributes.nil?
        raise Limit unless Codec.type(attributes) == Hash
        lossy ||= count_limit && attributes.size > count_limit
        budget = Budget.new
        hints = index_hints(attributes, '', {}, budget)
        attributes.each do |key, value|
          raise Limit unless Codec.type(key) == String
          # Empty payloads carry no bytes yet, but their media context must
          # survive admission if the SDK drops the hint before a later setter.
          omitted[key] = true if media_context(key, false, false, hints)[2]
          scrub(value, [], key, 0, budget, false, false, hints, on_media: -> { omitted[key] = true })
          values = Codec.type(value) == Array ? value : [value]
          lossy ||= length_limit && values.any? { |item| Codec.type(item) == String && item.length > length_limit }
        end
        Admission.new(omitted, !!lossy, previous&.failed)
      rescue StandardError
        Admission.new(omitted || {}, true, true)
      end
      def admitted_attributes(attributes, admission)
        return attributes || {} unless admission
        raise Limit unless attributes.nil? || Codec.type(attributes) == Hash
        (attributes || {}).each_with_object({}) do |(key, value), result|
          klass = Codec.type(value)
          empty = klass == NilClass || (klass == Array && value.empty?)
          result[key] = admission.omitted[key] && !empty ? MEDIA['other_replacement'] : value
        end
      end
      def truncated_attributes?(attributes)
        return false unless Codec.type(attributes) == Hash
        raise Limit if attributes.size > Budget::MAX_NODES
        attributes.any? do |_, value|
          values = Codec.type(value) == Array ? value : [value]
          raise Limit if values.size > Budget::MAX_NODES
          values.any? { |item| Codec.type(item) == String && item.end_with?('...') }
        end
      end
      def hide_errors?(config)
        # A recognised hide_error_details value wins, and an unrecognised one hides.
        # When it is not set, any content hide switch also hides error text.
        explicit = config.hide_setting('hide_error_details')
        return explicit unless explicit.nil?
        %w[hide_inputs hide_outputs hide_input_messages hide_output_messages hide_input_text hide_output_text].any? { |k| config.hidden?(k) }
      end
      # The operation name as the attributes carry it (a String), or nil. A String key and a Symbol key that read the same are
      # one attribute name after `hidden_by_name`, where the last one wins; the operation name is the last one too.
      def operation_name(attributes)
        return nil unless Codec.type(attributes) == Hash
        found = nil
        attributes.each do |key, value|
          found = value if Codec.type(value) == String && (Codec.type(key) == Symbol ? key.name : key) == 'gen_ai.operation.name'
        end
        found
      end
      # The attributes with the hide settings applied by their own names, before scrubbing renames anything.
      def hidden_by_name(attributes, config, operation)
        return attributes unless Codec.type(attributes) == Hash
        named = attributes.each_with_object({}) { |(key, value), out| out[Codec.type(key) == Symbol ? key.name : safe_string(key)] = value }
        mask(named, config, operation)
      end
      def mask(attributes, config, operation = nil)
        hide = ->(key) { config.hidden?(key) }
        attributes.each_with_object({}) do |(k, v), out|
          next if (k == 'input.mime_type' && hide.call('hide_inputs')) || (k == 'output.mime_type' && hide.call('hide_outputs'))
          input = k == 'input' || k.start_with?('input.', 'gen_ai.input.')
          output = k == 'output' || k.start_with?('output.', 'gen_ai.output.')
          input_messages = k == 'gen_ai.input.messages' || k.start_with?('gen_ai.input.messages.')
          output_messages = k == 'gen_ai.output.messages' || k.start_with?('gen_ai.output.messages.')
          system = k == 'gen_ai.system_instructions' || k.start_with?('gen_ai.system_instructions.')
          tools = k == 'gen_ai.tool' || k.start_with?('gen_ai.tool.')
          tool_arguments = k == 'gen_ai.tool.call.arguments'
          tool_result = k == 'gen_ai.tool.call.result'
          tools &&= !(tool_arguments || tool_result || TOOL_IDENTITY_KEYS.include?(k))
          raw = k == 'raw.input' || k.start_with?('raw.input.') || k == 'raw.output' || k.start_with?('raw.output.')
          next if raw && RAW_CONTENT_SWITCHES.any? { |flag| hide.call(flag) }
          parameters = !REQUEST_IDENTITY_KEYS.include?(k) && INVOCATION_FAMILIES.any? { |family| k == family || k.start_with?(family + '.') }
          next if (input_messages || system) && (hide.call('hide_inputs') || hide.call('hide_input_messages'))
          next if output_messages && (hide.call('hide_outputs') || hide.call('hide_output_messages'))
          next if parameters && hide.call('hide_llm_invocation_parameters')
          next if hide_errors?(config) && %w[error.message exception.message exception.stacktrace].include?(k)
          next if hide.call('hide_input_images') && !output && (k.match?(/image|file.*uri|file.*url/i) || (input_messages && k == 'gen_ai.input.messages'))
          next if hide.call('hide_embedding_vectors') && k.include?('embedding') && k.end_with?('.vector')
          input_side = input || system || tools || parameters
          output_side = output
          if k == 'gen_ai.audio.transcript' || k.start_with?('gen_ai.audio.transcript.')
            input_side = (operation || attributes['gen_ai.operation.name']) == 'text_to_speech'; output_side = !input_side
          end
          hidden = (input_side && (hide.call('hide_inputs') || hide.call('hide_input_text'))) || (output_side && (hide.call('hide_outputs') || hide.call('hide_output_text')))
          hidden ||= k == 'input.value' && (hide.call('hide_input_messages') || hide.call('hide_input_images') || hide.call('hide_llm_invocation_parameters'))
          hidden ||= k == 'output.value' && hide.call('hide_output_messages')
          hidden ||= tools && %w[hide_inputs hide_outputs hide_input_text hide_output_text].any? { |flag| hide.call(flag) }
          hidden ||= tool_arguments && TOOL_ARGUMENT_SWITCHES.any? { |flag| hide.call(flag) }
          hidden ||= tool_result && TOOL_RESULT_SWITCHES.any? { |flag| hide.call(flag) }
          out[k] = hidden ? '__REDACTED__' : v
        end
      end
      SafeEvent = Struct.new(:name, :attributes, :timestamp)
      SafeLink = Struct.new(:span_context, :attributes)
      SafeContext = Struct.new(:trace_id, :span_id, :tracestate, :trace_flags, :remote)
      def state(value, secrets, budget)
        return scrub(value, secrets, nil, 0, budget) if Codec.type(value) == String
        hash = ivar(value, :@hash)
        return '' unless Codec.type(hash) == Hash
        scrub(hash, secrets, nil, 0, budget).map { |k, v| "#{k}=#{safe_string(v)}" }.join(',')
      end
      def resource_attributes(record, resource)
        original = field(record, :resource)
        attributes = Codec.type(original) == ExportResource ? field(original, :attributes) : ivar(original, :@attributes)
        supplied = Codec.type(resource) == ExportResource ? field(resource, :attributes) : ivar(resource, :@attributes)
        raise Limit unless (attributes.nil? || Codec.type(attributes) == Hash) && (supplied.nil? || Codec.type(supplied) == Hash)
        utf8_attributes((attributes || {}).merge(supplied || {}))
      end
      # An application's SDK resource holds environment values (service.name,
      # OTEL_RESOURCE_ATTRIBUTES, the command line) as the SDK read them. Under
      # LANG=C those are tagged US-ASCII even when they hold UTF-8, and no
      # sanitizer check can read such a string. Read names and values as UTF-8
      # first, as the agent does for its own configuration.
      def utf8_attributes(attributes)
        raise Limit if attributes.size > Budget::MAX_NODES
        attributes.each_with_object({}) do |(key, value), result|
          result[Config.utf8(key)] = Codec.type(value) == Array ? value.first(Budget::MAX_NODES + 1).map { |item| Config.utf8(item) } : Config.utf8(value)
        end
      end
      # Failed span content must not erase independently safe service/custom
      # resource identity or the span name. Each recovery has its own bounded
      # traversal; an invalid name cannot erase a safe resource (or vice versa).
      # Registry failure still forbids exporting any recovered string.
      # The name and the resource each scrub with a Budget of their own, apart from the snapshot's (see MATCH_SPAN_WORK).
      def recover_identity(copy, record, resource, config, secrets)
        known = Auto.secret_values
        known = (known + secrets).uniq if secret_list?(secrets)
        begin
          copy.name = scrub(safe_string(field(record, :name)), known)
        rescue StandardError
          nil
        end
        begin
          attributes = scrub(resource_attributes(record, resource), known)
          attributes.reject! do |key, _|
            !AttributeProjection.protected_family(key) && (AttributeProjection.content_group(key, nil) || key == 'gen_ai.tool' || INVOCATION_FAMILIES.include?(key))
          end
          copy.resource = ExportResource.new(mask(attributes, config, copy.attributes['gen_ai.operation.name']))
        rescue StandardError
          nil
        end
      rescue StandardError
        nil
      end
      def snapshot(record, resource, config, secrets = nil)
        # Identity and timing are copied separately, so an error anywhere in the
        # sanitizer cannot drop a span/batch or allow unsanitized content through.
        copy = ExportRecord.new
        %i[kind parent_span_id start_timestamp end_timestamp span_id trace_id trace_flags].each { |key| copy[key] = field(record, key) }
        # SDK releases before 1.10 have no parent_span_is_remote member; their spans report a local parent.
        copy.parent_span_is_remote = record_member?(record, :parent_span_is_remote) ? field(record, :parent_span_is_remote) : false
        flags = copy.trace_flags
        flags = ivar(flags, :@flags) unless Codec.type(flags) == Integer
        copy.trace_flags = Codec.type(flags) == Integer ? flags & 255 : 0
        %i[total_recorded_attributes total_recorded_events total_recorded_links].each do |key|
          count = field(record, key)
          copy[key] = Codec.type(count) == Integer && count >= 0 ? count : 0
        end
        copy.name = ''; copy.attributes = {}; copy.events = []; copy.links = []; copy.tracestate = ''
        copy.resource = ExportResource.new({}); copy.instrumentation_scope = ExportScope.new('', '', {})
        copy.status = LocalStatus.new(0, '')
        source = field(record, :attributes) || {}; lossy = false
        begin
          budget = Budget.new
          admission = ivar(record, :@traceai_media) || {}
          raise Limit unless Codec.type(admission) == Hash
          event_media = admission[:events] || {}; link_media = admission[:links] || {}
          raise Limit unless Codec.type(event_media) == Hash && Codec.type(link_media) == Hash
          evidence = [admission[:attributes], *event_media.values, *link_media.values].compact
          raise Limit if evidence.size > Budget::MAX_NODES || evidence.any? { |item| Codec.type(item) != Admission }
          source = admitted_attributes(source, admission[:attributes])
          lossy = copy.total_recorded_attributes > source.size || evidence.any?(&:lossy) || truncated_attributes?(source)
          original_events = field(record, :events) || []; original_links = field(record, :links) || []
          raise Limit unless Codec.type(original_events) == Array && Codec.type(original_links) == Array
          raise Limit if original_events.size + original_links.size > Budget::MAX_NODES
          lossy ||= original_events.any? { |event| truncated_attributes?(part(event, :attributes)) } ||
                    original_links.any? { |link| truncated_attributes?(part(link, :attributes)) }
          status = field(record, :status)
          code = Codec.type(status) == LocalStatus ? field(status, :code) : ivar(status, :@code)
          description = Codec.type(status) == LocalStatus ? field(status, :description) : ivar(status, :@description)
          code = 0 unless Codec.type(code) == Integer
          code = {0 => 1, 1 => 0, 2 => 2}.fetch(code, 0) unless Codec.type(status) == LocalStatus
          # A safe numeric status survives even when content sanitization fails.
          copy.status = LocalStatus.new([0, 1, 2].include?(code) ? code : 0, '')
          known = Auto.secret_values
          secrets = secrets ? (known + secrets).uniq.sort_by { |value| -value.length } : known
          # Read and sorted once for the whole span.
          secrets = normalized_secrets(secrets)
          raise Limit if admission[:failed] || evidence.any?(&:failed)
          # Identity (name, status text, resource, scope) is scrubbed before the attributes, so a span whose attribute
          # values use up the span's matcher work (MATCH_SPAN_WORK) keeps its identity. The hide masks that need the
          # operation name read it from the attributes as they came (see below), so they do not wait for the scrub.
          name = scrub(safe_string(field(record, :name)), secrets, nil, 0, budget)
          status_text = hide_errors?(config) ? '__REDACTED__' : scrub(safe_string(description || ''), secrets, nil, 0, budget)
          resource_scrubbed = scrub(hidden_by_name(resource_attributes(record, resource) || {}, config, operation_name(source)), secrets, nil, 0, budget)
          scope = field(record, :instrumentation_scope)
          structured = Object.instance_method(:is_a?).bind_call(scope, Struct)
          scope_name = structured ? field(scope, :name) : ivar(scope, :@name)
          scope_version = structured ? field(scope, :version) : ivar(scope, :@version)
          scope_attrs = Codec.type(scope) == ExportScope ? field(scope, :attributes) : ivar(scope, :@attributes)
          scope_name_scrubbed = scrub(safe_string(scope_name || ''), secrets, nil, 0, budget)
          scope_version_scrubbed = scrub(safe_string(scope_version || ''), secrets, nil, 0, budget)
          scope_attrs_scrubbed = scrub(hidden_by_name(scope_attrs || {}, config, operation_name(source)), secrets, nil, 0, budget)
          # The operation name is an identity field: it is read from the attributes as they came, so a span that passes its
          # work bound before the name still tells a spoken input from a spoken output.
          operation = operation_name(source)
          # The hide settings decide by the attribute's own name, so they apply to the attributes as they came, before any
          # name is replaced by the placeholder; the same masks run again on the scrubbed names below.
          # The media context (a mime type beside a payload) is read from the same original attributes: a hide setting that
          # replaces a mime type must not take the context from the payload that sits beside it.
          hints = index_hints(source, '', {}, budget)
          attributes = scrub(hidden_by_name(source, config, operation), secrets, nil, 0, budget, false, false, hints, lossy: lossy, hinted: true)
          apply_media = lambda do |attrs, decision|
            admitted = admitted_attributes(attrs, decision)
            original_hints = Codec.type(admitted) == Hash ? index_hints(admitted, '', {}, budget) : {}
            mask(scrub(hidden_by_name(admitted, config, operation), secrets, nil, 0, budget, false, false, original_hints, lossy: lossy, hinted: true), config, operation)
          end
          safe_status = LocalStatus.new(copy.status.code, status_text)
          safe_resource = ExportResource.new(mask(resource_scrubbed, config, operation))
          safe_scope = ExportScope.new(scope_name_scrubbed, scope_version_scrubbed, mask(scope_attrs_scrubbed, config, operation))
          events = original_events.map do |event|
            budget.spend
            SafeEvent.new(scrub(safe_string(part(event, :name)), secrets, nil, 0, budget), apply_media.call(part(event, :attributes), event_media[event.object_id]), part(event, :timestamp))
          end
          links = original_links.map do |link|
            budget.spend
            ctx = part(link, :span_context)
            link_flags = ivar(ctx, :@trace_flags)
            link_flags = ivar(link_flags, :@flags) unless Codec.type(link_flags) == Integer
            link_flags = Codec.type(link_flags) == Integer ? link_flags & 255 : 0
            SafeLink.new(SafeContext.new(ivar(ctx, :@trace_id), ivar(ctx, :@span_id), state(ivar(ctx, :@tracestate), secrets, budget), link_flags, ivar(ctx, :@remote) == true), apply_media.call(part(link, :attributes), link_media[link.object_id]))
          end
          tracestate = state(field(record, :tracestate), secrets, budget)
          # Commit only after every field succeeds: fail closed on limits/errors.
          copy.name = name; copy.attributes = mask(attributes, config, operation); copy.status = safe_status
          copy.resource = safe_resource; copy.instrumentation_scope = safe_scope
          copy.events = events; copy.links = links; copy.tracestate = tracestate
        rescue StandardError => e
          # By the names the attributes came with, as the main path does: a registered secret inside a name changes the name
          # the scrub gives it, and the hide settings still decide by the original.
          named = begin
            hidden_by_name(source, config, operation_name(source))
          rescue StandardError
            source
          end
          copy.attributes = mask(safe_metadata(named, secrets, lossy: lossy), config)
          recover_identity(copy, record, resource, config, secrets)
          Auto.debug(e)
        end
        copy
      end
    end
  end
end
