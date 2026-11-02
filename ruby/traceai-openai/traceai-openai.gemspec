# frozen_string_literal: true
Gem::Specification.new do |spec|
  spec.name = 'traceai-openai'
  spec.version = '0.1.0' # x-release-please-version
  spec.summary = 'Opt-in OpenTelemetry spans for the official OpenAI Ruby SDK'
  spec.description = 'Chat Completions and Responses traces, including streaming usage and errors.'
  spec.authors = ['Future AGI']
  spec.homepage = 'https://github.com/future-agi/traceAI'
  spec.license = 'Apache-2.0'
  spec.required_ruby_version = '>= 3.3'
  spec.files = Dir['lib/**/*.rb', 'README.md', 'LICENSE']
  spec.require_paths = ['lib']
  spec.add_dependency 'openai', '>= 0.115.0', '< 1.0'
  spec.add_dependency 'opentelemetry-api', '~> 1.0'
end
