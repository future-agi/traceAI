# frozen_string_literal: true
require_relative 'codec'
module TraceAI
  module Auto
    module Privacy
      # Shipping bytes are checked against spec/auto-instrumentation by sync.py.
      # Use our private codec: loading privacy must never activate application gems.
      def self.freeze_catalog(value)
        value.each { |key, item| freeze_catalog(key); freeze_catalog(item) } if value.instance_of?(Hash)
        value.each { |item| freeze_catalog(item) } if value.instance_of?(Array)
        value.freeze
      end
      CATALOG = freeze_catalog(Codec.parse(File.binread(File.join(__dir__, 'privacy.json'))))
      REDACTED = CATALOG.fetch('credentials').fetch('replacement')
      PATTERNS = CATALOG['credentials']['patterns'].map { |item| item['contains'] }.freeze
      EXCLUSIONS = CATALOG['credentials']['exclusions'].map do |item|
        guards = item.fetch('unless_contains_patterns', []).map do |id|
          CATALOG['credentials']['patterns'].find { |pattern| pattern['id'] == id }.fetch('contains')
        end
        [Regexp.new('\\A(?:' + item['regex'].delete_prefix('^').delete_suffix('$') + ')\\z'), guards.freeze].freeze
      end.freeze
      CONTEXT_EXCLUSIONS = CATALOG['credentials']['contextual_exclusions'].map do |item|
        Regexp.new('\\A(?:' + item['path_regex'].delete_prefix('^').delete_suffix('$') + ')\\z')
      end.freeze
      # Catalog regexes are written for Python and JavaScript, where `$` without
      # a multiline flag is the end of the input. In Ruby it is the end of a
      # line, which would stop a private-key match at its first line break.
      def self.end_anchored(source)
        out = +''; in_class = false; escaped = false
        source.each_char do |char|
          if escaped
            escaped = false
          elsif char == '\\'
            escaped = true
          elsif in_class
            in_class = false if char == ']'
          elsif char == '['
            in_class = true
          elsif char == '$'
            out << '\\z'
            next
          end
          out << char
        end
        out
      end
      # Version 3: credential value shapes apply to text independently of any
      # name, after registered secrets. They only ever add redaction.
      VALUE_SHAPES = CATALOG['credentials'].fetch('value_shapes', []).map do |item|
        Regexp.new(end_anchored(item['regex']), item['flags'].to_s.include?('i') ? Regexp::IGNORECASE : 0)
      end.freeze
      # Version 3: the closed list of provider-response logprob token paths.
      # Only an explicit, parsed provider response root may use them.
      LOGPROB_FORMATS = CATALOG.fetch('logprob_tokens', {}).fetch('formats', []).to_h { |rule| [rule['format'], rule] }.freeze
      MEDIA = CATALOG.fetch('media')
      MIME = MEDIA.fetch('mime_siblings')
      AUDIO = MEDIA.fetch('audio')
      MAGIC = MEDIA['magic'].map do |rule|
        rule['tests'].map do |test|
          bytes = [test['hex']].pack('H*').bytes.freeze
          [test['offset'], bytes, [test.fetch('mask', 'ff' * bytes.length)].pack('H*').bytes.freeze].freeze
        end.freeze
      end.freeze
      BASE64 = Regexp.new('\\A[' + Regexp.escape(MEDIA['base64_alphabet']) + ']+\\z')
      OMISSION = /\A<omitted: (?:[0-9]+ base64 chars|media|data URI)>\z/
      DATA = /(?<![A-Za-z0-9+.\-_:])data:[^\s"'<>\\]*(?:[\r\n]+[A-Za-z0-9+\/_=\-]+)*/i
      URL_SOURCE = '(?:https?://|(?<!\\S)[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^\\s"<>\\\\])+'
      URL_VALUE = Regexp.new('\\A(?:' + URL_SOURCE + ')\\z', Regexp::IGNORECASE)
      TEXT = Regexp.new('(?<url>' + URL_SOURCE + ')|(?<data>' + DATA.source + ')', Regexp::IGNORECASE)
      module_function
      def normalize(name) = name.tr('A-Z', 'a-z').delete(CATALOG['normalization']['remove'].join)
      def path_parts(path)
        parts = path.split(/[.\[\]]+/, CATALOG['limits']['max_nodes'] + 2).reject(&:empty?)
        raise Limit if parts.length > CATALOG['limits']['max_nodes']
        parts
      end
      def numeric_part?(part) = part.match?(/\A[0-9]+\z/)
      def leaf(path) = normalize(path_parts(path).reject { |part| numeric_part?(part) }.last || '')
      def excluded?(name)
        folded = normalize(name)
        EXCLUSIONS.any? { |regex, guards| regex.match?(folded) && guards.none? { |marker| folded.include?(marker) } }
      end
      # The sole credential-name predicate, including ancestry and catalog-pinned
      # metric exclusions. Contextual exclusions apply only if the catalog lists
      # any; version 3 lists none, and logprob tokens get their exemption only
      # through Privacy.scrub_provider_response. Exemptions never hide an
      # independent marker.
      def credential?(name)
        parts = path_parts(name || '').reject { |part| numeric_part?(part) }.map { |part| normalize(part) }
        excluded = false
        parts.each_with_index do |part, index|
          if excluded?(part)
            excluded = true
          elsif PATTERNS.any? { |pattern| part.include?(pattern) }
            # Context exclusions only matter to a matching marker. Avoid building
            # every growing prefix for long flattened paths with ordinary names.
            context = parts.first(index + 1).join('.')
            if CONTEXT_EXCLUSIONS.any? { |regex| regex.match?(context) }
              excluded = true
            else
              return true
            end
          end
        end
        !excluded && !excluded?(name || '') && PATTERNS.any? { |pattern| normalize(name || '').include?(pattern) }
      end
      SIGNED = CATALOG['signed_url']['parameters'].map { |name| normalize(name) }.freeze
      def query_credential?(name)
        SIGNED.include?(normalize(name)) || (CATALOG['signed_url']['also_match_credential_patterns'] && credential?(name))
      end
      def decode_percent(value)
        value.b.gsub(/%[0-9a-f]{2}/i) { |hex| hex[1..].to_i(16).chr }.force_encoding('UTF-8').scrub
      end
      def decode_component(value) = decode_percent(value.tr('+', ' '))
      def omitted(value) = MEDIA['replacement'].sub('{length}', value.length.to_s)
      def data_uri(value)
        return unless value[0, 5].casecmp?('data:')
        metadata, comma, payload = value[5..].partition(',')
        return if comma.empty?
        payload = decode_percent(payload)
        return omitted(payload) if metadata.downcase.end_with?(';base64') && (payload.empty? || BASE64.match?(payload))
        MEDIA['data_uri']['other_replacement']
      end
      def data_text(value)
        value.gsub(DATA) { |uri| data_uri(uri) || uri }
      end
      def magic?(value)
        return false unless BASE64.match?(value)
        count = MEDIA['base64_prefix_decoded_bytes']
        # Only the prefix is decoded. No media-sized decoded allocation and no
        # require of the application's possibly pinned base64/default gems.
        prefix = +''
        value.each_char do |char|
          next if char == "\r" || char == "\n"
          prefix << char
          break if prefix.length >= (count * 8 + 5) / 6
        end
        prefix = prefix.tr('-_', '+/')
        decoded = (prefix + '=' * ((-prefix.length) % 4)).unpack1('m0').bytes.first(count)
        MAGIC.any? do |tests|
          tests.all? do |offset, expected, masks|
            expected.each_with_index.all? do |byte, index|
              actual = decoded[offset + index]
              actual && (actual & masks[index]) == (byte & masks[index])
            end
          end
        end
      rescue ArgumentError
        false
      end
      def hint(name, value)
        folded = leaf(name)
        return [false, false] unless MIME['names'].include?(folded) || folded == 'type'
        if Codec.type(value) == Array
          raise Limit if value.length > CATALOG['limits']['max_nodes']
          media = audio = false
          value.each do |item|
            next unless Codec.type(item) == String
            local = hint(name, item)
            media ||= local[0]; audio ||= local[1]
          end
          return [media, audio]
        end
        return [false, false] unless Codec.type(value) == String
        mime = value.split(';', 2).first.to_s.strip.downcase
        if MIME['names'].include?(folded)
          [MIME['prefixes'].any? { |prefix| mime.start_with?(prefix) } || MIME['exact'].include?(mime), mime.start_with?('audio/')]
        else
          [false, folded == 'type' && AUDIO['type_names'].include?(normalize(value))]
        end
      end
      # Only an intact, syntactically complete non-media MIME can establish a
      # text boundary after SDK loss. Ruby's truncation suffix is never safe.
      def safe_mime?(name, value)
        return false unless MIME['names'].include?(leaf(name))
        values = Codec.type(value) == Array ? value : [value]
        raise Limit if values.length > CATALOG['limits']['max_nodes']
        !values.empty? && values.all? do |item|
          Codec.type(item) == String && !item.include?('...') &&
            item.split(';', 2).first.to_s.strip.match?(%r{\A[a-zA-Z0-9!\#$&^_.+\-]+/[a-zA-Z0-9!\#$&^_.+\-]+\z}) && !hint(name, item).any?
        end
      end
      # With lost context, catalog payload paths cannot rely on metadata
      # boundaries or even an intact non-media MIME: a nested media hint may
      # have been evicted. Keep payload ancestry through every descendant.
      def lossy_media_path?(path)
        path_parts(path).any? do |part|
          name = normalize(part)
          !numeric_part?(part) && (MEDIA['unrecognized_base64_fields'].include?(name) || AUDIO['fields'].include?(name) ||
            MIME['payload_fields'].include?(name) || AUDIO['payload_fields'].include?(name))
        end
      end
      def lossy_base64?(value, path, hints)
        prefix = value.delete_suffix('...')
        return false unless prefix.length >= 4 && BASE64.match?(prefix)
        return true if value.end_with?('...')
        node = hints; context = safe = false
        path_parts(path).each do |part|
          local = node && node[nil]
          # Only this value's own sibling can establish a non-media boundary;
          # an ancestor's MIME cannot rule out a missing nested media hint.
          safe = local && local[2] && !local[0] && !local[1] && !local[4]
          context ||= local && local[3]
          node = node && node[part]
        end
        return false if safe
        # A missing hint cannot prove an otherwise untyped base64 value safe.
        # Fixed non-payload schema/metadata fields still retain A7 identity,
        # including error.type (whose name is also an unrecognized type hint).
        # Arbitrary usage suffixes and descendants are not such an exemption.
        schema_path = path_parts(path).reject { |part| numeric_part?(part) }.join('.')
        family = AttributeProjection.protected_family(schema_path)
        return false if family && (family != 4 || FALLBACK_USAGE_KEYS.include?(schema_path))
        return true if context
        !AUDIO['metadata_fields'].include?(leaf(path))
      end
      def index_hints(items, path = '', hints = {}, budget = Budget.new)
        indexed = hints.dup
        items.each do |name, item|
          budget.spend
          next unless Codec.type(name) == String || Codec.type(name) == Symbol
          name = name.name if Codec.type(name) == Symbol
          parts = path_parts(path.empty? ? name : path + '.' + name)
          budget.spend(parts.length)
          media, audio = hint(name, item)
          safe = safe_mime?(name, item)
          mime_name = MIME['names'].include?(leaf(name))
          context = mime_name || %w[type format].include?(leaf(name))
          unsafe = mime_name && !safe
          next unless media || audio || safe || context
          parts.pop while parts.last && numeric_part?(parts.last)
          parts.pop
          # Copy only the modified trie branches: nested-object hints cannot
          # mutate sibling context, and lookups never hash growing path strings.
          node = indexed
          parts.each { |part| node = (node[part] = (node[part] || {}).dup) }
          previous = node[nil] || [false, false]
          node[nil] = [previous[0] || media, previous[1] || audio, previous[2] || safe, previous[3] || context, previous[4] || unsafe]
        end
        indexed
      end
      def media_context(path, media = false, audio = false, hints = {})
        initial = hints[nil] || [false, false]
        media ||= initial[0]; audio ||= initial[1]
        payload = direct_audio = false
        node = hints
        path_parts(path).each do |part|
          node = node && node[part]
          name = normalize(part)
          unless numeric_part?(part)
            media = audio = payload = false if AUDIO['metadata_fields'].include?(name)
            direct_audio = AUDIO['fields'].include?(name)
            audio ||= direct_audio
            payload = true if (media && MIME['payload_fields'].include?(name)) || (audio && AUDIO['payload_fields'].include?(name))
          end
          local = (node && node[nil]) || [false, false]
          media ||= local[0]; audio ||= local[1]
        end
        [media, audio, payload || direct_audio]
      end
      # The sole media predicate. Used for all strings (any key), body/JSON and
      # query values; media_context also handles numeric/bool audio samples.
      def media_replacement(value, path = '', media = false, audio = false, hints = {}, framed = true)
        return if value == REDACTED || OMISSION.match?(value)
        if framed
          replacement = data_uri(value)
          return replacement if replacement
        end
        encoded = BASE64.match?(value)
        if magic?(value) || media_context(path, media, audio, hints)[2]
          return encoded ? omitted(value) : MEDIA['other_replacement']
        end
        if encoded && MEDIA['unrecognized_base64_fields'].include?(leaf(path)) && value.length > MEDIA['unrecognized_base64_min_chars_exclusive']
          omitted(value)
        end
      end
      def query_parts(url)
        target, hashmark, fragment = url.partition('#')
        base, question, query = target.partition('?')
        [base, question, query.split('&', -1).map do |pair|
          name, equals, value = pair.partition('=')
          [name, equals, value, decode_component(name), decode_component(value)]
        end, hashmark, fragment]
      end
      def url_value(original, budget)
        base, question, parsed, hashmark, fragment = query_parts(original)
        base = base.sub(%r{\A(https?://)[^\s/@]+:[^\s/@]+@}i, '\\1' + REDACTED + '@')
        hints = index_hints(parsed.select { |pair| !pair[1].empty? }.map { |pair| [pair[3], pair[4]] }, '', {}, budget)
        pairs = parsed.map do |raw_name, equals, raw_value, name, decoded|
          unless equals.empty?
            if query_credential?(name)
              raw_value = REDACTED
            else
              replacement = media_replacement(decoded, name, false, false, hints)
              unless replacement
                cleaned = data_uri(decoded) || data_text(decoded)
                replacement = cleaned if cleaned != decoded
              end
              raw_value = replacement if replacement
            end
          end
          raw_name + equals + raw_value
        end
        data_text(base) + question + pairs.join('&') + hashmark + data_text(fragment)
      end
      def url_value?(value)
        return true if URL_VALUE.match?(value)
        return false unless value.match?(%r{\A(?:https?://|[/?])}i)
        base, question, parsed, hashmark, fragment = query_parts(value)
        return false if question.empty?
        # Mask only structurally framed data URIs to validate a whole URL. Never
        # let credential/audio redaction swallow prose or later URLs here.
        candidate = base + question + parsed.map do |name, equals, raw, _, decoded|
          raw = MEDIA['data_uri']['other_replacement'] if !equals.empty? && data_uri(decoded)
          name + equals + raw
        end.join('&') + hashmark + fragment
        candidate != value && URL_VALUE.match?(candidate)
      end
      # The most the fragment matcher may do for one value: rounds over every secret, automaton steps, and (MATCH_WORK)
      # everything scanned.
      # Past either limit the whole value becomes the placeholder; a fragment is never left behind.
      MATCH_ROUNDS = 8
      MATCH_STEPS = 200_000
      # One budget for the whole value: every character scanned in any round, rescan or candidate pass, and every
      # automaton step, counts against it. Past it the value is replaced whole.
      MATCH_WORK = 8_000_000
      class MatchLimit < StandardError; end

      # A secret learned after a span was captured, or one that overlaps another, can leave part of itself next to a
      # placeholder: two secrets that share their edges leave the rest of the longer one beside the placeholder of the
      # other, and a secret overlapped at both ends by others is left as its middle. Whatever the shape, an
      # occurrence of a secret in the text is the secret with any number of disjoint runs of its characters replaced
      # by placeholders (a placeholder may also reach past the secret's end). So a secret S is matched here as the
      # language "S, in which any non-empty substrings may be a placeholder": an automaton whose state is the position
      # in S and whose placeholder step moves to any later position. A match holds at least one placeholder and one
      # literal character, and is replaced by one placeholder. A replacement can complete a match for another secret
      # (a chain), so the secrets are applied again until nothing changes. Every round that changes something adds a
      # placeholder and removes characters, so the process ends; the round and step limits bound the work, and a
      # value that reaches either is replaced whole.
      def close_fragments(text, secrets)
        return text unless text.include?(REDACTED)
        work = [0, 0] # characters and candidates scanned, automaton steps
        MATCH_ROUNDS.times do
          changed = false
          scan = fragment_scan(text, work)
          secrets.each do |secret|
            replaced = replace_fragments(scan, secret, work)
            next if replaced.nil?
            text = replaced; changed = true
            scan = fragment_scan(text, work)
          end
          return text unless changed
        end
        # The last round still changed something: if one more pass would too, the value is not settled.
        scan = fragment_scan(text, work)
        secrets.any? { |secret| replace_fragments(scan, secret, work) } ? REDACTED : text
      rescue MatchLimit
        REDACTED
      end

      def spend(work, amount)
        work[0] += amount
        raise MatchLimit if work[0] > MATCH_WORK
      end

      # The text as items (a character, or nil for a placeholder), the placeholder positions, and the positions of
      # each character, so that one secret finds its candidate starts without reading the whole text.
      def fragment_scan(text, work)
        spend(work, text.length + 1)
        items = []
        parts = text.split(REDACTED, -1)
        parts.each_with_index do |part, index|
          items.concat(part.chars)
          items << nil unless index == parts.length - 1
        end
        tokens = []; positions = Hash.new { |hash, key| hash[key] = [] }
        items.each_with_index do |item, index|
          tokens << index if item.nil?
          positions[item] << index
        end
        [items, tokens, positions]
      end

      # `text` with every match of `secret` beside a placeholder replaced by one, or nil when there is none.
      def replace_fragments(scan, secret, work)
        items, tokens, positions = scan
        size = secret.length
        spend(work, tokens.length + 1)
        listed = positions.fetch(secret[0], nil)
        starts = []
        second = secret[1]
        tokens.each do |token|
          # A match that starts at the placeholder goes on with a character of the secret (or another placeholder).
          following = items[token + 1]
          starts << token if token + 1 >= items.length || following.nil? || secret.include?(following)
          next unless listed
          low = token - size + 1
          at = listed.bsearch_index { |index| index >= low }
          while at && at < listed.length && listed[at] < token
            following = items[listed[at] + 1]
            starts << listed[at] if following.nil? || following == second
            at += 1
          end
        end
        return nil if starts.empty?
        spend(work, starts.length + size)
        table = Hash.new(0)
        secret.each_char.with_index { |char, index| table[char] |= 1 << index }
        full = (1 << (size + 1)) - 1
        accept = 1 << size
        output = []; cursor = 0; found = false
        starts.uniq.sort.each do |start|
          next if start < cursor
          stop = match_end(items, start, table, full, accept, work)
          next unless stop
          output.concat(items[cursor...start]); output << nil
          cursor = stop; found = true
        end
        return nil unless found
        spend(work, items.length)
        output.concat(items[cursor..])
        output.map { |item| item.nil? ? REDACTED : item }.join
      end

      # The end of the longest match of the automaton from `start` that holds a placeholder and a literal, or nil.
      def match_end(items, start, table, full, accept, work)
        mask = 1; position = start; best = nil; token = false; literal = false
        while mask != 0 && position < items.length
          work[1] += 1
          raise MatchLimit if work[1] > MATCH_STEPS
          spend(work, 1)
          item = items[position]
          if item.nil?
            lowest = mask & -mask
            mask = full & ~((lowest << 1) - 1)
            token = true
          else
            mask = (mask & table[item]) << 1
            literal = true
          end
          position += 1
          best = position if token && literal && mask & accept != 0
        end
        best
      end
      def clean_string(value, secrets, budget = Budget.new, framed = true)
        budget.spend(value.bytesize)
        text = value.encode('UTF-8', invalid: :replace, undef: :replace)
        replacement = media_replacement(text, '', false, false, {}, framed)
        return replacement if replacement
        if framed && url_value?(text)
          text = url_value(text, budget)
        else
          original_text = text
          text = text.gsub(TEXT) do
            match = Regexp.last_match
            original = match[0]
            if match[:data]
              data_text(original)
            else
              closing_quote = ''
              if match.begin(0).positive? && original_text[match.begin(0) - 1] == "'" && original.end_with?("'")
                original = original[0...-1]; closing_quote = "'"
              end
              url_value(original, budget) + closing_quote
            end
          end
        end
        # Secrets first, none matching across a placeholder; then whatever of an overlapping secret the first pass left
        # beside a placeholder (see close_fragments).
        plain = secrets.select { |secret| Codec.type(secret) == String && secret.length >= 8 && secret != REDACTED && !secret.include?(REDACTED) }
        plain.each do |secret|
          text = text.split(REDACTED, -1).map { |part| part.gsub(secret, REDACTED) }.join(REDACTED)
        end
        text = close_fragments(text, plain)
        VALUE_SHAPES.each { |shape| text = text.gsub(shape, REDACTED) }
        text
      end
    end
  end
end
