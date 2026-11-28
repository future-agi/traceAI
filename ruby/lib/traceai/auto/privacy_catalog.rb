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
      # A list of secrets already read and sorted (see Privacy.normalized_secrets); `plain` is the part of it that holds no placeholder.
      class NormalizedSecrets < Array
        attr_reader :plain
        def initialize(list)
          super(list)
          @plain = list.select { |secret| secret != REDACTED && !secret.include?(REDACTED) }.freeze
        end
      end
      PATTERNS = CATALOG['credentials']['patterns'].map { |item| item['contains'] }.freeze
      EXCLUSIONS = CATALOG['credentials']['exclusions'].map do |item|
        guards = item.fetch('unless_contains_patterns', []).map do |id|
          CATALOG['credentials']['patterns'].find { |pattern| pattern['id'] == id }.fetch('contains')
        end
        # Java's Pattern semantics, as the shared reference reads the exclusions: '.' matches no line terminator.
        source = item['regex'].delete_prefix('^').delete_suffix('$').gsub('.', '[^\\n\\r\\u0085\\u2028\\u2029]')
        [Regexp.new('\\A(?:' + source + ')\\z'), guards.freeze].freeze
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
      # The spec's patterns are Python regular expressions over str: `\s` is str.isspace, `\b` is a boundary between a
      # character that is alphanumeric (str.isalnum) or '_' and one that is not, and a case-insensitive class or letter i
      # also takes U+0130 and U+0131 (Python's simple case mapping; Ruby's case folding does not).
      PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x20\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
      PY_WORD = '[\\p{L}\\p{N}_]'
      def self.python_pattern(source, ignorecase)
        out = +''
        in_class = false
        index = 0
        while index < source.length
          char = source[index]
          if source[index, 6] == '[\\s\\S]'
            # Any character, whatever \s reads.
            out << '[\\s\\S]'
            index += 6
            next
          end
          if char == '\\'
            pair = source[index, 2]
            index += 2
            if pair == '\\s'
              out << (in_class ? '\\s' + PY_SPACE : '[' + PY_SPACE + ']')
            elsif pair == '\\b' && !in_class
              # The spec's patterns put \b only before a leading word character or after a trailing one.
              out << if index == 2 then '(?<!' + PY_WORD + ')'
                     elsif index == source.length then '(?!' + PY_WORD + ')'
                     else '(?:(?<=' + PY_WORD + ')(?!' + PY_WORD + ')|(?<!' + PY_WORD + ')(?=' + PY_WORD + '))'
                     end
            else
              out << pair
            end
            next
          end
          if in_class
            in_class = false if char == ']'
          elsif char == '['
            in_class = true
            out << char
            index += 1
            if source[index] == '^'
              out << '^'
              index += 1
            end
            out << '\\u0130\\u0131' if ignorecase
            next
          elsif ignorecase && (char == 'i' || char == 'I')
            out << '[iI\\u0130\\u0131]'
            index += 1
            next
          end
          out << char
          index += 1
        end
        end_anchored(out)
      end
      # Version 3: credential value shapes apply to text independently of any
      # name, after registered secrets. They only ever add redaction.
      VALUE_SHAPES = CATALOG['credentials'].fetch('value_shapes', []).map do |item|
        ignorecase = item['flags'].to_s.include?('i')
        # The JWT shape keeps its source: shape_byte_runs scans it exactly. Any other shape's leading \b is checked by
        # shape_byte_runs on the character before each match (a lookbehind over Unicode classes is slow in Onigmo).
        source = if item['regex'].start_with?('\\beyJ') then end_anchored(item['regex'])
                 elsif item['regex'].start_with?('\\b') then python_pattern(item['regex'][2..], ignorecase)
                 else python_pattern(item['regex'], ignorecase)
                 end
        Regexp.new(source, ignorecase ? Regexp::IGNORECASE : 0)
      end.freeze
      SHAPE_BOUNDARY = CATALOG['credentials'].fetch('value_shapes', []).map { |item| item['regex'].start_with?('\\b') }.freeze
      PY_WORD_CHAR = /\A[\p{L}\p{N}_]\z/
      def self.jwt_shape?(shape) = shape.source.start_with?('\\beyJ')
      # Whether the character that ends at byte offset stop is a word character of the spec's \b.
      def self.word_before?(text, stop)
        return false unless stop.positive?
        start = stop - 1
        start -= 1 while start.positive? && (text.getbyte(start) & 0xC0) == 0x80
        PY_WORD_CHAR.match?(text.byteslice(start, stop - start))
      end
      # The JWT shape is scanned exactly, in time linear in the text. Its character
      # classes exclude ".", so each segment is one maximal run of [A-Za-z0-9_-]
      # and a match is fully determined by three consecutive dot-separated segments
      # of a run of [A-Za-z0-9_.-]: "eyJ" at a word boundary in the first segment
      # with 8 or more characters after it, then two segments of 8 or more. Each
      # segment is examined as a first segment at most once, so repeated starts
      # such as "eyJ-" * N are never rescanned. A token ends where its third
      # segment ends, whatever the segment lengths.
      JWT_RUN = /[A-Za-z0-9_.-]+/.freeze
      # Offset of the leftmost start of a token inside the first segment, or nil.
      def self.jwt_start(segment, boundary_at_zero)
        found = segment.index('eyJ')
        while found
          return nil if segment.length - found - 3 < 8
          return found if found.zero? ? boundary_at_zero : segment.getbyte(found - 1) == 45
          found = segment.index('eyJ', found + 1)
        end
        nil
      end
      # The byte runs [start, stop] of a value shape's matches in text, left to right, not overlapping.
      def self.shape_byte_runs(shape, text)
        runs = []
        if jwt_shape?(shape)
          position = 0
          while text.byteindex(JWT_RUN, position)
            base, stop = $~.byteoffset(0)
            run = $~[0]
            position = stop
            next unless run.include?('eyJ') && run.count('.') >= 2
            boundary = run.start_with?('eyJ') && !word_before?(text, base)
            segments = run.split('.', -1)
            offsets = []
            offset = 0
            segments.each { |segment| offsets << offset; offset += segment.length + 1 }
            index = 0
            while index + 2 < segments.length
              if segments[index].length >= 11 && segments[index + 1].length >= 8 && segments[index + 2].length >= 8 &&
                 (start = jwt_start(segments[index], !index.zero? || boundary))
                runs << [base + offsets[index] + start, base + offsets[index + 2] + segments[index + 2].length]
                index += 3
              else
                index += 1
              end
            end
          end
          return runs
        end
        boundary = SHAPE_BOUNDARY[VALUE_SHAPES.index { |item| item.equal?(shape) } || 0]
        position = 0
        while text.byteindex(shape, position)
          start, stop = $~.byteoffset(0)
          if boundary && word_before?(text, start)
            # No boundary here: the search goes on from the next character, as the spec's regular expression would.
            step = 1
            step += 1 while start + step < text.bytesize && (text.getbyte(start + step) & 0xC0) == 0x80
            position = start + step
            next
          end
          runs << [start, stop]
          position = stop > start ? stop : start + 1
        end
        runs
      end
      def self.shape_gsub(shape, text)
        runs = shape_byte_runs(shape, text)
        return text if runs.empty?
        out = +''
        copied = 0
        runs.each do |start, stop|
          out << text.byteslice(copied, start - copied) << REDACTED
          copied = stop
        end
        out << text.byteslice(copied, text.bytesize - copied)
      end
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
      # The whitespace of the spec's patterns (Python's str.isspace, which `\s` reads there): U+0009-U+000D, U+001C-U+0020,
      # U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F and U+3000.
      SPACE_CLASS = '\\t\\n\\v\\f\\r\\x1c-\\x20\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
      DATA = Regexp.new('(?<![A-Za-z0-9+.\\-_:])data:[^' + SPACE_CLASS + '"\'<>\\\\]*(?:[\\r\\n]+[A-Za-z0-9+/_=\\-]+)*', Regexp::IGNORECASE)
      URL_SOURCE = '(?:https?://|(?<![^' + SPACE_CLASS + '])[/?])(?:<redacted>|<omitted: (?:[0-9]+ base64 chars|media|data URI)>|[^' +
                   SPACE_CLASS + '"<>\\\\])+'
      URL_VALUE = Regexp.new('\\A(?:' + URL_SOURCE + ')\\z', Regexp::IGNORECASE)
      TEXT = Regexp.new('(?<url>' + URL_SOURCE + ')|(?<data>' + DATA.source + ')', Regexp::IGNORECASE)
      class Limit < StandardError; end
      class PathBudget < Limit; end
      module_function
      def normalize(name) = name.tr('A-Z', 'a-z').delete(CATALOG['normalization']['remove'].join)
      def path_parts(path)
        parts = path.split(/[.\[\]]+/, CATALOG['limits']['max_nodes'] + 2).reject(&:empty?)
        raise Limit if parts.length > CATALOG['limits']['max_nodes']
        parts
      end
      # A decimal component (Python's str.isdecimal, as the shared reference reads it) is an index, not a name.
      def numeric_part?(part) = part.match?(/\A\p{Nd}+\z/)
      def leaf(path) = normalize(path_parts(path).reject { |part| numeric_part?(part) }.last || '')
      def excluded?(name)
        folded = normalize(name)
        EXCLUSIONS.any? { |regex, guards| regex.match?(folded) && guards.none? { |marker| folded.include?(marker) } }
      end
      MAX_DEPTH = CATALOG['limits']['max_depth']
      VOCABULARY = CATALOG['credentials']['vocabulary']['words'].to_h { |word| [word, true] }.freeze
      LETTER_OR_DIGIT = /\A[\p{L}\p{Nd}]\z/
      UPPER = /\A\p{Uppercase}\z/
      LOWER = /\A\p{Lowercase}\z/
      # Version 4 vocabulary: a credential word that is the whole name or one of its segments. A segment is a run of UTF-16
      # units that are letters or decimal digits (a surrogate half delimits), also split where a lowercase letter is followed
      # by an uppercase one; words compare in lowercase.
      def vocabulary?(name)
        lowered = name.gsub("\u0130", 'i').downcase
        return false unless VOCABULARY.each_key.any? { |word| lowered.include?(word) }
        word = +''
        previous = nil
        (name.encode('UTF-16LE').unpack('v*') << 45).each do |unit|
          surrogate = unit.between?(0xD800, 0xDFFF)
          char = surrogate ? nil : unit.chr(Encoding::UTF_8)
          letter = !surrogate && LETTER_OR_DIGIT.match?(char)
          boundary = !letter || (previous && UPPER.match?(char) && LOWER.match?(previous))
          if boundary && !word.empty?
            return true if VOCABULARY[word]
            word = +''
          end
          if letter
            lower = char.downcase
            word << (lower.length == 1 ? lower : (char == "\u0130" ? 'i' : char))
          end
          previous = char
        end
        false
      rescue EncodingError
        true
      end
      # A name of more than limits.max_depth components (each non-empty run between '.', '[' and ']', indices included) is
      # not read: the whole exported value becomes the placeholder (Privacy.scrub catches PathBudget).
      def path_budget(name)
        return unless name.count('.[]') >= MAX_DEPTH
        raise PathBudget if name.split(/[.\[\]]+/).count { |part| !part.empty? } > MAX_DEPTH
      end
      # The sole credential-name predicate, including ancestry and catalog-pinned
      # metric exclusions. Contextual exclusions apply only if the catalog lists
      # any; the catalog lists none, and logprob tokens get their exemption only
      # through Privacy.scrub_provider_response. Exemptions never hide an
      # independent marker. The patterns and the vocabulary words are tested per
      # component (an excluded component is not tested), then on the whole name.
      def credential?(name)
        name ||= ''
        # Every component's folded text is a substring of the whole name's, so a name that holds no pattern and no word
        # anywhere is no credential name (exclusions only ever say no). Long names keep the component count check.
        if name.length <= CATALOG['limits']['max_nodes']
          folded = normalize(name)
          lowered = name.gsub("\u0130", 'i').downcase
          return false if PATTERNS.none? { |pattern| folded.include?(pattern) } && VOCABULARY.each_key.none? { |word| lowered.include?(word) }
        end
        raw = path_parts(name).reject { |part| numeric_part?(part) }
        parts = raw.map { |part| normalize(part) }
        excluded = false
        parts.each_with_index do |part, index|
          if excluded?(part)
            excluded = true
          elsif PATTERNS.any? { |pattern| part.include?(pattern) } || vocabulary?(raw[index])
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
        !excluded && !excluded?(name) && (PATTERNS.any? { |pattern| normalize(name).include?(pattern) } || vocabulary?(name))
      end
      # credential? at an export site: the name's depth budget first.
      def credential_name?(name)
        path_budget(name)
        credential?(name)
      end
      SIGNED = CATALOG['signed_url']['parameters'].map { |name| normalize(name) }.freeze
      def query_credential?(name)
        SIGNED.include?(normalize(name)) || (CATALOG['signed_url']['also_match_credential_patterns'] && credential?(name))
      end
      def query_name?(name) = SIGNED.include?(normalize(name)) || credential_name?(name)
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
        # As Python's b64decode(validate=True) reads it: the alphabet was checked above, a prefix of 4k+1 characters is
        # no base64, and the unused low bits of the last character are not checked.
        return false if prefix.length % 4 == 1
        decoded = (prefix + '=' * ((-prefix.length) % 4)).unpack1('m').bytes.first(count)
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
          full_name = path.empty? ? name : path + '.' + name
          # A hint's name has the depth budget of every exported name.
          path_budget(full_name) if Codec.type(item) == String || (Codec.type(item) == Array && item.any? { |value| Codec.type(value) == String })
          parts = path_parts(full_name)
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
      # ---- The shared privacy spec v4 text rules (spec/auto-instrumentation/generate_privacy_fixtures.py, class Reference) ----
      # Positions are character indices. The pair rule reads the text as an array of characters, so every index is O(1)
      # whatever the text holds; regular expressions run on the string at byte offsets (String#byteindex).
      def self.chars_of(source) = source.unpack('U*').map { |code| [code].pack('U') }.freeze
      def self.set_of(source) = chars_of(source).to_h { |char| [char, true] }.freeze
      def self.code_set(codes) = codes.to_h { |code| [[code].pack('U'), true] }.freeze
      # Python's str.isspace.
      ISSPACE = code_set([*9..13, *28..32, 0x85, 0xA0, 0x1680, *0x2000..0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000])
      # Java's Character.isWhitespace: no U+0085 and none of the no-break spaces U+00A0, U+2007 and U+202F.
      JAVA_SPACE = code_set([*9..13, *28..32, 0x1680, *0x2000..0x2006, *0x2008..0x200A, 0x2028, 0x2029, 0x205F, 0x3000])
      PAIR_NAME_CHARS = set_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]')
      PAIR_NAME_START = set_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_')
      PAIR_PADDING_FOLLOWERS = set_of("= \t\n\v\f\r,;)]}\"'")
      PAIR_LIST_HEADERS = %w[cookie setcookie authorization proxyauthorization].to_h { |name| [name, true] }.freeze
      # A bounded name (a pair name, a quoted name, a --name, a name after a '?') is read through its last NAME_CAP
      # characters; a name that runs on past that window is undecided and its pair is replaced.
      NAME_CAP = 128
      QUESTION_CAP = 8
      GAP_CAP = 8
      ESCAPED_QUOTE = '\\"'
      ENTITY_QUOTE = '&quot;'
      PATH_PAIR_SEPARATORS = set_of(';,&')
      ENCODED_SPACES = %w[20 09 0A 0B 0C 0D].to_h { |hex| [hex, true] }.freeze
      ENCODED_NBSP = '%C2%A0'
      JSON_SPACE_ESCAPES = set_of('tnrfb')
      HEX_DIGITS = set_of('0123456789abcdefABCDEF')
      JAVA_FINAL_TERMINATORS = ["\r\n", "\n", "\r", "\u0085", "\u2028", "\u2029"].freeze
      LINE_BREAKS = set_of("\n\r\u0085\u2028\u2029\f\v")
      OPEN_BRACKETS = set_of('([{')
      CLOSE_BRACKETS = set_of(')]}')
      VALUE_BREAKS = set_of(",;\n\r")
      JSON_SCALAR_CHARS = set_of('0123456789+-.eEtrufalsn')
      JSON_SCALAR_ENDS = set_of(",}] \t\r\n")
      JSON_MEMBER_ENDS = set_of(',}]')
      JSON_NUMBER = /\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\z/
      USERINFO_STOPS = set_of("/?# \t\r\n")
      BASE64_LINE = set_of('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_=-')
      BASE64_ALPHABET = set_of('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-')
      BASE64_INDEX = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'.each_char.with_index.to_h.merge('-' => 62, '_' => 63).freeze
      PERCENT_ESCAPE = /%[0-9A-Fa-f]{2}/
      NAME_CONTENT = /[\p{L}\p{N}_]/
      USERINFO = %r{\A(https?://)[^\s/@]+:[^\s/@]+@}i
      # The value shapes as read at a --name's value start: the leading word boundary is dropped and the match is anchored there.
      WORD_SHAPES = CATALOG['credentials'].fetch('value_shapes', []).map do |item|
        source = item['regex'].start_with?('\\b') ? item['regex'][2..] : item['regex']
        ignorecase = item['flags'].to_s.include?('i')
        Regexp.new('\\G(?:' + python_pattern(source, ignorecase) + ')', ignorecase ? Regexp::IGNORECASE : 0)
      end.freeze

      # Python slicing and searching over a character array.
      def sub(t, from, to)
        to = t.length if to > t.length
        from < to ? t[from...to].join : ''
      end
      def starts?(t, text, at)
        return false if at.negative? || at + text.length > t.length
        index = 0
        while index < text.length
          return false unless t[at + index] == text[index]
          index += 1
        end
        true
      end
      def find(t, text, from, to = t.length)
        to = t.length if to > t.length
        at = from
        while at + text.length <= to
          return at if t[at] == text[0] && starts?(t, text, at)
          at += 1
        end
        -1
      end
      def rfind(t, text, from, to)
        to = t.length if to > t.length
        at = to - text.length
        while at >= from
          return at if t[at] == text[0] && starts?(t, text, at)
          at -= 1
        end
        -1
      end
      def encoded_equals?(t, at) = t[at] == '%' && t[at + 1] == '3' && (t[at + 2] == 'D' || t[at + 2] == 'd')
      def space?(char) = ISSPACE.key?(char)
      def dashes_space?(char) = ISSPACE.key?(char) && !LINE_BREAKS.key?(char)
      def code_space?(hex) = ISSPACE.key?([hex.to_i(16)].pack('U'))
      def hex4?(text) = text.length == 4 && text.each_char.all? { |char| HEX_DIGITS.key?(char) }

      # Whether another pair starts at start (pair_value_end's boundary look): one bounded forward read.
      def next_pair?(t, start, spaced)
        length = t.length
        i = start
        if spaced
          i += 1 while i < length && (t[i] == ' ' || t[i] == "\t") && i - start <= 8
          return false if i - start > 8
        end
        dashes = i
        i += 1 while i < length && t[i] == '-' && i - dashes <= 2
        return false if i - dashes > 2 || i >= length || !PAIR_NAME_START.key?(t[i])
        name = i
        i += 1
        i += 1 while i < length && PAIR_NAME_CHARS.key?(t[i]) && i - name < 128
        return false if i < length && PAIR_NAME_CHARS.key?(t[i])
        if spaced
          gap = i
          i += 1 while i < length && t[i] == ' ' && i - gap <= 8
          return false if i - gap > 8
        end
        return false if i >= length
        return true if t[i] == ':'
        return false unless t[i] == '='
        rest = i + 1
        !(rest >= length || PAIR_PADDING_FOLLOWERS.key?(t[rest]) ||
          (length - rest <= 2 && JAVA_FINAL_TERMINATORS.include?(sub(t, rest, length))))
      end

      # The true end of a name=value value: a pair boundary with no bracket open, or the end of the text.
      def pair_value_end(t, start)
        length = t.length
        return length if start >= length
        if starts?(t, '-----BEGIN', start)
          stop = find(t, '-----END', start)
          return length if stop.negative?
          close = find(t, '-----', stop + 8)
          return close.negative? ? length : close + 5
        end
        first = t[start]
        i = start
        if first == '"' || first == "'"
          i = start + 1
          i += (t[i] == '\\' ? 2 : 1) while i < length && t[i] != first
          return length if i >= length
          i += 1
        end
        depth = 0
        while i < length
          char = t[i]
          if OPEN_BRACKETS.key?(char)
            depth += 1
          elsif CLOSE_BRACKETS.key?(char)
            depth = [depth - 1, 0].max
          elsif depth.zero?
            return i if VALUE_BREAKS.key?(char) && next_pair?(t, i + 1, true)
            return i if char == ' ' && next_pair?(t, i + 1, false)
          end
          i += 1
        end
        length
      end

      # The width of the encoded whitespace (a percent escape or a JSON string escape) that ends at stop, or 0.
      def encoded_space_width(t, stop, floor)
        return 3 if stop - 3 >= floor && t[stop - 3] == '%' && ENCODED_SPACES.key?(sub(t, stop - 2, stop).upcase)
        return 6 if stop - 6 >= floor && t[stop - 6] == '%' && sub(t, stop - 6, stop).upcase == ENCODED_NBSP
        width = 0
        if stop - 2 >= floor && t[stop - 2] == '\\' && JSON_SPACE_ESCAPES.key?(t[stop - 1])
          width = 2
        elsif stop - 6 >= floor && t[stop - 6] == '\\' && t[stop - 5] == 'u' && hex4?(digits = sub(t, stop - 4, stop)) && code_space?(digits)
          width = 6
        end
        return 0 if width.zero?
        slashes = 1
        slashes += 1 while stop - width - slashes >= floor && t[stop - width - slashes] == '\\'
        slashes.odd? ? width : 0
      end

      # The start of the gap (whitespace and encoded whitespace, in any order) that ends at stop.
      def gap_start(t, stop, floor)
        while stop > floor
          if space?(t[stop - 1])
            stop -= 1
            next
          end
          width = encoded_space_width(t, stop, floor)
          break if width.zero?
          stop -= width
        end
        stop
      end

      def escape_before?(t, stop, floor)
        stop - 3 >= floor && t[stop - 3] == '%' && HEX_DIGITS.key?(t[stop - 2]) && HEX_DIGITS.key?(t[stop - 1])
      end

      # A name that ends at a percent escape, read back over name characters and escapes and decoded once:
      # [decoded, undecided], or nil when the name does not end at an escape.
      def escaped_name(t, name_start, name_end, floor)
        ends_at_escape = escape_before?(t, name_end, floor) && !encoded_equals?(t, name_end - 3)
        double = name_start > floor && escape_before?(t, name_start, floor) && sub(t, name_start - 2, name_start) == '25'
        return nil unless ends_at_escape || double
        start = name_end
        while start > floor && name_end - start < NAME_CAP
          if escape_before?(t, start, floor)
            break if encoded_equals?(t, start - 3)
            start -= 3
          elsif PAIR_NAME_CHARS.key?(t[start - 1])
            start -= 1
          else
            break
          end
        end
        overflow = name_end - start >= NAME_CAP && start > floor && (PAIR_NAME_CHARS.key?(t[start - 1]) || escape_before?(t, start, floor))
        decoded = decode_percent(sub(t, start, name_end))
        [decoded, overflow || PERCENT_ESCAPE.match?(decoded) || !NAME_CONTENT.match?(decoded)]
      end

      # Whether pad '=' after body are trailing base64 padding of a ;base64 data URI line.
      def padding?(body, pad, base64, padded, count)
        return false if !base64 || padded || pad > 2 || body.empty? || body.start_with?('-') || ((count + body.length + pad) % 4).nonzero?
        (BASE64_INDEX[body[-1]] % (pad == 1 ? 4 : 16)).zero?
      end

      # The end of the data URI that DATA matched at t[start...stop]: a continuation line belongs to it only while its '='
      # characters are trailing base64 padding, and never when a separator follows it.
      def data_uri_end(t, start, stop)
        head = start + 5
        head += 1 while head < stop && t[head] != "\r" && t[head] != "\n"
        metadata, comma, payload = sub(t, start + 5, head).partition(',')
        base64 = !comma.empty? && metadata.downcase.end_with?(';base64')
        decoded = decode_percent(payload)
        count = decoded.each_char.count { |char| BASE64_ALPHABET.key?(char) }
        padded = decoded.include?('=')
        position = head
        while position < stop
          line = position
          line += 1 while line < stop && (t[line] == "\r" || t[line] == "\n")
          break if line == position
          after = line
          after += 1 while after < stop && BASE64_LINE.key?(t[after])
          break if after == line
          gap = after
          while gap < t.length && (dashes_space?(t[gap]) || encoded_space_ahead(t, gap).positive?)
            width = encoded_space_ahead(t, gap)
            gap += width.positive? ? width : 1
          end
          break if gap < t.length && (t[gap] == '=' || t[gap] == ':' || encoded_equals?(t, gap))
          group = sub(t, line, after)
          body = group.sub(/=+\z/, '')
          pad = group.length - body.length
          break if body.start_with?('--') || body.include?('=') || (pad.positive? && !padding?(body, pad, base64, padded, count))
          count += body.length
          padded ||= pad.positive?
          position = after
        end
        position
      end

      # Character index <-> byte offset maps, for regular expressions over text that is not ASCII only.
      def offset_maps(text)
        chars = [0]
        bytes = Array.new(text.bytesize + 1)
        bytes[0] = 0
        offset = 0
        text.each_char.with_index do |char, index|
          offset += char.bytesize
          chars << offset
          bytes[offset] = index + 1
        end
        [chars, bytes]
      end

      # The matches of DATA (data only) or TEXT, left to right and not overlapping, each data URI cut by data_uri_end:
      # [start, stop, kind, byte start, byte stop].
      def scan_text(pattern, text, data_only)
        ascii = text.ascii_only?
        to_byte, to_char = ascii ? [nil, nil] : offset_maps(text)
        t = nil
        found = []
        position = 0
        length = text.length
        while position <= length
          break unless text.byteindex(pattern, ascii ? position : to_byte[position])
          match = $~
          byte_start, byte_stop = match.byteoffset(0)
          start = ascii ? byte_start : to_char[byte_start]
          stop = ascii ? byte_stop : to_char[byte_stop]
          kind = data_only || match[:url].nil? ? :data : :url
          if kind == :data
            t ||= text.unpack('U*').map { |code| [code].pack('U') }
            stop = data_uri_end(t, start, stop)
            byte_stop = ascii ? stop : to_byte[stop]
          end
          found << [start, stop, kind, byte_start, byte_stop]
          position = stop > start ? stop : start + 1
        end
        found
      end

      def substitute(pattern, text, data_only)
        out = +''
        copied = 0
        scan_text(pattern, text, data_only).each do |start, _stop, kind, byte_start, byte_stop|
          out << text.byteslice(copied, byte_start - copied)
          out << yield(text.byteslice(byte_start, byte_stop - byte_start), kind, start, byte_start)
          copied = byte_stop
        end
        out << text.byteslice(copied, text.bytesize - copied)
      end

      def data_text(value) = substitute(DATA, value, true) { |uri, _kind, _start, _byte| data_uri(uri) || uri }

      # Whether the encoded whitespace of width at start stands for a control character.
      def encoded_control?(t, start, width)
        return width == 3 && sub(t, start + 1, start + 3) != '20' if t[start] == '%'
        return true if width == 2
        code = sub(t, start + 2, start + 6).to_i(16)
        code < 0x20 || code.between?(0x7F, 0x9F)
      end

      # The width of the pair separator at k ('=', a ':' that does not open "://", %3D or %3d), or 0.
      def separator_at(t, k)
        char = t[k]
        return 1 if char == '=' || (char == ':' && !(t[k + 1] == '/' && t[k + 2] == '/'))
        encoded_equals?(t, k) ? 3 : 0
      end

      def separator_value_end(t, k, size)
        value = k + size
        value += 1 while value < t.length && value - k - size < 8 && t[value] == ' '
        name = k
        name -= 1 while name.positive? && k - name < NAME_CAP && PAIR_NAME_CHARS.key?(t[name - 1])
        if PAIR_LIST_HEADERS.key?(normalize(sub(t, name, k)))
          value += 1 while value < t.length && t[value] != "\n" && t[value] != "\r"
          return value
        end
        pair_value_end(t, value)
      end

      # No rule leaves an '=', ':' or %3D unread at the end of the text it consumed: while t[start...stop] ends at a separator
      # that is a credential or undecided pair, the replacement runs on to that pair's value end.
      def consumed_end(t, start, stop, decide)
        while stop > start
          if (t[stop - 1] == '=' || t[stop - 1] == ':') && separator_at(t, stop - 1).positive?
            size = 1
          elsif stop - 3 >= start && separator_at(t, stop - 3) == 3
            size = 3
          else
            return stop
          end
          stop_at = decide.call(stop - size, size, start, true)
          return stop if stop_at <= stop
          stop = stop_at
        end
        stop
      end

      # The width of the encoded whitespace that starts at start, or 0.
      def encoded_space_ahead(t, start)
        if t[start] == '%'
          return 3 if t[start + 1] && t[start + 2] && ENCODED_SPACES.key?((t[start + 1] + t[start + 2]).upcase)
          return t[start + 1] == 'C' || t[start + 1] == 'c' ? (sub(t, start, start + 6).upcase == ENCODED_NBSP ? 6 : 0) : 0
        end
        if t[start] == '\\'
          return 2 if JSON_SPACE_ESCAPES.key?(t[start + 1])
          if t[start + 1] == 'u'
            digits = sub(t, start + 2, start + 6)
            return 6 if hex4?(digits) && code_space?(digits)
          end
        end
        0
      end

      def dashes_separator?(t, start)
        start < t.length && (t[start] == '=' || t[start] == ':' || encoded_equals?(t, start))
      end

      # 2 for "--", 1 for a single dash and a name start, else 0.
      def flag_dashes(t, i)
        return 2 if t[i] == '-' && t[i + 1] == '-'
        t[i] == '-' && PAIR_NAME_START.key?(t[i + 1]) ? 1 : 0
      end

      def readable_gap?(t, start, stop)
        return false if stop - start > GAP_CAP
        (start...stop).all? { |k| t[k] == ' ' }
      end

      def quote_before(t, stop)
        return ENTITY_QUOTE if stop >= ENTITY_QUOTE.length && starts?(t, ENTITY_QUOTE, stop - ENTITY_QUOTE.length)
        return nil if stop < 1 || (t[stop - 1] != '"' && t[stop - 1] != "'")
        if t[stop - 1] == '"'
          slashes = 0
          slashes += 1 while stop - 2 - slashes >= 0 && t[stop - 2 - slashes] == '\\' && slashes < NAME_CAP
          return ESCAPED_QUOTE if slashes.odd?
        end
        t[stop - 1]
      end

      # A quoted name that ends, after a gap, before the separator at colon: [opening, quote, close, overflow], or nil.
      def json_name(t, colon)
        stop = gap_start(t, colon, 0)
        quote = quote_before(t, stop)
        return nil if quote.nil? || (quote.length == 1 && stop < 3)
        close = stop - quote.length
        floor = [0, close - 130].max
        opening = rfind(t, quote, floor, close)
        return [opening, quote, close, !readable_gap?(t, stop, colon)] if opening >= 0
        floor.positive? ? [-1, quote, close, true] : nil
      end

      def json_string_end(t, start)
        quote = t[start]
        i = start + 1
        while i < t.length
          if t[i] == '\\'
            i += 2
            next
          end
          return i + 1 if t[i] == quote
          i += 1
        end
        t.length
      end

      def json_member_boundary?(t, stop)
        stop += 1 while stop < t.length && JAVA_SPACE.key?(t[stop])
        stop >= t.length || JSON_MEMBER_ENDS.key?(t[stop])
      end

      def escaped_string_end(t, start)
        length = t.length
        i = start + ESCAPED_QUOTE.length
        inner_escape = false
        while i < length
          if t[i] == '\\' && i + 1 < length
            outer = t[i + 1]
            i += 2
            if inner_escape
              inner_escape = false
            elsif outer == '\\'
              inner_escape = true
            elsif outer == '"'
              return i
            end
          else
            i += 1
            inner_escape = false
          end
        end
        length
      end

      def entity_string_end(t, start)
        size = ENTITY_QUOTE.length
        i = start + size
        while i < t.length
          if t[i] == '\\'
            i += 1 + (starts?(t, ENTITY_QUOTE, i + 1) ? size : 1)
          elsif starts?(t, ENTITY_QUOTE, i)
            return i + size
          else
            i += 1
          end
        end
        t.length
      end

      # The end of the value of a quoted credential name: (a) a quoted string at its closing quote, (b) a number, true,
      # false or null before ',', '}', ']' or the end, (c) anything else to the true value end (the line end for a list header).
      def json_value_end(t, start, list_header = false, quote = '"')
        length = t.length
        return start if start >= length
        return entity_string_end(t, start) if quote == ENTITY_QUOTE && starts?(t, ENTITY_QUOTE, start)
        return escaped_string_end(t, start) if quote == ESCAPED_QUOTE && starts?(t, ESCAPED_QUOTE, start)
        return json_string_end(t, start) if t[start] == '"' || t[start] == "'"
        stop = start
        stop += 1 while stop < length && JSON_SCALAR_CHARS.key?(t[stop])
        if (stop >= length || JSON_SCALAR_ENDS.key?(t[stop])) && json_member_boundary?(t, stop)
          scalar = sub(t, start, stop)
          return stop if %w[true false null].include?(scalar) || JSON_NUMBER.match?(scalar)
        end
        if list_header
          start += 1 while start < length && t[start] != "\n" && t[start] != "\r"
          return start
        end
        pair_value_end(t, start)
      end

      # A credential name for the pair rule, with the command-line and bracket spellings undone first.
      def pair_name?(name)
        return false if name.empty?
        stripped = name.sub(/\A-+/, '')
        stripped = stripped[1..] if stripped.start_with?('D') && stripped.length > 1 && name.start_with?('-D')
        stripped = stripped.tr('[]', '..')
        !stripped.empty? && (credential_name?(stripped) || credential_name?(name))
      end

      # The separator branch's decision for the separator (of size) at i: [pair, name, overflow].
      def separator_pair(t, i, size, floor, credential)
        name_end = gap_start(t, i, floor)
        readable = readable_gap?(t, name_end, i)
        name_start = name_end
        while name_start > floor && name_end - name_start < NAME_CAP && PAIR_NAME_CHARS.key?(t[name_start - 1]) &&
              !escape_before?(t, name_start, floor)
          name_start -= 1
        end
        overflow = name_end - name_start >= NAME_CAP && name_start > floor && PAIR_NAME_CHARS.key?(t[name_start - 1]) &&
                   !escape_before?(t, name_start, floor)
        raw = sub(t, name_start, name_end)
        path_budget(raw) if overflow
        escaped = overflow ? nil : escaped_name(t, name_start, name_end, floor)
        pair = (name_end - name_start >= 2 && (overflow || !readable || credential.call(raw))) ||
               (!escaped.nil? && (escaped[1] || !readable || credential.call(escaped[0])))
        [pair ? true : false, escaped ? escaped[0] : raw, overflow]
      end

      # Whether another flag starts at start: a "--", or a single-dash full credential name, plain whitespace and more text.
      def flag_ahead?(t, start, credential)
        dashes = flag_dashes(t, start)
        return dashes == 2 if dashes != 1
        stop = start + 1
        stop += 1 while stop < t.length && PAIR_NAME_CHARS.key?(t[stop]) && stop - start <= NAME_CAP
        return false if stop - start < 3 || stop - start > NAME_CAP || stop >= t.length || !space?(t[stop])
        after = stop
        after += 1 while after < t.length && space?(t[after])
        after < t.length && credential.call(sub(t, start, stop)) ? true : false
      end

      # Whether the line read fresh at start begins with a pair, by any rule, or another flag.
      def line_starts_pair?(t, start, credential)
        length = t.length
        k = -1
        [ENTITY_QUOTE, ESCAPED_QUOTE, '"', "'"].each do |quote|
          next unless starts?(t, quote, start)
          close = find(t, quote, start + quote.length, [length, start + quote.length + 130].min)
          return false if close.negative?
          k = close + quote.length
          break
        end
        if k.negative?
          return true if flag_ahead?(t, start, credential)
          name = start
          name += 1 while name < length && t[name] == '-' && name - start < 2
          k = name
          while k < length && k - name < NAME_CAP
            if PAIR_NAME_CHARS.key?(t[k])
              k += 1
            elsif k + 3 <= length && escape_before?(t, k + 3, k) && !encoded_equals?(t, k) && encoded_space_ahead(t, k).zero?
              k += 3
            else
              break
            end
          end
          return false if k == name
        end
        while k < length && (dashes_space?(t[k]) || encoded_space_ahead(t, k).positive?)
          width = encoded_space_ahead(t, k)
          k += width.positive? ? width : 1
        end
        k < length && separator_at(t, k).positive?
      end

      # The credential pair rule, one left-to-right pass linear in the text. The value of every credential-named name=value,
      # name: value, "name": value, quoted name with '=', --name value and -name value runs to its true end and becomes the
      # placeholder; so does the user information of scheme://user:password@host. skip holds the spans [start, stop,
      # omission] the URL catalog replaced in a whole URL value: a pair whose value starts in a placeholder span is left as
      # the catalog wrote it; an omission span shields nothing.
      PAIR_TRIGGERS = /[:=%-]/
      def pair_rule(text, skip = [])
        t = text.chars
        length = t.length
        out = nil
        copied = 0
        known = {}
        i = 0
        dashes_colon = -1
        ascii = text.ascii_only?
        byte_offsets = marks = nil
        space_next = -1
        skip = skip.reject { |span| span[2] }
        starts = skip.map(&:first)
        credential = ->(name) { known.key?(name) ? known[name] : (known[name] = pair_name?(name)) }
        skipped = lambda do |position|
          k = (starts.bsearch_index { |start| start > position } || starts.length) - 1
          k >= 0 && position < skip[k][1] ? skip[k][1] : -1
        end
        quoted_pair = lambda do |quoted|
          opening, quote, close, overflow = quoted
          name = opening.negative? ? '' : sub(t, opening + quote.length, close)
          letters = name.unpack('U*').map! { |code| [code].pack('U') }
          plain = letters.length
          plain -= 1 while plain.positive? && PAIR_NAME_CHARS.key?(letters[plain - 1]) && !escape_before?(letters, plain, 0)
          escaped = opening >= 0 ? escaped_name(letters, plain, letters.length, 0) : nil
          [(overflow || credential.call(name) || (!escaped.nil? && (escaped[1] || credential.call(escaped[0])))) ? true : false, name]
        end
        equals_value_end = lambda do |k, size, name|
          value = k + size
          value += 1 while value < length && value - k - size < 8 && t[value] == ' '
          if PAIR_LIST_HEADERS.key?(normalize(name))
            value += 1 while value < length && t[value] != "\n" && t[value] != "\r"
            next value
          end
          pair_value_end(t, value)
        end
        decide = lambda do |k, size, floor, read_end|
          quoted = json_name(t, k)
          if quoted && quoted[0] >= floor
            pair, name = quoted_pair.call(quoted)
            if size == 1 && t[k] == ':'
              next -1 unless pair
              next k unless read_end
              value = k + 1
              value += 1 while value < length && JAVA_SPACE.key?(t[value])
              next json_value_end(t, value, PAIR_LIST_HEADERS.key?(normalize(name)), quoted[1])
            end
            next(read_end ? equals_value_end.call(k, size, name) : k) if pair
          end
          next -1 unless separator_pair(t, k, size, floor, credential)[0]
          read_end ? separator_value_end(t, k, size) : k
        end
        quoted_equals = lambda do |k|
          quoted = k.positive? ? json_name(t, k) : nil
          !quoted.nil? && quoted_pair.call(quoted)[0]
        end
        word_end = lambda do |from, value|
          value_end = word = value
          if t[value] == '"' || t[value] == "'"
            value_end = value + 1
            value_end += (t[value_end] == '\\' ? 2 : 1) while value_end < length && t[value_end] != t[value]
            value_end = word = [length, value_end + 1].min
          end
          shape_end = value
          unless WORD_SHAPES.empty?
            byte = ascii ? value : (byte_offsets ||= offset_maps(text)[0])[value]
            WORD_SHAPES.each do |shape|
              next unless text.byteindex(shape, byte)
              stop = $~.byteoffset(0)[1]
              stop = ascii ? stop : value + text.byteslice(byte, stop - byte).length
              shape_end = stop if stop > shape_end
            end
          end
          first = last = -1
          while value_end < length && (value_end < shape_end || !space?(t[value_end]))
            size = value_end >= word ? separator_at(t, value_end) : 0
            if size.positive? && decide.call(value_end, size, from, false) >= 0
              first = value_end if first.negative?
              last = value_end
            end
            value_end += 1
          end
          ahead = value_end
          while ahead < length && (dashes_space?(t[ahead]) || encoded_space_ahead(t, ahead).positive?)
            width = encoded_space_ahead(t, ahead)
            ahead += width.positive? ? width : 1
          end
          if ahead > value_end && ahead < length && separator_at(t, ahead).positive? &&
             decide.call(ahead, separator_at(t, ahead), from, false) >= 0
            first = ahead if first.negative?
            last = ahead
          end
          [first, last].uniq.reject(&:negative?).sort.each do |k|
            value_end = [value_end, decide.call(k, separator_at(t, k), from, true)].max
          end
          consumed_end(t, from, value_end, decide)
        end

        # Only ':', '=', '%' and '-' start a branch; the read jumps from one to the next.
        triggers = []
        if ascii
          text.scan(PAIR_TRIGGERS) { triggers << $~.byteoffset(0)[0] }
        else
          to_char = offset_maps(text)[1]
          text.scan(PAIR_TRIGGERS) { triggers << to_char[$~.byteoffset(0)[0]] }
        end
        trigger = 0
        while i < length
          trigger += 1 while trigger < triggers.length && triggers[trigger] < i
          break if trigger >= triggers.length
          i = triggers[trigger]
          char = t[i]
          start = stop = -1
          replacement = REDACTED
          userinfo = false
          quoted = char == ':' && i.positive? ? json_name(t, i) : nil
          if quoted
            quote = quoted[1]
            pair, name = quoted_pair.call(quoted)
            if pair
              value = i + 1
              value += 1 while value < length && JAVA_SPACE.key?(t[value])
              if !skip.empty? && (span_end = skipped.call(value)) >= 0
                i = [span_end, i + 1].max
                next
              end
              header = PAIR_LIST_HEADERS.key?(normalize(name))
              value_end = json_value_end(t, value, header, quote)
              value_end = consumed_end(t, value, value_end, decide) unless header
              start, stop, replacement = value, value_end, quote + REDACTED + quote if value_end > value
            end
            if start.negative?
              i += 1
              next
            end
          elsif char == ':' && t[i + 1] == '/' && t[i + 2] == '/' && i != dashes_colon
            limit = [length, i + 3 + 512].min
            stop_at = i + 3
            stop_at += 1 while stop_at < limit && !USERINFO_STOPS.key?(t[stop_at])
            # The '@' and ':' positions and the next space are looked up, not rescanned, so a text of many URLs stays linear.
            marks ||= {'@' => [], ':' => []}.tap { |found| t.each_with_index { |c, k| found[c] << k if found.key?(c) } }
            last_at = lambda do |from, to|
              k = (marks['@'].bsearch_index { |position| position >= to } || marks['@'].length) - 1
              k >= 0 && marks['@'][k] >= from ? marks['@'][k] : -1
            end
            at = last_at.call(i + 3, stop_at)
            if at < i + 3
              # A password may hold '/', '?' or '#': a user:password form before a later '@' in the same word runs to it.
              if space_next < i + 3
                space_next = [space_next + 1, i + 3].max
                space_next += 1 while space_next < length && !JAVA_SPACE.key?(t[space_next])
              end
              word = [space_next, i + 1024, length].min
              later = last_at.call(i + 3, word)
              colon = -1
              if later > i + 3
                k = marks[':'].bsearch_index { |position| position >= i + 3 }
                colon = marks[':'][k] if k && marks[':'][k] < later
              end
              at = later if later > i + 3 && colon > i + 3
            end
            if at < i + 3
              i += 3
              next
            end
            start, stop, userinfo = i + 3, at, true
          elsif (char == '=' || encoded_equals?(t, i)) && quoted_equals.call(i)
            size = char == '=' ? 1 : 3
            value = i + size
            value += 1 while value < length && value - i - size < 8 && t[value] == ' '
            if !skip.empty? && (span_end = skipped.call(value)) >= 0
              i = [span_end, i + 1].max
              next
            end
            name = quoted_pair.call(json_name(t, i))[1]
            start = value
            stop = equals_value_end.call(i, size, name)
            stop = consumed_end(t, value, stop, decide) unless PAIR_LIST_HEADERS.key?(normalize(name))
          elsif char == '=' || char == ':' || encoded_equals?(t, i)
            size = char == '=' || char == ':' ? 1 : 3
            pair, name, overflow = separator_pair(t, i, size, copied, credential)
            if pair
              value = i + size
              value += 1 while value < length && value - i - size < 8 && t[value] == ' '
              if !skip.empty? && (span_end = skipped.call(value)) >= 0
                # A span the catalog replaced: never read its value end (that read would run on over the rest of the URL).
                i = [span_end, i + 1].max
                next
              end
              start = value
              if !overflow && PAIR_LIST_HEADERS.key?(normalize(name))
                stop = value
                stop += 1 while stop < length && t[stop] != "\n" && t[stop] != "\r"
              else
                stop = consumed_end(t, value, pair_value_end(t, value), decide)
              end
            end
          elsif char == '-' && flag_dashes(t, i).positive? && (i.zero? || space?(t[i - 1]) || encoded_space_width(t, i, 0).positive?)
            # A --name, or a single-dash flag with a full credential name (never -p or a cluster that is no credential name).
            dashes = flag_dashes(t, i)
            name_end = i + dashes
            name_end += 1 while name_end < length && PAIR_NAME_CHARS.key?(t[name_end]) && name_end - i < NAME_CAP + dashes
            overflow = name_end - i >= NAME_CAP + dashes && name_end < length && PAIR_NAME_CHARS.key?(t[name_end])
            name_end += 1 while overflow && name_end < length && PAIR_NAME_CHARS.key?(t[name_end])
            path_budget(sub(t, i, name_end)) if overflow
            if name_end < length && name_end - i - dashes >= (dashes == 2 ? 1 : 2) &&
               (dashes_space?(t[name_end]) || LINE_BREAKS.key?(t[name_end]) || encoded_space_ahead(t, name_end).positive?) &&
               (overflow || credential.call(sub(t, i, name_end)))
              value = name_end
              control = -1
              gap_end = name_end
              spaced = false
              while value < length
                if dashes_space?(t[value])
                  value += 1
                  spaced ||= control >= 0
                  next
                end
                width = encoded_space_ahead(t, value)
                break if width.zero?
                control = value if control.negative? && encoded_control?(t, value, width)
                value += width
                gap_end = value
              end
              if dashes_separator?(t, value)
                # A separator after the gap: the pair is name<gap>=value, read at the separator.
                dashes_colon = t[value] == ':' ? value : -1
                i = value
                next
              end
              from = control >= 0 ? control : value
              value_end = -1
              if value < length && LINE_BREAKS.key?(t[value])
                # After ONE line break the next line is read fresh; a blank line ends the --name.
                line = value + (t[value] == "\r" && t[value + 1] == "\n" ? 2 : 1)
                encoded = false
                while line < length
                  if dashes_space?(t[line])
                    line += 1
                    next
                  end
                  if encoded && LINE_BREAKS.key?(t[line])
                    line += t[line] == "\r" && t[line + 1] == "\n" ? 2 : 1
                    encoded = false
                    next
                  end
                  width = encoded_space_ahead(t, line)
                  break if width.zero?
                  control = line if control.negative? && encoded_control?(t, line, width)
                  line += width
                  encoded = true
                end
                if line >= length || LINE_BREAKS.key?(t[line]) || dashes_separator?(t, line)
                  if line >= length || LINE_BREAKS.key?(t[line])
                    i = name_end
                  else
                    dashes_colon = t[line] == ':' ? line : -1
                    i = line
                  end
                  next
                end
                if line_starts_pair?(t, line, credential)
                  if control >= 0
                    start, stop = control, gap_end
                  else
                    i = line
                    next
                  end
                else
                  from = control >= 0 ? control : line
                  value_end = word_end.call(from, line)
                end
              elsif control >= 0 && !spaced && value < length
                # An encoded control right before the word starts it.
                value_end = word_end.call(from, value)
              elsif value >= length || flag_ahead?(t, value, credential)
                start, stop = control, gap_end if control >= 0
              else
                value_end = word_end.call(from, value)
              end
              start, stop = from, value_end if value_end > from
            end
            if start.negative?
              i = name_end
              next
            end
          end
          if start >= 0 && !skip.empty?
            span_end = skipped.call(start)
            # User information that runs on past the catalog's own (to a later '@') is replaced whole, the span with it.
            if span_end >= 0 && !(userinfo && stop > span_end)
              i = [span_end, i + 1].max
              next
            end
          end
          if start >= copied && stop >= start
            out ||= +''
            out << sub(t, copied, start) << replacement
            copied = stop
            i = [stop, i + 1].max
            next
          end
          i += 1
        end
        return text unless out
        out << sub(t, copied, length)
      end

      # A URL text built from kept and replaced pieces, recording by position every span the catalog replaced:
      # [start, stop, omission].
      class Rewrite
        attr_reader :spans
        def initialize
          @text = +''
          @spans = []
          @length = 0
        end
        def keep(text)
          @text << text
          @length += text.length
        end
        def put(text, omission = false)
          @spans << [@length, @length + text.length, omission]
          keep(text)
        end
        # Each run [start, stop] (sorted, disjoint; an empty run inserts the placeholder) becomes the placeholder, and each
        # data URI in the original text its omission; where a run and a data URI overlap, the run's placeholder is written
        # and the data URI's omission covers the rest of it.
        def region(text, runs = [])
          data = []
          Privacy.scan_text(DATA, text, true).each do |start, stop|
            replacement = Privacy.data_uri(text[start...stop])
            data << [start, stop, replacement] if replacement
          end
          position = 0
          length = text.length
          r = d = 0
          last = nil
          loop do
            r += 1 while r < runs.length && runs[r][1] <= position && !(runs[r][0] == runs[r][1] && runs[r][1] == position)
            if r < runs.length && runs[r][0] == runs[r][1] && runs[r][1] == position
              put(REDACTED)
              last = [:run, r]
              r += 1
              next
            end
            d += 1 while d < data.length && data[d][1] <= position
            break if position >= length
            if r < runs.length && runs[r][0] <= position
              if last != [:run, r]
                put(REDACTED)
                last = [:run, r]
              end
              position = runs[r][1]
            elsif d < data.length && data[d][0] <= position
              stop = r < runs.length ? [data[d][1], runs[r][0]].min : data[d][1]
              if last != [:data, d]
                put(data[d][2], true)
                last = [:data, d]
              end
              position = stop
            else
              stop = [length, (runs[r][0] if r < runs.length), (data[d][0] if d < data.length)].compact.min
              keep(text[position...stop])
              position = stop
              last = nil
            end
          end
        end
        def result = [@text, @spans]
      end

      # Replace each run [start, stop] (sorted, disjoint) with the placeholder. A span a run overlaps is dropped; the others move.
      def replace_runs(value, runs, spans)
        return [value, spans] if runs.empty?
        out = +''
        moved = []
        copied = shift = k = 0
        runs.each do |start, stop|
          while k < spans.length && spans[k][1] <= start
            moved << [spans[k][0] + shift, spans[k][1] + shift, spans[k][2]]
            k += 1
          end
          k += 1 while k < spans.length && spans[k][0] < stop
          out << value[copied...start] << REDACTED
          copied = stop
          shift += REDACTED.length - (stop - start)
        end
        spans[k..].each { |span| moved << [span[0] + shift, span[1] + shift, span[2]] }
        out << value[copied..]
        [out, moved]
      end

      # Whether a name after a '?' is a credential name: each name after a '?' of marks, up to stop, is read; a name longer
      # than NAME_CAP or one that holds more than QUESTION_CAP '?' marks is undecided (true).
      def after_question?(t, marks, stop)
        return false if marks.empty?
        return true if stop - marks[0] - 1 > NAME_CAP || marks.length > QUESTION_CAP
        marks.any? { |mark| query_name?(decode_component(sub(t, mark + 1, stop))) }
      end

      def question_name?(raw_name)
        return true if query_name?(decode_component(raw_name))
        t = raw_name.unpack('U*').map! { |code| [code].pack('U') }
        marks = []
        t.each_with_index do |char, at|
          next unless char == '?'
          break if marks.length > QUESTION_CAP
          marks << at
        end
        after_question?(t, marks, t.length)
      end

      # Where a credential-named pair after a second '?' starts in a query or fragment value (after its '='), or -1.
      def second_question_cut(raw_value)
        return -1 unless raw_value.include?('?')
        t = raw_value.unpack('U*').map! { |code| [code].pack('U') }
        i = t.index('?')
        marks = []
        while i < t.length
          if t[i] == '?'
            marks << i if marks.length <= QUESTION_CAP
          elsif t[i] == '='
            return i + 1 if !marks.empty? && after_question?(t, marks, i)
            marks = []
          end
          i += 1
        end
        -1
      end

      # The URL path pair rule: every '=' and %3D in each path segment; a credential name's value is replaced to the next
      # pair in the segment or the segment end.
      def path_pairs(base, out)
        b = base.unpack('U*').map! { |code| [code].pack('U') }
        path = 0
        lowered = base[0, 8].downcase
        if lowered.start_with?('http://') || lowered.start_with?('https://')
          path = find(b, '/', base.index('//') + 2)
          path = b.length if path.negative?
        end
        runs = []
        position = path
        while position < b.length
          stop = find(b, '/', position + 1)
          stop = b.length if stop.negative?
          first = b[position] == '/' ? position + 1 : position
          k = name = first
          decided = false
          while k < stop
            char = b[k]
            if PATH_PAIR_SEPARATORS.key?(char)
              k += 1
              name = k
              next
            end
            size = char == '=' ? 1 : (encoded_equals?(b, k) ? 3 : 0)
            if size.zero?
              k += 1
              next
            end
            credential = k > name && credential_name?(decode_percent(sub(b, name, k)))
            credential = credential_name?(decode_percent(sub(b, first, k))) if !decided && !credential && k > first
            decided = true
            if credential
              value_end = path_value_end(b, k + size, stop)
              runs << [k + size, value_end]
              k = name = value_end
            else
              k = name = k + size
            end
          end
          position = stop
        end
        out.region(base, runs)
      end

      def path_value_end(b, start, stop)
        k = start
        while k < stop
          unless PATH_PAIR_SEPARATORS.key?(b[k])
            k += 1
            next
          end
          j = k + 1
          j += 1 while j < stop && !PATH_PAIR_SEPARATORS.key?(b[j]) && b[j] != '=' && !encoded_equals?(b, j)
          return k if j < stop && j > k + 1 && !PATH_PAIR_SEPARATORS.key?(b[j])
          k = j
        end
        stop
      end

      # The part of a URL after its '#': split at '&' like a query; a credential-named pair's value (the name read whole or
      # after a '?') is replaced to the next '&', and so is a credential pair after a second '?' in any other value.
      def tail_pairs(segment, out)
        runs = []
        offset = 0
        segment.split('&', -1).each do |piece|
          raw_name, equals, raw_value = piece.partition('=')
          value = offset + raw_name.length + equals.length
          stop = offset + piece.length
          if !equals.empty? && question_name?(raw_name)
            runs << [value, stop]
          elsif !equals.empty?
            cut = second_question_cut(raw_value)
            runs << [value + cut, stop] if cut >= 0
          end
          offset = stop + 1
        end
        out.region(segment, runs)
      end

      # Scrub one already-framed URL: [text, spans the catalog replaced].
      def framed_url(original, budget)
        out = Rewrite.new
        target, hashmark, fragment = original.partition('#')
        base, question, raw_query = target.partition('?')
        if (userinfo = USERINFO.match(base))
          out.keep(userinfo[1])
          out.put(REDACTED)
          out.keep('@')
          base = userinfo.post_match
        end
        path_pairs(base, out)
        out.keep(question)
        parsed = raw_query.split('&', -1).map do |pair|
          raw_name, equals, raw_value = pair.partition('=')
          [raw_name, equals, raw_value, decode_component(raw_name), decode_component(raw_value)]
        end
        # Queries are flat objects: index every hint before scrubbing any value, keeping duplicates.
        hints = index_hints(parsed.reject { |pair| pair[1].empty? }.map { |pair| [pair[3], pair[4]] }, '', {}, budget)
        parsed.each_with_index do |(raw_name, equals, raw_value, name, decoded), index|
          out.keep('&') if index.positive?
          out.keep(raw_name + equals)
          next if equals.empty?
          if query_name?(name) || question_name?(raw_name)
            out.put(REDACTED)
            next
          end
          replacement = media_replacement(decoded, name, false, false, hints)
          if replacement
            # The whole value is media: its omission covers a pair after a second '?' in it too.
            out.put(replacement, true)
            next
          end
          cut = second_question_cut(raw_value)
          if cut >= 0
            out.keep(raw_value[0...cut])
            out.put(REDACTED)
          elsif (data_uri(decoded) || data_text(decoded)) != decoded
            # The decoded value with each data URI replaced; only those are spans.
            out.region(decoded)
          else
            out.keep(raw_value)
          end
        end
        out.keep(hashmark)
        tail_pairs(fragment, out) unless hashmark.empty?
        out.result
      end

      def url_value(original, budget) = framed_url(original, budget)[0]

      # The runs [start, stop] (character indices) of a value shape's matches.
      def shape_runs(shape, text)
        runs = shape_byte_runs(shape, text)
        return runs if runs.empty? || text.ascii_only?
        to_char = offset_maps(text)[1]
        runs.map { |start, stop| [to_char[start], to_char[stop]] }
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
      # Four limits bound what the fragment matcher may do. Three are for one value:
      #   MATCH_ROUNDS - rounds over every secret that may still change the value;
      #   MATCH_STEPS  - automaton steps, in total;
      #   MATCH_WORK   - everything scanned, in total: each character of every scan of the text, every candidate
      #                  examined for every secret (and every character a membership test reads), the binary searches
      #                  over the candidates, the code points of every secret, the table and the bit masks built and
      #                  used for it, the output built for a replacement, and the automaton steps. Each amount is
      #                  charged BEFORE the work it pays for, so the charged total never exceeds the limit.
      # The fourth is for one span, that is, for every value scrubbed with one Budget:
      #   MATCH_SPAN_WORK - the same work, added up over all of those values (a value's work counts toward both its own
      #                  MATCH_WORK and this). Without it a span of max_nodes strings could cost that many times
      #                  MATCH_WORK.
      # Past a value's limit, the whole value becomes the placeholder; a fragment is never left behind. Past the
      # span's, that value and every later value of the span becomes the placeholder. A value that still changes in
      # the last round is replaced whole only if one more pass over it would change it again.
      MATCH_ROUNDS = 8
      MATCH_STEPS = 200_000
      MATCH_WORK = 16_000_000
      # What the matcher costs scales with the registry, not only with the text: a value that holds a placeholder is
      # read once for every registered secret (up to 1024), at the length of the secret, and each placeholder is
      # examined for every secret. A span with a full registry of long credentials and a few hundred placeholders in
      # its values can therefore reach this bound; the span of a few values with a few placeholders, and the largest
      # span without them, cannot: the text budget of a span is max_text_chars (4 MiB) and charges each string twice,
      # so a span holds about 2 MiB of text, and a value with one fragment next to a placeholder costs about 4 units a
      # character (12 million for 3 million characters), about 8 million for such a span. The bound is 32 million:
      # twice MATCH_WORK, so a single value can still use all of its own. Matching costs from 20 to 90 nanoseconds a
      # unit, so the bound caps the matching of one span at roughly one to three seconds on the host's thread.
      # Past it the value being matched and every later value of the span (and every later name in it) is the
      # placeholder. Privacy.snapshot scrubs a span's name, status text, resource and scope before its attributes, so
      # the span keeps its identity. The fallbacks that run only after the snapshot failed (Privacy.safe_metadata, with
      # one Budget for keys and one for values, and Privacy.recover_identity, with one for the name and one for the
      # resource) each use a Budget of their own, so the worst case for one span is five times this bound.
      MATCH_SPAN_WORK = 32_000_000
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
      # placeholder and removes characters, so the process ends; the limits above bound the work, and a
      # value that reaches any of them is replaced whole.
      def close_fragments(text, secrets, work = [0, 0, nil])
        return text unless text.include?(REDACTED)
        # work: characters and candidates scanned, automaton steps. The scan is rebuilt only after a change and then
        # serves the rest of the round, the next round, and the final check.
        scan = fragment_scan(text, work)
        MATCH_ROUNDS.times do
          changed = false
          secrets.each do |secret|
            replaced = replace_fragments(scan, secret, work)
            next if replaced.nil?
            text = replaced; changed = true
            scan = fragment_scan(text, work)
          end
          return text unless changed
        end
        secrets.any? { |secret| replace_fragments(scan, secret, work) } ? REDACTED : text
      rescue MatchLimit
        REDACTED
      end

      # Charges `amount` before the work it pays for; past the limit the work is never done.
      # `work` is [work, steps] of one value, with the span's Budget as a third item when there is one: the amount
      # is charged to the span as well.
      def spend(work, amount)
        raise MatchLimit if work[0] + amount > MATCH_WORK
        work[2]&.spend_work(amount)
        work[0] += amount
      end

      # The text as items (a character's code point, or nil for a placeholder), the placeholder positions, and the positions of
      # each character, so that one secret finds its candidate starts without reading the whole text.
      def fragment_scan(text, work)
        spend(work, text.length + 1)
        items = []
        parts = text.split(REDACTED, -1)
        parts.each_with_index do |part, index|
          items.concat(part.codepoints)
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
        spend(work, 2 * size) # codepoints reads the whole secret, and so does the set of its characters below
        codes = secret.codepoints
        present = {}
        codes.each { |code| present[code] = true }
        listed = positions.fetch(codes[0], nil)
        starts = []
        second = codes[1]
        # Two binary searches per token, each at most one step more than the bits of the list's length.
        spend(work, 2 * tokens.length * (listed.length.bit_length + 1)) if listed
        tokens.each do |token|
          # A match that starts at the placeholder goes on with a character of the secret (or another placeholder).
          following = items[token + 1]
          if token + 1 >= items.length || following.nil?
            starts << token
          else
            starts << token if present.key?(following) # one hash lookup; the loop is charged above
          end
          next unless listed
          low = token - size + 1
          first = listed.bsearch_index { |index| index >= low }
          next unless first
          last = listed.bsearch_index { |index| index >= token } || listed.length
          spend(work, last - first + 1) # the window walk below
          (first...last).each do |at|
            following = items[listed[at] + 1]
            starts << listed[at] if following.nil? || following == second
          end
        end
        return nil if starts.empty?
        # One step per start, and one per character of the secret for the loop that builds the table. The table sets
        # bit `index` of one mask per distinct character, and both `1 << index` and the `|=` make a mask of at most
        # index / 64 + 1 words: at most 2 * (size + size * (size - 1) / 128) words in all, which 2 * size +
        # size * size / 64 covers.
        spend(work, starts.length + size + 2 * size + ((size * size) >> 6))
        table = Hash.new(0)
        codes.each_with_index { |code, index| table[code] |= 1 << index }
        full = (1 << (size + 1)) - 1
        accept = 1 << size
        output = []; cursor = 0; found = false
        starts.uniq.sort.each do |start|
          next if start < cursor
          stop = match_end(items, start, table, full, accept, work, size)
          next unless stop
          spend(work, start - cursor + 1)
          output.concat(items[cursor...start]); output << nil
          cursor = stop; found = true
        end
        return nil unless found
        spend(work, (items.length - cursor) + output.length + 1) # the final copy, then map and join of the whole output
        output.concat(items[cursor..])
        output.slice_when { |a, b| a.nil? != b.nil? }.map { |run| run.first.nil? ? REDACTED * run.length : run.pack('U*') }.join
      end

      # The end of the longest match of the automaton from `start` that holds a placeholder and a literal, or nil.
      def match_end(items, start, table, full, accept, work, size)
        mask = 1; position = start; best = nil; token = false; literal = false
        # A step makes at most seven operations on masks of at most size + 1 bits (the placeholder branch: negate, and,
        # shift, subtract, complement, and, then the test against `accept`), each reading and writing at most
        # (size + 1) / 64 + 1 words; 8 words' worth covers them, plus one for the step itself.
        step = 1 + 8 * (((size + 1) >> 6) + 1)
        while mask != 0 && position < items.length
          work[1] += 1
          raise MatchLimit if work[1] > MATCH_STEPS
          spend(work, step)
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
      # Every exported string: the URL catalog and data URIs, then known secrets, then value shapes, then the pair rule.
      # structured: the text is the re-dumped JSON of a structured string, whose leaf strings already went through the pair
      # rule one by one; it is not read again. A whole URL value at a framed site keeps, by position, the spans the catalog
      # replaced; the pair rule skips only those.
      def clean_string(value, secrets, budget = Budget.new, framed = true, structured = false)
        budget.spend(value.bytesize)
        # The span has used all the matching work it may: nothing more of it is read.
        return REDACTED if budget.work_spent?
        text = value.encode('UTF-8', invalid: :replace, undef: :replace)
        replacement = media_replacement(text, '', false, false, {}, framed)
        return replacement if replacement
        # Secrets one at a time, longest first (the caller's order), none matching across a placeholder; after the pair
        # rule, whatever of an overlapping secret the first pass left beside a placeholder (see close_fragments).
        # Secrets are read as the registry reads them (invalid bytes become U+FFFD), never skipped for their bytes.
        plain = if secrets.instance_of?(NormalizedSecrets)
                  secrets.plain
                else
                  normalize_secrets(secrets).select { |secret| secret != REDACTED && !secret.include?(REDACTED) }
                end
        if framed && url_value?(text)
          text, spans = framed_url(text, budget)
          plain.each do |secret|
            runs = []
            offset = 0
            text.split(REDACTED, -1).each do |part|
              found = part.index(secret)
              while found
                runs << [offset + found, offset + found + secret.length]
                found = part.index(secret, found + secret.length)
              end
              offset += part.length + REDACTED.length
            end
            text, spans = replace_runs(text, runs, spans)
          end
          VALUE_SHAPES.each { |shape| text, spans = replace_runs(text, shape_runs(shape, text), spans) }
          # The pair rule reads the text the secrets left, one secret at a time; whatever of an overlapping secret is left
          # beside a placeholder is closed after it (close_fragments only adds redaction).
          return close_fragments(pair_rule(text, spans), plain, [0, 0, budget])
        end
        original_text = text
        text = substitute(TEXT, text, false) do |original, kind, _start, byte_start|
          if kind == :data
            data_text(original)
          else
            # Retain enclosing single quotes in prose without treating internal apostrophes as delimiters.
            closing_quote = ''
            if byte_start.positive? && original_text.byteslice(byte_start - 1, 1) == "'" && original.end_with?("'")
              original = original[0...-1]
              closing_quote = "'"
            end
            url_value(original, budget) + closing_quote
          end
        end
        plain.each do |secret|
          text = text.split(REDACTED, -1).map { |part| part.gsub(secret, REDACTED) }.join(REDACTED)
        end
        VALUE_SHAPES.each { |shape| text = shape_gsub(shape, text) }
        text = pair_rule(text) unless structured
        close_fragments(text, plain, [0, 0, budget])
      end
    end
  end
end
