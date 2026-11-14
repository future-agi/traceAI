# frozen_string_literal: true
# Dependency-free until enabled: RUBYOPT runs before Bundler resolves the app's
# gems. Even requiring a default gem such as JSON here can break a pinned bundle.
module TraceAI
  module Auto
    class Config
      DEFAULTS = {'base_url' => 'https://api.futureagi.com', 'project_type' => 'observe', 'wire_capture' => true,
                  'wire_max_body_bytes' => 4_194_304, 'sample_rate' => 1.0, 'auto_debug' => false}.freeze
      # The hide switches the agent reads: an FI_HIDE_* variable, the init option
      # or the config file key of the same name.
      HIDE_SWITCHES = %w[hide_inputs hide_outputs hide_input_messages hide_output_messages hide_input_text hide_output_text
                         hide_input_images hide_embedding_vectors hide_llm_invocation_parameters hide_error_details].freeze
      HIDE_VALUES = {'true' => true, '1' => true, 'yes' => true, 'on' => true,
                     'false' => false, '0' => false, 'no' => false, 'off' => false}.freeze
      attr_reader :values, :warnings, :credential_values
      # Config values are UTF-8 whatever the process locale. Under LANG=C, Ruby
      # tags environment values and paths as US-ASCII even when they hold UTF-8.
      def self.utf8(value)
        return value unless value.instance_of?(String)
        encoding = value.encoding
        return value if encoding == Encoding::UTF_8 && value.valid_encoding?
        if [Encoding::UTF_8, Encoding::US_ASCII, Encoding::ASCII_8BIT].include?(encoding) || !value.valid_encoding?
          text = value.b.force_encoding(Encoding::UTF_8)
          text.valid_encoding? ? text : text.scrub
        else
          value.encode(Encoding::UTF_8, invalid: :replace, undef: :replace)
        end
      rescue EncodingError
        value.b.force_encoding(Encoding::UTF_8).scrub
      end
      # announce: false leaves printing the warnings to the caller (the agent
      # prints each one once per process, however often it reads its config).
      def initialize(arguments = {}, env: ENV, cwd: Dir.pwd, announce: true)
        # Reading the config file can already add a warning (see read_file).
        @warnings = []
        file = arguments[:config_file] || arguments['config_file'] || env['FI_CONFIG_FILE'] || File.join(cwd, 'futureagi.toml')
        file_values = read_file(file)
        environment = env.each_with_object({}) do |(k, v), out|
          name = Config.utf8(k)
          out[name.delete_prefix('FI_').downcase] = Config.utf8(v) if name.start_with?('FI_')
        end
        explicit = arguments.to_h { |k, v| [Config.utf8(k.to_s), v] }
        # A blank hide switch is not set, so it does not replace a lower source's value.
        set = ->(source) { source.reject { |key, value| HIDE_SWITCHES.include?(key) && Config.blank?(value) } }
        @values = DEFAULTS.merge(file_values).merge(set.call(environment)).merge(set.call(explicit))
        # Credentials are one atomic pair per source, never independently merged.
        @credential_values = [explicit, environment, file_values].flat_map do |source|
          %w[api_key secret_key].filter_map { |key| Config.utf8(source[key]).dup.freeze if source[key].instance_of?(String) }
        end.freeze
        @values.delete('api_key'); @values.delete('secret_key')
        selected = nil; selected_source = nil
        [['explicit arguments', explicit], ['environment', environment], ['config file', file_values]].each do |label, source|
          api = source['api_key']; secret = source['secret_key']
          has_api = api.instance_of?(String) && !api.empty?
          has_secret = secret.instance_of?(String) && !secret.empty?
          if has_api && has_secret
            selected_source ||= label
            selected ||= [api, secret]
          elsif has_api || has_secret
            @warnings << "#{label} credential pair ignored: missing #{has_api ? 'FI_SECRET_KEY' : 'FI_API_KEY'}"
          end
        end
        @values['api_key'], @values['secret_key'] = selected if selected
        # futureagi.toml is meant to be committed. Its base_url must not send key
        # pairs from elsewhere (the environment or init) to another host, so it
        # counts only when the key pair comes from the same file.
        from_file = file_values.key?('base_url') && !environment.key?('base_url') && !explicit.key?('base_url')
        if from_file && selected && selected_source != 'config file'
          @values['base_url'] = DEFAULTS['base_url']
          @warnings << 'config file base_url ignored: the key pair does not come from the same file'
        end
        # Hide switches are read once, here, from the merged values, so a value that
        # a higher source replaces is never reported by this read. (A RUBYOPT
        # preload reads without the init options; see read_hide.)
        @hidden = HIDE_SWITCHES.to_h { |key| [key, read_hide(key, @values[key])] }.freeze
        @warnings.freeze
        @warnings.each { |message| warn 'traceai-auto: ' + message } if announce
        @values['auto_debug'] = @values.delete('debug') if @values.key?('debug')
        @values['project_name'] ||= Config.utf8(env['OTEL_SERVICE_NAME'] || File.basename(cwd))
        @values['project_name'] = 'DEFAULT_PROJECT_NAME' if @values['project_name'].to_s.empty?
        @values['sdk_disabled'] = env['OTEL_SDK_DISABLED'].to_s.downcase == 'true'
        @values['wire_max_body_bytes'] = [[Integer(@values['wire_max_body_bytes']), 1].max, 64 * 1024 * 1024].min
        @values['sample_rate'] = Float(@values['sample_rate']).clamp(0.0, 1.0)
        # Snapshot options without freezing or retaining the application's mutable
        # strings/lists. There is no live configuration/reconfiguration path.
        @values = @values.transform_values do |value|
          if value.instance_of?(String)
            Config.utf8(value).dup.freeze
          elsif value.instance_of?(Array)
            value.map { |item| item.instance_of?(String) ? Config.utf8(item).dup.freeze : item }.freeze
          else
            value
          end
        end.freeze
      end
      def [](key) = @values[key.to_s]
      # Switches other than the hide switches: on only for true, in any letter case.
      def truthy?(key) = self[key] == true || self[key].to_s.downcase == 'true'
      # A hide switch as read at startup: true (hide), false (show) or nil (not set).
      def hide_setting(key) = @hidden[key.to_s]
      # True when the named hide switch hides its content.
      def hidden?(key) = hide_setting(key) == true
      def list(key)
        v = self[key]
        v.is_a?(Array) ? v.map(&:to_s) : v.to_s.split(',').map(&:strip).reject(&:empty?)
      end
      def enabled?(integration)
        !truthy?('sdk_disabled') && !list('disabled_instrumentations').include?(integration) && (list('instrumentations').empty? || list('instrumentations').include?(integration))
      end
      def credentials? = !self['api_key'].to_s.empty? && !self['secret_key'].to_s.empty?
      def masked
        require_relative 'privacy'
        masked = @values.to_h { |k, v| [k, Privacy.credential?(k) ? (v.to_s.empty? ? '(unset)' : '<redacted>') : v] }
        secrets = (Auto.secret_values + Privacy.secrets({}, @credential_values)).uniq.sort_by { |value| -value.length }
        Privacy.scrub(masked, secrets)
      rescue StandardError
        {}
      end
      def self.blank?(value) = value.nil? || (value.instance_of?(String) && utf8(value).strip.empty?)
      private
      # Reads one hide switch, ignoring case and surrounding spaces: true, 1, yes
      # and on hide; false, 0, no and off show; nil when the value is blank (not
      # set). Any other value hides, because a hide switch someone set must never
      # silently do nothing. The warning names the variable and never the value.
      # Its text is the same for every read, so the agent prints it once. Under
      # RUBYOPT the first read comes before the application can call init, and
      # an init option that sets the switch then decides; the text allows for it.
      def read_hide(key, value)
        text = begin
          Config.utf8(value.to_s).strip.downcase
        rescue StandardError
          nil
        end
        return nil if text == ''
        return HIDE_VALUES[text] if HIDE_VALUES.key?(text)
        @warnings << "FI_#{key.upcase} has a value that is not recognised, so this content is hidden unless an init option sets it; " \
                     'use true, 1, yes or on to hide it, or false, 0, no or off to show it'
        true
      end
      QUOTED_STRING = /"(?:[^"\\\x00-\x1f]|\\(?:["\\\/bfnrt]|u[0-9a-fA-F]{4}))*"/.freeze
      ESCAPES = {'"' => '"', '\\' => '\\', '/' => '/', 'b' => "\b", 'f' => "\f", 'n' => "\n", 'r' => "\r", 't' => "\t"}.freeze
      def parse_string(raw)
        raise ArgumentError unless raw.match?(/\A#{QUOTED_STRING}\z/)
        result = raw[1...-1].gsub(/\\u([dD][89aAbB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})|\\(["\\\/bfnrt])/) do
          high, low, code, escape = Regexp.last_match.captures
          if high
            [0x10000 + ((high.to_i(16) - 0xd800) << 10) + low.to_i(16) - 0xdc00].pack('U')
          elsif code
            point = code.to_i(16)
            raise ArgumentError if (0xd800..0xdfff).cover?(point)
            [point].pack('U')
          else
            ESCAPES.fetch(escape)
          end
        end
        raise ArgumentError unless result.valid_encoding?
        result
      end
      def parse_value(raw)
        return true if raw == 'true'
        return false if raw == 'false'
        return parse_string(raw) if raw.start_with?('"')
        if raw.start_with?('[') && raw.end_with?(']')
          rest = raw[1...-1].strip; values = []
          until rest.empty?
            item = /\A(#{QUOTED_STRING})\s*(,\s*|\z)/.match(rest)
            raise ArgumentError unless item
            values << parse_string(item[1])
            rest = rest[item.end(0)..].to_s
            raise ArgumentError if rest.empty? && !item[2].empty?
          end
          return values
        end
        # TOML's flat subset also permits signed numbers and digit separators.
        number = raw.delete('_')
        raise ArgumentError unless number.match?(/\A[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\z/)
        number.match?(/[.eE]/) ? Float(number) : Integer(number, 10)
      end
      # A config file that is there but cannot be used is never passed over
      # without a word, because the hide settings in it then do nothing. One
      # warning names its path and what failed (its size, the read or the
      # decoding) and never its content. What the file sets is unchanged: a file
      # over 64 KiB or one that cannot be read sets nothing, and a line that is
      # not valid UTF-8 is skipped. A file that is not there is not reported.
      def read_file(path)
        present = File.file?(path)
        return {} unless present
        if File.size(path) > 65_536
          file_warning(path, 'is over the size limit of 64 KiB, so it is not read and none of its settings apply')
          return {}
        end
        # TOML is UTF-8. Never decode it in the locale encoding (US-ASCII under
        # LANG=C); a line that is not valid UTF-8 is skipped like a malformed one.
        text = File.binread(path).force_encoding(Encoding::UTF_8).delete_prefix([0xFEFF].pack('U'))
        undecodable = false
        values = text.each_line.each_with_object({}) do |line, out|
          unless line.valid_encoding?
            undecodable = true
            next
          end
          # Remove comments outside quoted strings, respecting JSON/TOML escapes.
          quote = false; escape = false; clean = +''
          line.each_char do |c|
            break if c == '#' && !quote
            clean << c
            if escape then escape = false
            elsif c == '\\' && quote then escape = true
            elsif c == '"' then quote = !quote
            end
          end
          found = /\A\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*\z/.match(clean)
          next unless found
          key = found[1] == 'debug' ? 'auto_debug' : found[1]
          begin
            out[key] = parse_value(found[2])
          rescue ArgumentError, RangeError, EncodingError
            # Unsupported/malformed lines do not affect other settings. A hide
            # switch is the exception, because one someone set must never be
            # dropped: its text is kept (a single-quoted string without its
            # quotes, otherwise as written) and read_hide decides.
            raw = found[2]
            next unless HIDE_SWITCHES.include?(key) && !raw.empty?
            out[key] = raw.length >= 2 && raw.start_with?("'") && raw.end_with?("'") ? raw[1...-1] : raw
          end
        end
        if undecodable
          file_warning(path, 'has lines that are not valid UTF-8 and cannot be decoded, so those lines are skipped and their settings do not apply')
        end
        values
      rescue StandardError
        file_warning(path, 'could not be read, so none of its settings apply') if present
        {}
      end
      # The text is the same for every read of the same file, so the agent
      # prints it once per process. It holds the path and never the content.
      def file_warning(path, problem)
        @warnings << "config file #{Config.utf8(path.to_s)} #{problem}"
      rescue StandardError
        nil
      end
    end
  end
end
