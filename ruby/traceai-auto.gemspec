# frozen_string_literal: true
Gem::Specification.new do |s|
  s.name = 'traceai-auto'
  s.version = '0.1.0'
  s.summary = 'Zero-code, privacy-aware AI wire tracing for Ruby'
  s.authors = ['Future AGI']
  s.license = 'Apache-2.0'
  s.required_ruby_version = '>= 3.1'
  s.files = Dir['lib/**/*', 'bin/*', 'README.md']
  s.bindir = 'bin'
  s.executables = ['traceai-auto']
  s.require_paths = ['lib']
  # RUBYOPT precedes Bundler. Runtime dependencies here would let RubyGems
  # activate them before our fail-open shim even runs. SDK forwarding is optional;
  # the dependency-free wire pipeline works without selecting application gems.
end
