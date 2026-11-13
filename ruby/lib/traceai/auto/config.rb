# frozen_string_literal: true
# Dependency-free until enabled: RUBYOPT runs before Bundler resolves the app's
# gems. Even requiring a default gem such as JSON here can break a pinned bundle.
module TraceAI
  module Auto
    class Config
      DEFAULTS = {'base_url' => 'https://api.futureagi.com', 'project_type' => 'observe', 'wire_capture' => true,
                  'wire_max_body_bytes' => 4_194_304, 'sample_rate' => 1.0, 'auto_debug' => false}.freeze
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
      def initialize(arguments = {}, env: ENV, cwd: Dir.pwd)
        file = arguments[:config_file] || arguments['config_file'] || env['FI_CONFIG_FILE'] || File.join(cwd, 'futureagi.toml')
        file_values = read_file(file)
        environment = env.each_with_object({}) do |(k, v), out|
          name = Config.utf8(k)
          out[name.delete_prefix('FI_').downcase] = Config.utf8(v) if name.start_with?('FI_')
        end
        explicit = arguments.to_h { |k, v| [Config.utf8(k.to_s), v] }
        @values = DEFAULTS.merge(file_values).merge(environment).merge(explicit)
        # Credentials are one atomic pair per source, never independently merged.
        @warnings = []
        @credential_values = [explicit, environment, file_values].flat_map do |source|
          %w[api_key secret_key].filter_map { |key| Config.utf8(source[key]).dup.freeze if source[key].instance_of?(String) }
        end.freeze
        @values.delete('api_key'); @values.delete('secret_key')
        selected = nil
        [['explicit arguments', explicit], ['environment', environment], ['config file', file_values]].each do |label, source|
          api = source['api_key']; secret = source['secret_key']
          has_api = api.instance_of?(String) && !api.empty?
          has_secret = secret.instance_of?(String) && !secret.empty?
          if has_api && has_secret
            selected ||= [api, secret]
          elsif has_api || has_secret
            @warnings << "#{label} credential pair ignored: missing #{has_api ? 'FI_SECRET_KEY' : 'FI_API_KEY'}"
          end
        end
        @values['api_key'], @values['secret_key'] = selected if selected
        @warnings.freeze
        @warnings.each { |message| warn 'traceai-auto: ' + message }
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
      def truthy?(key) = self[key] == true || self[key].to_s.downcase == 'true'
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
      private
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
      def read_file(path)
        return {} unless File.file?(path) && File.size(path) <= 65_536
        # TOML is UTF-8. Never decode it in the locale encoding (US-ASCII under
        # LANG=C); a line that is not valid UTF-8 is skipped like a malformed one.
        text = File.binread(path).force_encoding(Encoding::UTF_8).delete_prefix("﻿")
        text.each_line.each_with_object({}) do |line, out|
          next unless line.valid_encoding?
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
            # Unsupported/malformed lines do not affect other settings.
            next
          end
        end
      rescue StandardError
        {}
      end
    end
  end
end
