# frozen_string_literal: true
# Private JSON codec. Never require/activate the application's json gem and never
# invoke to_json/as_json/to_s on application values.
module TraceAI
  module Auto
    module Codec
      class ParserError < StandardError; end
      class NestingError < ParserError; end
      module_function
      def type(value) = Object.instance_method(:class).bind_call(value)
      def type_name(value) = Module.instance_method(:name).bind_call(type(value)) || 'Object'
      def generate(value, depth = 0, budget = [100_000])
        budget[0] -= 1
        raise NestingError if depth > 64 || budget[0] < 0
        klass = type(value)
        if klass == String
          '"' + value.encode('UTF-8', invalid: :replace, undef: :replace).gsub(/["\\\x00-\x1f]/) { |c| {'"' => '\\"', '\\' => '\\\\', "\b" => '\\b', "\f" => '\\f', "\n" => '\\n', "\r" => '\\r', "\t" => '\\t'}[c] || '\\u%04x' % c.ord } + '"'
        elsif klass == Integer
          value.to_s
        elsif klass == Float
          value.finite? ? value.to_s : generate(value.nan? ? 'NaN' : (value.positive? ? 'Infinity' : '-Infinity'), depth + 1, budget)
        elsif klass == TrueClass then 'true'
        elsif klass == FalseClass then 'false'
        elsif klass == NilClass then 'null'
        elsif klass == Array
          '[' + value.map { |v| generate(v, depth + 1, budget) }.join(',') + ']'
        elsif klass == Hash
          '{' + value.map { |k, v| generate(type(k) == String ? k : (type(k) == Symbol ? k.name : type_name(k)), depth + 1, budget) + ':' + generate(v, depth + 1, budget) }.join(',') + '}'
        elsif klass == Symbol
          generate(value.name, depth + 1, budget)
        else
          generate(type_name(value), depth + 1, budget)
        end
      end
      def parse(text) = Reader.new(text).parse
      class Reader
        def initialize(text)
          raise ParserError unless Codec.type(text) == String && text.bytesize <= 64 * 1024 * 1024
          @text = text.dup.force_encoding('UTF-8'); @pos = 0; @remaining = 100_000
          raise ParserError unless @text.valid_encoding?
          @text = @text.b
        end
        def parse
          result = value(0); whitespace
          raise ParserError unless @pos == @text.length
          result
        end
        def whitespace
          @pos += 1 while @pos < @text.length && " \t\r\n".include?(@text[@pos])
        end
        def value(depth)
          @remaining -= 1
          raise NestingError if depth > 64 || @remaining < 0
          whitespace
          case @text[@pos]
          when '"' then string
          when '{', '['
            object = @text[@pos] == '{'; closing = object ? '}' : ']'; @pos += 1
            result = object ? {} : []; whitespace
            if @text[@pos] == closing then @pos += 1; return result; end
            loop do
              if object
                whitespace; key = string; whitespace
                raise ParserError unless @text[@pos] == ':'
                @pos += 1; result[key] = value(depth + 1)
              else
                result << value(depth + 1)
              end
              whitespace; separator = @text[@pos]; @pos += 1
              break if separator == closing
              raise ParserError unless separator == ','
            end
            result
          else
            literal = /\G(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.match(@text, @pos)&.to_s
            raise ParserError unless literal && literal.bytesize <= 1024
            exponent = /[eE]([+-]?\d+)\z/.match(literal)
            raise ParserError if exponent && exponent[1].to_i > 308
            @pos += literal.length
            case literal
            when 'true' then true
            when 'false' then false
            when 'null' then nil
            else
              number = literal.match?(/[.eE]/) ? literal.to_f : Integer(literal, 10)
              raise ParserError if number.is_a?(Float) && !number.finite?
              number
            end
          end
        end
        def string
          raise ParserError unless @text[@pos] == '"'
          @pos += 1; out = +''.b
          loop do
            part = /\G([^"\\\x00-\x1f]*)(["\\]|[\x00-\x1f]|\z)/n.match(@text, @pos)
            raise ParserError unless part && !part[2].empty?
            out << part[1]; @pos = part.end(0)
            char = part[2]
            return out.force_encoding('UTF-8') if char == '"'
            raise ParserError if char.ord < 32
            if char == '\\'
              char = @text[@pos]; @pos += 1
              if char == 'u'
                hex = @text[@pos, 4]; raise ParserError unless hex&.match?(/\A[0-9a-fA-F]{4}\z/)
                @pos += 4; point = hex.to_i(16)
                if (0xd800..0xdbff).cover?(point)
                  pair = @text[@pos, 6]; raise ParserError unless pair&.match?(/\A\\u[dD][c-fC-F][0-9a-fA-F]{2}\z/)
                  @pos += 6; point = 0x10000 + ((point - 0xd800) << 10) + pair[2..].to_i(16) - 0xdc00
                elsif (0xdc00..0xdfff).cover?(point)
                  raise ParserError
                end
                out << [point].pack('U')
              else
                decoded = {'"' => '"', '\\' => '\\', '/' => '/', 'b' => "\b", 'f' => "\f", 'n' => "\n", 'r' => "\r", 't' => "\t"}[char]
                raise ParserError unless decoded
                out << decoded
              end
            else
              out << char
            end
          end
        end
      end
    end
  end
end
