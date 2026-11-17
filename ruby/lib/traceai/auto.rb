# frozen_string_literal: true
# Keep this shim parseable by older Rubies: the launcher may inject an embedded
# agent into any interpreter. Check compatibility before loading Ruby 3 syntax.
begin
  major, minor = RUBY_VERSION.split('.').map(&:to_i)
  if major > 3 || (major == 3 && minor >= 2)
    require_relative 'auto/agent'
    TraceAI::Auto.preload
  else
    warn 'traceai-auto: Ruby >= 3.2 required; application left running'
  end
rescue LoadError, SyntaxError, StandardError => e
  warn "traceai-auto: preload failed (#{e.class.name}); application left running"
end
