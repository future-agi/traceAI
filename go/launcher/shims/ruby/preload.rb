# A missing gem in any child Ruby must never prevent that child from starting.
begin
  unless defined?($traceai_launcher_loaded) && $traceai_launcher_loaded
    $traceai_launcher_loaded = true
    begin
      require 'traceai/auto'
    rescue Exception
      begin
        fallback = __TRACEAI_RUBY_LIB__
        if fallback && !fallback.empty?
          $LOAD_PATH << fallback unless $LOAD_PATH.include?(fallback)
          load File.join(fallback, 'traceai', 'auto.rb')
        end
      rescue Exception
        # Unsupported Ruby, missing dependencies, and broken agents fail open.
      end
    end
  end
rescue Exception
end
