# frozen_string_literal: true
module TraceAI
  module Auto
    # A defined boot boundary, not a general Ruby loader interceptor. Leave
    # application exceptions untouched and discover only after all gems load.
    module BundlerHook
      def require(...)
        result = super
        Auto.bundler_boot
        result
      end
    end
  end
end
