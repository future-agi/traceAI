# frozen_string_literal: true
module TraceAI
  module Auto
    # The one gate every FI-key-carrying send passes, before a key header exists.
    #
    # 1. Literal loopback (localhost, 127.0.0.1, ::1 exactly as written) may use plaintext.
    # 2. Future AGI hosts need https; the opt-in never applies to them.
    # 3. A target whose written and parsed host could differ is refused.
    # 4. Any other plaintext target needs FI_ALLOW_INSECURE_HTTP to be exactly "1";
    #    that opt-in warns once per process, without values.
    # 5. No proxy is consulted: the exporter opens its own socket to the peer.
    # 6. A 3xx answer to a key-carrying send is a refusal.
    # Refusals carry no configured value and no key.
    module KeyTransport
      # `reason` is one of REASONS; it is the only detail a refusal warning prints.
      REASONS = ['insecure target', 'redirect', 'peer not loopback', 'unparseable URL'].freeze
      class Refused < StandardError
        attr_reader :reason
        def initialize(message = 'FI keys are not sent over this transport', reason: 'insecure target')
          super(message)
          @reason = reason
        end
      end
      LOOPBACK = %w[localhost 127.0.0.1 ::1].freeze
      LOCK = Mutex.new
      class << self
        def reset_warning
          LOCK.synchronize { @warned = false; @refusal_warned = false }
        end

        def allowed?(base, opt_in: ENV['FI_ALLOW_INSECURE_HTTP'])
          check(base, opt_in: opt_in)
          true
        rescue Refused
          false
        end

        # Raises Refused unless keys may go to base (the target as written).
        def check(base, opt_in: ENV['FI_ALLOW_INSECURE_HTTP'])
          written = written_host(base.to_s) or raise Refused.new(reason: 'unparseable URL')
          scheme = base.to_s[/\A(https?):\/\//i, 1].to_s.downcase
          parsed = Wire.parse_url(base.to_s)
          raise Refused.new(reason: 'unparseable URL') unless parsed && parsed.host.casecmp?(written)
          return true if scheme == 'https'
          raise Refused, 'Future AGI hosts require https' if future_agi?(written)
          return true if LOOPBACK.include?(written.downcase)
          raise Refused, 'plaintext target; FI_ALLOW_INSECURE_HTTP=1 allows it on a trusted network' unless opt_in == '1'
          warn_once
          true
        end

        # The connected peer must agree with a loopback claim.
        def peer_agrees?(written, socket)
          return true unless LOOPBACK.include?(written.to_s.downcase)
          address = socket.remote_address
          address.ipv4_loopback? || address.ipv6_loopback?
        rescue StandardError
          false
        end

        def refuses_redirect?(status_line) = status_line.match?(/\AHTTP\/1\.[01] 3\d\d(?: |\r)/)

        # The host exactly as written: nil for any form whose reading could
        # differ between parsers (userinfo, backslash, percent, spaces, empty).
        def written_host(base)
          found = /\Ahttps?:\/\/([^\/?#]*)/i.match(base) or return nil
          return nil if base.match?(/[\\@%\s\x00-\x1f\x7f]/)
          authority = found[1]
          host = if authority.start_with?('[')
                   authority[/\A\[([^\]]*:[^\]]*)\](?::\d+)?\z/, 1]
                 else
                   authority[/\A([^:\[\]]+)(?::\d+)?\z/, 1]
                 end
          host unless host.nil? || host.empty?
        end

        # One value-free warning per process for the first refused send.
        def warn_refused(error)
          return unless claim(:@refusal_warned)
          reason = error.respond_to?(:reason) && REASONS.include?(error.reason) ? error.reason : 'insecure target'
          detail = error.message.include?('FI_ALLOW_INSECURE_HTTP') ? '; FI_ALLOW_INSECURE_HTTP=1 allows plaintext only on a trusted network' : ''
          Kernel.warn "traceai-auto: Future AGI export refused by the key transport check (#{reason}#{detail})"
        end

        private

        def future_agi?(host)
          name = host.downcase.delete_suffix('.')
          name == 'futureagi.com' || name.end_with?('.futureagi.com')
        end

        # True for the first caller only: the check of the flag and its write are one step under the lock.
        def claim(flag)
          LOCK.synchronize do
            return false if instance_variable_get(flag)
            instance_variable_set(flag, true)
          end
        end

        def warn_once
          return unless claim(:@warned)
          Kernel.warn 'traceai-auto: FI_ALLOW_INSECURE_HTTP=1 sends Future AGI keys over plaintext HTTP to a non-loopback target'
        end
      end
    end
  end
end
